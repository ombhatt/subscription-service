"""The nightly grace-expiry job.

It was covered only in `integration/`, which needs real Stripe and network
and is excluded from CI by `testpaths`, so a change that broke it passed
every check in the repo. Grace expiry is what actually revokes access from
someone who has stopped paying.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.jobs.expire_grace import expire_grace_windows
from app.models import SubscriptionAudit, SubscriptionStatus
from app.plans import Tier
from app.services.entitlements import resolve_entitlements
from tests.rows import past_due, reload, seed


async def test_a_closed_grace_window_revokes_effective_access(session):
    """What the customer can use, not what the column says.

    This used to assert `sub.tier == free`, which was asserting that the job
    overwrote the Stripe mirror -- the very thing that made `reconcile` see
    drift and write it back, nightly, forever. The mirror is Stripe's; what
    changes here is the effective tier, and that is what the read path serves.
    """
    # DUNNING_GRACE_DAYS is 7 in the test environment.
    sub = await seed(session, **past_due(days_ago=8))

    expired = await expire_grace_windows(session)

    assert expired == ["u1"]
    await reload(session, sub)
    assert sub.tier == Tier.PRO.value, "the mirror is Stripe's to write, not this job's"
    assert (await resolve_entitlements(session, "u1")).tier == Tier.FREE.value


async def test_a_grace_window_still_open_is_left_alone(session):
    """Most failed renewals are expired cards; cutting a willing payer off early
    converts them to churn."""
    sub = await seed(session, **past_due(days_ago=2))

    expired = await expire_grace_windows(session)

    assert expired == []
    await reload(session, sub)
    assert sub.tier == Tier.PRO.value, "still inside the window -- keep paid access"


async def test_someone_already_on_free_is_skipped(session):
    """Otherwise every free row is rewritten and audited every single night."""
    await seed(session, tier=Tier.FREE.value, status=SubscriptionStatus.PAST_DUE.value,
               past_due_since=datetime.now(UTC) - timedelta(days=30))

    assert await expire_grace_windows(session) == []
    rows = (await session.execute(select(SubscriptionAudit))).scalars().all()
    assert rows == [], "no state changed, so nothing should be audited"


async def test_the_revocation_is_audited_with_its_reason(session):
    """Support has to be able to answer 'why did I lose access last night'."""
    await seed(session, **past_due(days_ago=10))

    await expire_grace_windows(session)

    audit = (await session.execute(select(SubscriptionAudit))).scalars().all()
    assert len(audit) == 1
    assert audit[0].reason == "dunning.grace_expired"
    assert audit[0].from_tier == Tier.PRO.value
    assert audit[0].to_tier == Tier.FREE.value


async def test_the_entitlement_cache_is_invalidated(session):
    """The window has to close *after* the entitlements were cached.

    The read path re-derives `grace_expired` on a cache miss, so a subscriber
    whose window has already closed resolves to free whether or not this job
    has ever run -- that is the point of applying the rule live. What the job's
    invalidation protects is the other case: entitlements cached while the
    window was still open, which would keep serving Pro from the cache until
    the TTL lapsed even after the row was revoked.
    """
    sub = await seed(session, **past_due(days_ago=2))       # window still open
    cached = await resolve_entitlements(session, "u1")
    assert cached.tier == Tier.PRO.value, "precondition: cached while still in grace"

    # The window closes.
    sub.past_due_since = datetime.now(UTC) - timedelta(days=10)
    await session.commit()

    assert await expire_grace_windows(session) == ["u1"]

    after = await resolve_entitlements(session, "u1")
    assert after.tier == Tier.FREE.value, "a stale cache entry kept granting paid access"


async def test_running_it_again_changes_nothing(session):
    """It runs every night against the same table."""
    await seed(session, **past_due(days_ago=10))

    assert await expire_grace_windows(session) == ["u1"]
    assert await expire_grace_windows(session) == [], "second run must be a no-op"

    audit = (await session.execute(select(SubscriptionAudit))).scalars().all()
    assert len(audit) == 1, "a no-op must not write a second audit row"


async def test_a_new_dunning_cycle_is_reported_again(session, stripe):
    """Idempotency keys on `past_due_since`, which is stamped fresh each cycle,
    so recovering and lapsing again must produce a second row."""
    sub = await seed(session, **past_due(days_ago=40))
    assert await expire_grace_windows(session) == ["u1"]

    # Backdate that first report to when it would really have happened -- 33
    # days ago, seven days after they first lapsed. Without this the test asks
    # whether a cycle that began *before* its own report gets reported again,
    # which cannot happen: the report always lands after the lapse it reports.
    first = (await session.execute(select(SubscriptionAudit))).scalars().one()
    first.created_at = datetime.now(UTC) - timedelta(days=33)
    # They pay, recover, and lapse again nine days ago.
    sub.past_due_since = datetime.now(UTC) - timedelta(days=9)
    await session.commit()

    assert await expire_grace_windows(session) == ["u1"], "a new cycle is a new event"
    audits = (await session.execute(select(SubscriptionAudit).where(
        SubscriptionAudit.reason == "dunning.grace_expired"))).scalars().all()
    assert len(audits) == 2


async def test_several_subscribers_are_handled_in_one_run(session):
    await seed(session, user_id="u1", stripe_customer_id="cus_1", **past_due(days_ago=9))
    await seed(session, user_id="u2", stripe_customer_id="cus_2", **past_due(days_ago=20))
    await seed(session, user_id="u3", stripe_customer_id="cus_3", **past_due(days_ago=1))

    expired = await expire_grace_windows(session)

    assert sorted(expired) == ["u1", "u2"], "u3 is still inside its window"


# ==========================================================================
# the entrypoint the cron actually runs
# ==========================================================================


async def test_expire_grace_main_reports_what_it_revoked(session, run_main):
    from app.jobs import expire_grace as job

    await seed(session, **past_due(days_ago=10))

    _, events = await run_main(job)

    assert "grace.expired" in events, "a nightly job that revokes access must say so"
    assert events["grace.expired"]["count"] == 1
    assert events["grace.expired"]["user_ids"] == ["u1"]
