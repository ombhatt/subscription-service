from __future__ import annotations

import pytest

from app.errors import QuotaExceeded
from app.models import Subscription
from app.plans import Tier
from app.services import quota
from app.services.entitlements import resolve_entitlements


async def entitlements_for(session, user_id: str, tier: str, status: str = "active") -> dict:
    if tier != "free":
        session.add(Subscription(user_id=user_id, tier=tier, status=status))
        await session.commit()
    return await resolve_entitlements(session, user_id)


async def test_free_tier_is_capped(session):
    ents = await entitlements_for(session, "q1", "free")
    limit = next(q["limit"] for q in ents["quotas"] if q["key"] == "messages_per_day")

    for _ in range(limit):
        await quota.consume(session, user_id="q1", key="messages_per_day", entitlements=ents)

    with pytest.raises(QuotaExceeded) as excinfo:
        await quota.consume(session, user_id="q1", key="messages_per_day", entitlements=ents)

    error = excinfo.value
    assert error.limit == limit
    assert error.current_tier == "free"
    assert error.upgrade_tier == "plus", "point at the cheapest tier that lifts the cap"
    assert error.to_payload()["remaining"] == 0


async def test_paid_tier_gets_its_own_ceiling(session):
    ents = await entitlements_for(session, "q2", "pro")
    state = await quota.consume(
        session, user_id="q2", key="messages_per_day", entitlements=ents
    )
    assert state["limit"] == 1500
    assert state["remaining"] == 1499


async def test_unlimited_quota_never_raises(session):
    ents = await entitlements_for(session, "q3", "pro")
    for _ in range(50):
        state = await quota.consume(
            session, user_id="q3", key="file_uploads_per_day", entitlements=ents
        )
    assert state["limit"] is None
    assert state["remaining"] is None


async def test_counters_are_per_user(session):
    ents_a = await entitlements_for(session, "q4", "free")
    ents_b = await entitlements_for(session, "q5", "free")
    await quota.consume(session, user_id="q4", key="messages_per_day", entitlements=ents_a)
    state = await quota.peek("q5", "messages_per_day", ents_b)
    assert state["used"] == 0


async def test_daily_window_starts_at_utc_midnight(session):
    ents = await entitlements_for(session, "q6", "free")
    start, end = quota.window_for("daily", ents)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end - start).days == 1


async def test_billing_window_follows_the_subscribers_own_period(session):
    """A monthly cap must reset on the renewal date, not on the 1st."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    period_start = now - timedelta(days=10)
    period_end = now + timedelta(days=20)
    ents = {
        "current_period_start": period_start.isoformat(),
        "current_period_end": period_end.isoformat(),
    }
    start, end = quota.window_for("billing_period", ents)
    assert start == period_start
    assert end == period_end


async def test_free_users_fall_back_to_the_calendar_month(session):
    ents = {"current_period_start": None, "current_period_end": None}
    start, end = quota.window_for("billing_period", ents)
    assert start.day == 1
    assert end.day == 1
    assert end > start


def test_upgrade_target_is_the_cheapest_tier_that_helps():
    assert quota.upgrade_tier_for("messages_per_day", Tier.FREE) is Tier.PLUS
    assert quota.upgrade_tier_for("file_uploads_per_day", Tier.PLUS) is Tier.PRO
    assert quota.upgrade_tier_for("messages_per_day", Tier.PRO) is None
