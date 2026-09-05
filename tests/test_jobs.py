"""The two nightly jobs.

Neither had a test. `reconcile` had none anywhere; `expire_grace` was covered
only in `integration/`, which needs real Stripe and network and is excluded
from CI by `testpaths`. So a change that broke either of them passed every
check in the repo.

That is the wrong pair of things to leave unverified. Reconciliation is the
safety net for missed webhooks -- the thing you rely on precisely when
something else has already failed -- and grace expiry is what actually revokes
access from someone who has stopped paying.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.jobs.expire_grace import expire_grace_windows
from app.jobs.reconcile import reconcile
from app.models import Subscription, SubscriptionAudit, SubscriptionStatus
from app.plans import Tier
from app.services.entitlements import resolve_entitlements


async def seed(session, **kwargs) -> Subscription:
    defaults = {"user_id": "u1", "stripe_customer_id": "cus_1", "tier": Tier.FREE.value,
                "status": SubscriptionStatus.FREE.value}
    sub = Subscription(**{**defaults, **kwargs})
    session.add(sub)
    await session.commit()
    return sub


async def reload(session, sub) -> Subscription:
    await session.refresh(sub)
    return sub


# ==========================================================================
# reconcile
# ==========================================================================


async def test_a_row_that_already_agrees_with_stripe_is_left_alone(session, stripe):
    await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    report = await reconcile(session)

    assert report.checked == 1
    assert report.mismatched == 0, "a matching row must not be reported as drift"
    assert report.repaired == 0


async def test_a_missed_webhook_shows_up_as_drift_and_is_repaired(session, stripe):
    """The whole reason this job exists: local says free, Stripe says paid."""
    sub = await seed(session, tier=Tier.FREE.value, status=SubscriptionStatus.FREE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    report = await reconcile(session)

    assert report.mismatched == 1
    assert report.repaired == 1
    detail = report.details[0]
    assert detail["local"] == {"tier": "free", "status": "free"}
    assert detail["stripe"]["tier"] == "pro"
    assert detail["user_id"] == "u1"

    await reload(session, sub)
    assert sub.tier == Tier.PRO.value, "the repair must actually write the row"
    assert sub.status == SubscriptionStatus.ACTIVE.value


async def test_drift_the_other_way_is_caught_too(session, stripe):
    """Local still granting Pro after Stripe cancelled -- access nobody paid for."""
    sub = await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="canceled", price_id="price_pro_m")

    report = await reconcile(session)

    assert report.mismatched == 1
    await reload(session, sub)
    assert sub.tier == Tier.FREE.value, "cancelled in Stripe must revoke locally"


async def test_dry_run_reports_without_touching_anything(session, stripe):
    """So you can look at drift before letting a job rewrite rows."""
    sub = await seed(session, tier=Tier.FREE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    report = await reconcile(session, dry_run=True)

    assert report.mismatched == 1
    assert report.repaired == 0
    await reload(session, sub)
    assert sub.tier == Tier.FREE.value, "dry run must not write"


async def test_a_customer_stripe_knows_about_and_we_do_not(session, stripe):
    """A checkout whose webhook never arrived leaves exactly this shape."""
    stripe.customers["cus_ghost"] = {"id": "cus_ghost", "metadata": {"user_id": "ghost-user"}}
    stripe.set_subscription("cus_ghost", status="active", price_id="price_plus_m")

    report = await reconcile(session)

    assert report.unknown_customers == ["cus_ghost"]
    assert report.repaired == 1

    found = (await session.execute(
        select(Subscription).where(Subscription.user_id == "ghost-user")
    )).scalar_one_or_none()
    assert found is not None, "sync should have created the row from customer metadata"
    assert found.tier == Tier.PLUS.value


async def test_a_customer_with_no_user_id_metadata_is_not_invented(session, stripe):
    """Nothing ties it to a user, so there is nothing safe to do but report it."""
    stripe.customers["cus_orphan"] = {"id": "cus_orphan", "metadata": {}}
    stripe.set_subscription("cus_orphan", status="active", price_id="price_plus_m")

    report = await reconcile(session)

    assert report.unknown_customers == ["cus_orphan"]
    rows = (await session.execute(select(Subscription))).scalars().all()
    assert rows == [], "must not fabricate a subscription with no user to attach it to"


async def test_every_page_is_walked(session, stripe):
    """`has_more` and the starting_after hand-off are real logic: if paging
    stops early, drift on later pages is silently never found."""
    for n in range(1, 6):
        await seed(session, user_id=f"u{n}", stripe_customer_id=f"cus_{n}")
        stripe.customers[f"cus_{n}"] = {"id": f"cus_{n}", "metadata": {"user_id": f"u{n}"}}
        stripe.set_subscription(f"cus_{n}", status="active", price_id="price_pro_m",
                                subscription_id=f"sub_{n}")
    stripe.page_size = 2   # forces three pages

    report = await reconcile(session)

    assert report.checked == 5, f"paging stopped early: only saw {report.checked} of 5"
    assert report.mismatched == 5


async def test_the_report_carries_what_the_alert_needs(session, stripe):
    await seed(session, tier=Tier.FREE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    payload = (await reconcile(session, dry_run=True)).as_dict()

    assert set(payload) == {"checked", "mismatched", "repaired", "unknown_customers", "details"}
    assert payload["mismatched"] == 1


# ==========================================================================
# expire_grace
# ==========================================================================


def past_due(days_ago: float) -> dict:
    return {
        "tier": Tier.PRO.value,
        "status": SubscriptionStatus.PAST_DUE.value,
        "past_due_since": datetime.now(UTC) - timedelta(days=days_ago),
    }


async def test_a_closed_grace_window_drops_the_subscriber_to_free(session):
    # DUNNING_GRACE_DAYS is 7 in the test environment.
    sub = await seed(session, **past_due(days_ago=8))

    expired = await expire_grace_windows(session)

    assert expired == ["u1"]
    await reload(session, sub)
    assert sub.tier == Tier.FREE.value


async def test_a_grace_window_still_open_is_left_alone(session):
    """Most failed renewals are expired cards; cutting a willing payer off early
    converts them to churn."""
    sub = await seed(session, **past_due(days_ago=2))

    expired = await expire_grace_windows(session)

    assert expired == []
    await reload(session, sub)
    assert sub.tier == Tier.PRO.value, "still inside the window -- keep paid access"


async def test_an_active_subscriber_is_never_touched(session):
    sub = await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)

    assert await expire_grace_windows(session) == []
    await reload(session, sub)
    assert sub.tier == Tier.PRO.value


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
    assert cached["tier"] == Tier.PRO.value, "precondition: cached while still in grace"

    # The window closes.
    sub.past_due_since = datetime.now(UTC) - timedelta(days=10)
    await session.commit()

    assert await expire_grace_windows(session) == ["u1"]

    after = await resolve_entitlements(session, "u1")
    assert after["tier"] == Tier.FREE.value, "a stale cache entry kept granting paid access"


async def test_running_it_again_changes_nothing(session):
    """It runs every night against the same table."""
    await seed(session, **past_due(days_ago=10))

    assert await expire_grace_windows(session) == ["u1"]
    assert await expire_grace_windows(session) == [], "second run must be a no-op"

    audit = (await session.execute(select(SubscriptionAudit))).scalars().all()
    assert len(audit) == 1, "a no-op must not write a second audit row"


async def test_several_subscribers_are_handled_in_one_run(session):
    await seed(session, user_id="u1", stripe_customer_id="cus_1", **past_due(days_ago=9))
    await seed(session, user_id="u2", stripe_customer_id="cus_2", **past_due(days_ago=20))
    await seed(session, user_id="u3", stripe_customer_id="cus_3", **past_due(days_ago=1))

    expired = await expire_grace_windows(session)

    assert sorted(expired) == ["u1", "u2"], "u3 is still inside its window"


# ==========================================================================
# the entrypoints the cron actually runs
# ==========================================================================
#
# `main()` is what `docker run <image> reconcile` executes. It is also where
# the alertable signal lives -- DEPLOY.md tells you to alert on the
# `reconcile.finished` log line and its `mismatched` field, because the Gauge
# is set in a process nothing scrapes. An untested main() means that contract
# is only a comment.


class _FakeSessionmaker:
    """Hands `main()` the test's own session without opening a real engine."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_reconcile_main_reports_drift_on_the_line_the_alert_watches(
    session, stripe, monkeypatch, caplog
):
    from app.jobs import reconcile as job
    from app.observability import reconciliation_drift

    await seed(session, tier=Tier.FREE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    monkeypatch.setattr(job, "get_sessionmaker", lambda: _FakeSessionmaker(session))
    monkeypatch.setattr(job, "dispose_engine", _noop)
    monkeypatch.setattr(job, "configure_logging", lambda **kw: None)

    with caplog.at_level("INFO"):
        await job.main()

    finished = [r for r in caplog.records if getattr(r, "context", {}).get(
        "event") == "reconcile.finished"]
    assert finished, "the job must emit reconcile.finished; the alert is built on it"
    fields = finished[0].context
    assert fields["mismatched"] == 1
    assert fields["checked"] == 1
    assert fields["repaired"] == 1

    assert reconciliation_drift._value.get() == 1, "the gauge must carry the drift count"


async def test_expire_grace_main_reports_what_it_revoked(session, monkeypatch, caplog):
    from app.jobs import expire_grace as job

    await seed(session, **past_due(days_ago=10))

    monkeypatch.setattr(job, "get_sessionmaker", lambda: _FakeSessionmaker(session))
    monkeypatch.setattr(job, "dispose_engine", _noop)
    monkeypatch.setattr(job, "configure_logging", lambda **kw: None)

    with caplog.at_level("INFO"):
        await job.main()

    expired = [r for r in caplog.records if getattr(r, "context", {}).get(
        "event") == "grace.expired"]
    assert expired, "a nightly job that revokes access must say so"
    assert expired[0].context["count"] == 1
    assert expired[0].context["user_ids"] == ["u1"]


async def _noop(*args, **kwargs):
    return None
