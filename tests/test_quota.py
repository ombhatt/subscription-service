from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.errors import QuotaExceeded
from app.models import SalesInquiry, Subscription
from app.plans import QuotaWindow, Tier
from app.services import quota
from app.services.entitlements import Entitlements, resolve_entitlements


async def entitlements_for(
    session, user_id: str, tier: str, status: str = "active"
) -> Entitlements:
    if tier != "free":
        session.add(Subscription(user_id=user_id, tier=tier, status=status))
        await session.commit()
    return await resolve_entitlements(session, user_id)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


async def test_free_tier_is_capped(session):
    ents = await entitlements_for(session, "q1", "free")
    limit = ents.quota("messages_per_day").limit

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


async def test_daily_window_is_the_utc_day_containing_now(session, clock):
    ents = await entitlements_for(session, "q6", "free")
    clock.now = utc(2026, 3, 14, 23, 59, 59)
    assert quota.window_for(QuotaWindow.DAILY, ents) == (
        utc(2026, 3, 14),
        utc(2026, 3, 15),
    )


async def test_billing_window_follows_the_subscribers_own_period(session, clock):
    """A monthly cap must reset on the renewal date, not on the 1st."""
    period_start = clock.now - timedelta(days=10)
    period_end = clock.now + timedelta(days=20)
    ents = (await entitlements_for(session, "q6b", "pro")).model_copy(
        update={"current_period_start": period_start, "current_period_end": period_end}
    )
    start, end = quota.window_for(QuotaWindow.BILLING_PERIOD, ents)
    assert start == period_start
    assert end == period_end


@pytest.mark.parametrize(
    ("now", "month_start", "next_month_start"),
    [
        (utc(2026, 1, 15, 12), utc(2026, 1, 1), utc(2026, 2, 1)),
        (utc(2026, 1, 31, 23, 59, 59), utc(2026, 1, 1), utc(2026, 2, 1)),
        (utc(2026, 2, 1), utc(2026, 2, 1), utc(2026, 3, 1)),
        (utc(2026, 12, 20), utc(2026, 12, 1), utc(2027, 1, 1)),
    ],
    ids=["mid-month", "last-second-of-month", "first-instant-of-month", "december"],
)
async def test_free_users_fall_back_to_the_calendar_month(
    session, clock, now, month_start, next_month_start
):
    ents = await entitlements_for(session, "q6c", "free")
    assert ents.current_period_start is None
    clock.now = now
    assert quota.window_for(QuotaWindow.BILLING_PERIOD, ents) == (
        month_start,
        next_month_start,
    )


# --------------------------------------------------------------------------
# The mirror writes in its own transaction, never in the caller's.
#
# `consume` used to commit the request session -- and roll it back when the
# mirror hit trouble -- so whatever the caller had pending was saved early or
# thrown away, depending on how the mirror fared. Harmless only while the one
# metered endpoint had nothing pending when it consumed.
# --------------------------------------------------------------------------


async def _count(sessionmaker_, model) -> int:
    async with sessionmaker_() as fresh:
        return (await fresh.execute(select(func.count()).select_from(model))).scalar_one()


async def test_consume_never_commits_the_callers_work(session, sessionmaker_):
    """The caller decides whether its work lands. The usage record lands either
    way, because Redis has already counted the request and nothing refunds it."""
    ents = await entitlements_for(session, "q7", "free")

    session.add(SalesInquiry(email="half-done@example.test", source="test"))
    await quota.consume(session, user_id="q7", key="messages_per_day", entitlements=ents)
    await session.rollback()  # the caller abandons its own work

    assert await _count(sessionmaker_, SalesInquiry) == 0, (
        "consume committed a write that belonged to its caller"
    )
    async with sessionmaker_() as fresh:
        rows = await quota.counter_rows(fresh, "q7")
    assert [row.count for row in rows] == [1], "the mirror must not depend on the caller's commit"


async def test_a_mirror_failure_leaves_the_callers_work_alone(session, sessionmaker_, monkeypatch):
    """The mirror is best-effort and swallows its own failures. It used to do
    that by rolling back the caller's session, discarding whatever the caller
    had pending."""
    ents = await entitlements_for(session, "q8", "free")

    def broken_update(*args, **kwargs):
        raise RuntimeError("usage mirror unavailable")

    monkeypatch.setattr(quota, "update", broken_update)

    session.add(SalesInquiry(email="keep-me@example.test", source="test"))
    state = await quota.consume(session, user_id="q8", key="messages_per_day", entitlements=ents)
    assert state["used"] == 1, "enforcement must not depend on the mirror"

    await session.commit()  # the caller finishes its own work
    assert await _count(sessionmaker_, SalesInquiry) == 1, (
        "a mirror failure discarded a write that belonged to its caller"
    )


def test_upgrade_target_is_the_cheapest_tier_that_helps():
    assert quota.upgrade_tier_for("messages_per_day", Tier.FREE) is Tier.PLUS
    assert quota.upgrade_tier_for("file_uploads_per_day", Tier.PLUS) is Tier.PRO
    assert quota.upgrade_tier_for("messages_per_day", Tier.PRO) is None
