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

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.cache import get_cache, get_json, set_json
from app.config import get_settings
from app.models import PAID_STATUSES, EntitlementGrant, Subscription
from app.observability import entitlement_cache, entitlement_invalidations
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


_MARK_KEY = "entitlements_to_invalidate"


async def invalidate_entitlements(user_id: str) -> None:
    """Drop this user's cached entitlements.

    **Bypasses the ordering guarantee.** Called directly from a write path,
    before that write commits, it clears the key and a concurrent reader
    immediately repopulates it from the uncommitted row -- so the stale answer
    outlives the write by a full TTL. That is a real defect this codebase had,
    and it is why write paths call `mark_entitlements_stale` and commit through
    `commit_and_invalidate` instead.

    Kept public for the cases with no write to order against: clearing a cache
    by hand, and test setup.
    """
    await get_cache().delete(_key(user_id))


def mark_entitlements_stale(session: AsyncSession, user_id: str) -> None:
    """Register that this transaction changes what `user_id` may do.

    The invalidation happens at `commit_and_invalidate`, not here, so a write
    path three frames below the commit can register without threading a return
    value up through every layer -- which is exactly how the old code ended up
    invalidating from inside the write.

    Nothing happens if the transaction rolls back: the marks live on the
    session and go with it.
    """
    session.sync_session.info.setdefault(_MARK_KEY, set()).add(user_id)


async def commit_and_invalidate(session: AsyncSession) -> None:
    """Commit, then drop the cached entitlements of everyone this touched.

    The order is the whole point, and it is inside here so no caller can get it
    wrong. Invalidating before the commit leaves a window in which a concurrent
    reader sees the pre-commit row and caches it; invalidating after leaves no
    window at all.

    Marks are taken *before* the commit and the set is cleared, so the
    undrained-marks detector below stays silent on this path and fires only on
    a plain `session.commit()`.

    A delete that fails is logged and counted, never raised. The row is already
    durably written; raising here would invite the caller to retry a completed
    operation, and for a webhook it would make Stripe redeliver an event we
    have already processed. The cost of swallowing it is one user holding a
    stale entitlement for up to the TTL, which is strictly better.
    """
    user_ids = session.sync_session.info.pop(_MARK_KEY, set())
    await session.commit()

    for user_id in user_ids:
        try:
            await invalidate_entitlements(user_id)
            entitlement_invalidations.labels(outcome="ok").inc()
        except Exception:
            entitlement_invalidations.labels(outcome="failed").inc()
            log.exception(
                "committed, but could not invalidate entitlements for %s; "
                "they hold a stale answer until it expires",
                user_id,
            )


@event.listens_for(Session, "after_rollback")
def _discard_marks_on_rollback(session: Session) -> None:
    """A rolled-back write changes nothing, so it invalidates nothing.

    `session.info` is a plain dict and is *not* transactional -- marks survive
    a rollback on their own. Without this the webhook failure path, which rolls
    back and then commits an error record, would invalidate for a write that
    never happened and trip the undrained detector on every failed webhook.
    """
    session.info.pop(_MARK_KEY, None)


@event.listens_for(Session, "after_commit")
def _warn_on_undrained_marks(session: Session) -> None:
    """Detector for a plain `session.commit()` under a marking write.

    Installed at import time rather than wired from `main.py`'s lifespan,
    because the cron jobs never run lifespan -- and `expire_grace` is a marking
    write path by definition, running further from anyone watching than any
    request does.

    It only logs, so the fact that this callback is synchronous and cannot
    await the delete does not matter. Curing it here would need a scheduled
    task that can silently fail, which is the failure mode being removed.
    """
    left = session.info.pop(_MARK_KEY, None)
    if left:
        entitlement_invalidations.labels(outcome="undrained").inc()
        log.error(
            "session.commit() dropped entitlement invalidations for %s -- "
            "use commit_and_invalidate() instead",
            sorted(left),
        )


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


def _ttl_for(data: dict[str, Any]) -> int:
    """How long this answer stays true.

    Normally the configured TTL. But a subscriber inside a dunning grace window
    has an expiry date on their access, and crossing it is the passage of time
    rather than a write -- so no invalidation can ever reach the cached entry.
    Without this cap they keep paid access for up to the full TTL after the
    window closes, and nothing in the system knows to stop them.

    Capping at the boundary makes the entry expire exactly when the answer
    stops being true.

    Returns 0 to mean "do not cache this". Under a second of window left, the
    floor and the cap fight: a TTL of 1 outlives the boundary, and a TTL of 0
    means *no expiry* to some backends, which is far worse. Declining to cache
    is the only answer that is wrong in neither direction, and it applies for
    at most one second per subscriber per dunning cycle.
    """
    ttl = get_settings().entitlement_cache_ttl
    grace_until = data.get("grace_ends_at")
    if not grace_until:
        return ttl

    remaining = (as_utc(datetime.fromisoformat(grace_until)) - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return ttl  # already past it; the answer is stable again
    if remaining < 1:
        return 0
    # int() truncates, and that direction is deliberate: the entry expires just
    # *before* the boundary rather than just after it. Rounding would let a
    # subscriber hold paid access for up to a second past the window on an
    # entry nothing can invalidate.
    return min(ttl, int(remaining))


async def resolve_entitlements(session: AsyncSession, user_id: str) -> dict[str, Any]:
    cached = await get_json(_key(user_id))
    if cached is not None:
        entitlement_cache.labels(result="hit").inc()
        return cached

    try:
        data = await _resolve_from_db(session, user_id)
    except Exception:
        stale = await get_json(_stale_key(user_id))
        if stale is not None:
            # Worth its own label: serving stale means the database is in
            # trouble, and a rising rate here is an outage in progress.
            entitlement_cache.labels(result="stale").inc()
            log.exception("entitlement lookup failed for %s; serving stale copy", user_id)
            return stale
        raise

    entitlement_cache.labels(result="miss").inc()

    ttl = _ttl_for(data)
    if ttl > 0:
        await set_json(_key(user_id), data, ttl)
    # The stale copy is written regardless: it exists to survive a database
    # outage, not to answer normally, and the read path only reaches for it
    # when _resolve_from_db has already failed.
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
