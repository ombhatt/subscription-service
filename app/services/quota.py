"""Metering and enforcement.

Redis is the enforcement path: an atomic INCR against a key whose TTL expires
exactly when the window does, so counters clean themselves up and no reset job
exists to fall behind. Postgres holds a mirror for support and analytics.

The window is the subtle part. A "daily" cap resets at UTC midnight for
everyone; a "billing period" cap resets on the subscriber's own renewal date,
which is a different day for almost every customer. Getting those two confused
is how some customers get six free weeks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_cache
from app.errors import QuotaExceeded
from app.models import UsageCounter
from app.observability import quota_rejections
from app.plans import CATALOG, TIER_RANK, QuotaWindow, Tier
from app.services.entitlements import quota_limit

log = logging.getLogger(__name__)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _calendar_month(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, end


def window_for(
    window: str, entitlements: dict[str, Any], now: datetime | None = None
) -> tuple[datetime, datetime]:
    now = now or datetime.now(UTC)
    if window == QuotaWindow.DAILY.value:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    start = _parse(entitlements.get("current_period_start"))
    end = _parse(entitlements.get("current_period_end"))
    if start and end and start <= now < end:
        return start, end
    # Free users have no billing period; fall back to the calendar month so the
    # counter still has a well-defined window.
    return _calendar_month(now)


def _counter_key(user_id: str, key: str, window_start: datetime) -> str:
    return f"quota:{user_id}:{key}:{int(window_start.timestamp())}"


def upgrade_tier_for(key: str, current: Tier) -> Tier | None:
    """The cheapest tier that actually raises this cap.

    Pointing every blocked user at the most expensive plan is both worse for
    them and worse for conversion.
    """
    current_limit = CATALOG[current].quotas[key].limit if key in CATALOG[current].quotas else 0
    for tier in sorted(TIER_RANK, key=lambda t: TIER_RANK[t]):
        if TIER_RANK[tier] <= TIER_RANK[current]:
            continue
        quota = CATALOG[tier].quotas.get(key)
        if quota is None:
            continue
        if quota.limit is None or (current_limit is not None and quota.limit > current_limit):
            return tier
    return None


async def peek(user_id: str, key: str, entitlements: dict[str, Any]) -> dict[str, Any] | None:
    """Current usage without consuming. None if this tier does not meter `key`."""
    found = quota_limit(entitlements, key)
    if found is None:
        return None
    limit, window = found
    start, end = window_for(window, entitlements)
    used = await get_cache().get_int(_counter_key(user_id, key, start))
    return {
        "key": key,
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(0, limit - used),
        "reset_at": end,
    }


async def states(user_id: str, entitlements: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for quota in entitlements.get("quotas", []):
        state = await peek(user_id, quota["key"], entitlements)
        if state is not None:
            out.append(state)
    return out


async def consume(
    session: AsyncSession,
    *,
    user_id: str,
    key: str,
    entitlements: dict[str, Any],
) -> dict[str, Any]:
    """Record one unit of usage, or raise QuotaExceeded.

    Increment-then-check: the counter may read one above the limit for a
    rejected request, which is correct -- it records the attempt.
    """
    found = quota_limit(entitlements, key)
    if found is None:
        return {"key": key, "limit": None, "used": 0, "remaining": None}

    limit, window = found
    start, end = window_for(window, entitlements)
    ttl = max(60, int((end - datetime.now(UTC)).total_seconds()))
    used = await get_cache().incr(_counter_key(user_id, key, start), ttl)

    if limit is not None and used > limit:
        current = Tier(entitlements["tier"])
        quota_rejections.labels(quota=key, tier=current.value).inc()
        raise QuotaExceeded(
            key=key,
            limit=limit,
            used=used,
            reset_at=end,
            current_tier=current.value,
            upgrade_tier=(t.value if (t := upgrade_tier_for(key, current)) else None),
        )

    await _mirror(session, user_id=user_id, key=key, start=start, end=end)
    return {
        "key": key,
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(0, limit - used),
        "reset_at": end,
    }


async def _mirror(
    session: AsyncSession, *, user_id: str, key: str, start: datetime, end: datetime
) -> None:
    """Durable copy of the counter. Never blocks enforcement: a failure here is
    logged and swallowed, because Redis already made the decision."""
    try:
        result = await session.execute(
            update(UsageCounter)
            .where(
                UsageCounter.user_id == user_id,
                UsageCounter.key == key,
                UsageCounter.window_start == start,
            )
            .values(count=UsageCounter.count + 1)
        )
        if result.rowcount == 0:
            session.add(
                UsageCounter(
                    user_id=user_id, key=key, window_start=start, window_end=end, count=1
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                # Another worker created the row between the update and the
                # insert; the update now finds it.
                await session.rollback()
                await session.execute(
                    update(UsageCounter)
                    .where(
                        UsageCounter.user_id == user_id,
                        UsageCounter.key == key,
                        UsageCounter.window_start == start,
                    )
                    .values(count=UsageCounter.count + 1)
                )
        await session.commit()
    except Exception:
        log.exception("usage mirror failed for %s/%s", user_id, key)
        await session.rollback()


async def counter_rows(session: AsyncSession, user_id: str) -> list[UsageCounter]:
    result = await session.execute(
        select(UsageCounter)
        .where(UsageCounter.user_id == user_id)
        .order_by(UsageCounter.window_start.desc())
        .limit(50)
    )
    return list(result.scalars().all())
