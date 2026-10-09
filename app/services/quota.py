"""Metering and enforcement.

Redis is the enforcement path: an atomic INCR against a key whose TTL expires
exactly when the window does, so counters clean themselves up and no reset job
exists to fall behind. Postgres holds a mirror for support and analytics.

When Redis is unreachable, enforcement fails open: the request is allowed,
`quota_errors_total` counts it, and the mirror still records it. A paying
customer refused by our outage is worse than a few uncounted requests -- the
same trade the read path and the contact-sales rate limiter make.

The window is the subtle part. A "daily" cap resets at UTC midnight for
everyone; a "billing period" cap resets on the subscriber's own renewal date,
which is a different day for almost every customer. Getting those two confused
is how some customers get six free weeks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import timeutil
from app.cache import get_cache
from app.errors import QuotaExceeded
from app.models import UsageCounter
from app.observability import quota_errors, quota_rejections
from app.plans import CATALOG, TIER_RANK, QuotaWindow, Tier
from app.services.entitlements import Entitlements
from app.timeutil import as_utc

log = logging.getLogger(__name__)


def _calendar_month(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, end


def window_for(window: QuotaWindow, entitlements: Entitlements) -> tuple[datetime, datetime]:
    now = timeutil.utcnow()
    if window is QuotaWindow.DAILY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    start = as_utc(entitlements.current_period_start)
    end = as_utc(entitlements.current_period_end)
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
        if not CATALOG[tier].purchasable:
            # This value drives a *buy* button. Enterprise lifts every cap but
            # cannot be bought, so offering it here would put a customer one
            # click from a checkout that refuses them. A Pro subscriber who
            # runs out is the strongest Enterprise lead there is, but that is a
            # "contact sales" prompt, not an upgrade target.
            continue
        quota = CATALOG[tier].quotas.get(key)
        if quota is None:
            continue
        if quota.limit is None or (current_limit is not None and quota.limit > current_limit):
            return tier
    return None


async def peek(user_id: str, key: str, entitlements: Entitlements) -> dict[str, Any] | None:
    """Current usage without consuming. None if this tier does not meter `key`."""
    found = entitlements.quota(key)
    if found is None:
        return None
    limit = found.limit
    start, end = window_for(found.window, entitlements)
    try:
        used = await get_cache().get_int(_counter_key(user_id, key, start))
    except Exception:
        # Display only: enforcement is `consume`. An unreachable cache must not
        # take the entitlement payload down with it. No identifiers in the
        # message: the outage is the cache's, not this caller's.
        log.exception("quota read failed; reporting 0 used")
        used = 0
    return {
        "key": key,
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(0, limit - used),
        "reset_at": end,
    }


async def states(user_id: str, entitlements: Entitlements) -> list[dict[str, Any]]:
    out = []
    for quota in entitlements.quotas:
        state = await peek(user_id, quota.key, entitlements)
        if state is not None:
            out.append(state)
    return out


async def consume(
    session: AsyncSession,
    *,
    user_id: str,
    key: str,
    entitlements: Entitlements,
) -> dict[str, Any]:
    """Record one unit of usage, or raise QuotaExceeded.

    Increment-then-check: the counter may read one above the limit for a
    rejected request, which is correct -- it records the attempt.

    Never commits, rolls back or flushes `session`. The caller owns its
    transaction; `session` is used only to find the database, and the durable
    mirror is written in a transaction of its own (see `_mirror`).
    """
    found = entitlements.quota(key)
    if found is None:
        return {"key": key, "limit": None, "used": 0, "remaining": None}

    limit = found.limit
    start, end = window_for(found.window, entitlements)
    ttl = max(60, int((end - timeutil.utcnow()).total_seconds()))
    try:
        used = await get_cache().incr(_counter_key(user_id, key, start), ttl)
    except Exception:
        quota_errors.labels(quota=key).inc()
        log.exception("quota check for %s failed; allowing the request", key)
        await _mirror(session.bind, user_id=user_id, key=key, start=start, end=end)
        # Not counted, so reported like peek() reports an unreachable counter.
        return {"key": key, "limit": limit, "used": 0, "remaining": limit}

    if limit is not None and used > limit:
        current = entitlements.tier
        quota_rejections.labels(quota=key, tier=current.value).inc()
        raise QuotaExceeded(
            key=key,
            limit=limit,
            used=used,
            reset_at=end,
            current_tier=current.value,
            upgrade_tier=(t.value if (t := upgrade_tier_for(key, current)) else None),
        )

    await _mirror(session.bind, user_id=user_id, key=key, start=start, end=end)
    return {
        "key": key,
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(0, limit - used),
        "reset_at": end,
    }


async def _mirror(
    engine: AsyncEngine, *, user_id: str, key: str, start: datetime, end: datetime
) -> None:
    """Durable copy of the counter, in its own short transaction.

    Never blocks enforcement: a failure here is logged and swallowed, because
    Redis already made the decision.

    Not the caller's session. This used to commit the request session, saving
    whatever the endpoint had pending before the endpoint had decided to, and
    to roll it back on a mirror failure, throwing that work away. The mirror
    should not follow the caller's outcome in any case: Redis counted this
    request when it was made and nothing refunds it, so the durable copy
    records the same fact whether or not the endpoint's own work lands.

    The cost is a second pooled connection for one UPDATE (or INSERT) and its
    commit, and only while the request session is holding one as well.
    """
    try:
        # Inside the try with everything else: nothing about the mirror may
        # escape to the endpoint.
        increment = (
            update(UsageCounter)
            .where(
                UsageCounter.user_id == user_id,
                UsageCounter.key == key,
                UsageCounter.window_start == start,
            )
            .values(count=UsageCounter.count + 1)
        )
        async with AsyncSession(engine, expire_on_commit=False) as own:
            result = await own.execute(increment)
            if result.rowcount == 0:
                own.add(
                    UsageCounter(
                        user_id=user_id, key=key, window_start=start, window_end=end, count=1
                    )
                )
                try:
                    await own.flush()
                except IntegrityError:
                    # Another worker created the row between the update and the
                    # insert; the update now finds it.
                    await own.rollback()
                    await own.execute(increment)
            await own.commit()
    except Exception:
        log.exception("usage mirror failed for %s/%s", user_id, key)


async def counter_rows(session: AsyncSession, user_id: str) -> list[UsageCounter]:
    result = await session.execute(
        select(UsageCounter)
        .where(UsageCounter.user_id == user_id)
        .order_by(UsageCounter.window_start.desc())
        .limit(50)
    )
    return list(result.scalars().all())
