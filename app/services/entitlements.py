"""The read path.

Hit on every product request, so it is cached and it fails *open*: if the
database is unreachable we serve the last entitlement set we computed rather
than locking a paying customer out of a product they have paid for. The write
path fails closed. That asymmetry is deliberate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_cache, get_json, set_json
from app.config import get_settings
from app.models import PAID_STATUSES, EntitlementGrant, Subscription
from app.plans import CATALOG, TIER_RANK, Tier, higher_tier, limits_for
from app.policy import grace_ends_at, grace_expired
from app.timeutil import as_utc

log = logging.getLogger(__name__)

# Bump the version segment whenever the cached shape changes; old entries then
# expire on their own instead of being read back with missing fields.
_CACHE_VERSION = "v1"
# How long a fail-open copy stays usable. Long, because it is only ever read
# when the database is already down.
_STALE_TTL = 24 * 60 * 60


def _key(user_id: str) -> str:
    return f"ent:{_CACHE_VERSION}:{user_id}"


def _stale_key(user_id: str) -> str:
    return f"ent:{_CACHE_VERSION}:stale:{user_id}"


async def invalidate_entitlements(user_id: str) -> None:
    """Called by every write that can change what a user may do."""
    await get_cache().delete(_key(user_id))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _active_grant_tier(session: AsyncSession, user_id: str) -> Tier | None:
    now = datetime.now(UTC)
    result = await session.execute(
        select(EntitlementGrant).where(
            EntitlementGrant.user_id == user_id,
            EntitlementGrant.revoked_at.is_(None),
        )
    )
    tiers = [
        Tier(grant.tier)
        for grant in result.scalars().all()
        if grant.expires_at is None or as_utc(grant.expires_at) > now
    ]
    if not tiers:
        return None
    return max(tiers, key=lambda t: TIER_RANK[t])


async def _resolve_from_db(session: AsyncSession, user_id: str) -> dict[str, Any]:
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    sub = result.scalar_one_or_none()

    subscription_tier = Tier.FREE
    status = "free"
    period_start = None
    period_end = None
    cancel_at_period_end = False
    grace_until = None

    if sub is not None:
        status = sub.status
        period_start = sub.current_period_start
        period_end = sub.current_period_end
        cancel_at_period_end = sub.cancel_at_period_end
        grace_until = grace_ends_at(sub)
        paid = sub.status in (s.value for s in PAID_STATUSES)
        # The grace check runs here as well as in the nightly job so a lapsed
        # subscriber does not keep access just because the job has not fired.
        if paid and not grace_expired(sub):
            subscription_tier = Tier(sub.tier)

    grant_tier = await _active_grant_tier(session, user_id)
    effective = (
        subscription_tier
        if grant_tier is None
        else higher_tier(subscription_tier, grant_tier)
    )

    if grant_tier is not None and TIER_RANK[grant_tier] > TIER_RANK[subscription_tier]:
        source = "grant"
    elif subscription_tier is not Tier.FREE:
        source = "subscription"
    else:
        source = "default"

    definition = limits_for(effective)
    return {
        "user_id": user_id,
        "tier": effective.value,
        "display_name": definition.display_name,
        "status": status,
        "source": source,
        "features": dict(definition.features),
        "quotas": [
            {"key": q.key, "limit": q.limit, "window": q.window.value}
            for q in definition.quotas.values()
        ],
        "current_period_start": _iso(as_utc(period_start)),
        "current_period_end": _iso(as_utc(period_end)),
        "cancel_at_period_end": cancel_at_period_end,
        "grace_ends_at": _iso(grace_until),
    }


async def resolve_entitlements(session: AsyncSession, user_id: str) -> dict[str, Any]:
    cached = await get_json(_key(user_id))
    if cached is not None:
        return cached

    try:
        data = await _resolve_from_db(session, user_id)
    except Exception:
        stale = await get_json(_stale_key(user_id))
        if stale is not None:
            log.exception("entitlement lookup failed for %s; serving stale copy", user_id)
            return stale
        raise

    ttl = get_settings().entitlement_cache_ttl
    await set_json(_key(user_id), data, ttl)
    await set_json(_stale_key(user_id), data, _STALE_TTL)
    return data


def tier_of(entitlements: dict[str, Any]) -> Tier:
    return Tier(entitlements["tier"])


def feature(entitlements: dict[str, Any], name: str, default: Any = None) -> Any:
    return entitlements.get("features", {}).get(name, default)


def quota_limit(entitlements: dict[str, Any], key: str) -> tuple[int | None, str] | None:
    """(limit, window) for a quota key on this entitlement set, or None if the
    tier does not meter it at all."""
    for q in entitlements.get("quotas", []):
        if q["key"] == key:
            return q["limit"], q["window"]
    return None


def minimum_tier_for_feature(name: str, value: Any) -> Tier | None:
    """The cheapest tier whose `name` feature includes `value`.

    Used to tell a blocked user which upgrade actually unblocks them, instead of
    always pointing at the most expensive plan.
    """
    for tier in sorted(TIER_RANK, key=lambda t: TIER_RANK[t]):
        available = CATALOG[tier].features.get(name)
        if isinstance(available, list) and value in available:
            return tier
        if isinstance(available, bool) and available and value:
            return tier
    return None
