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

from app import stripe_client
from app.config import get_settings
from app.jobs.expire_grace import expire_grace_windows
from app.jobs.reconcile import reconcile
from app.models import Subscription, SubscriptionAudit, SubscriptionStatus
from app.plans import Tier
from app.services.entitlements import _ttl_for, resolve_entitlements


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


async def test_one_customer_failing_does_not_undo_the_others(
    session, sessionmaker_, stripe, monkeypatch
):
    """Stripe errors for one customer mid-run. Every other repair must still land.

    The job used to be one transaction: a single exception rolled back every
    repair made before it, and nothing after it ran at all -- so one bad
    customer meant nobody's drift was fixed that night.
    """
    for n in ("a", "b", "c"):
        await seed(session, user_id=f"u{n}", stripe_customer_id=f"cus_{n}")
        stripe.customers[f"cus_{n}"] = {"id": f"cus_{n}", "metadata": {"user_id": f"u{n}"}}
        stripe.set_subscription(f"cus_{n}", status="active", price_id="price_pro_m",
                                subscription_id=f"sub_{n}")

    async def flaky(customer_id):
        if customer_id == "cus_b":
            raise ConnectionError("stripe timed out for this one customer")
        return stripe.subscriptions.get(customer_id)

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", flaky)

    report = await reconcile(session)

    assert report.mismatched == 3
    assert report.repaired == 2
    assert report.failed == ["cus_b"]
    async with sessionmaker_() as fresh:
        rows = await fresh.execute(select(Subscription.stripe_customer_id, Subscription.tier))
        assert dict(rows.all()) == {"cus_a": "pro", "cus_b": "free", "cus_c": "pro"}


async def test_each_repair_commits_before_the_next_customer_is_fetched(
    session, stripe, monkeypatch
):
    """The commit is what releases the row lock.

    `sync` takes `SELECT ... FOR UPDATE` and holds it until the transaction
    ends. With one transaction for the whole run, every repaired row stayed
    locked across every later Stripe call, and webhooks for those customers
    waited on it until their lock timeout. SQLite has no row locks to observe,
    so this asserts the order that releases them.
    """
    from app.jobs import reconcile as job

    for n in ("a", "b"):
        await seed(session, user_id=f"u{n}", stripe_customer_id=f"cus_{n}")
        stripe.customers[f"cus_{n}"] = {"id": f"cus_{n}", "metadata": {"user_id": f"u{n}"}}
        stripe.set_subscription(f"cus_{n}", status="active", price_id="price_pro_m",
                                subscription_id=f"sub_{n}")

    events: list[str] = []
    real_fetch = stripe_client.fetch_current_subscription
    real_commit = job.commit_and_invalidate

    async def watched_fetch(customer_id):
        events.append(f"fetch {customer_id}")
        return await real_fetch(customer_id)

    async def watched_commit(session_):
        events.append("commit")
        await real_commit(session_)

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", watched_fetch)
    monkeypatch.setattr(job, "commit_and_invalidate", watched_commit)

    await reconcile(session)

    assert events == ["fetch cus_a", "commit", "fetch cus_b", "commit"]


async def test_the_report_carries_what_the_alert_needs(session, stripe):
    await seed(session, tier=Tier.FREE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    payload = (await reconcile(session, dry_run=True)).as_dict()

    assert set(payload) == {
        "checked", "mismatched", "repaired", "failed", "unknown_customers", "details"
    }
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
    assert (await resolve_entitlements(session, "u1"))["tier"] == Tier.FREE.value


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
        assert await job.main() == 0, "drift that was repaired is a successful run"

    finished = [r for r in caplog.records if getattr(r, "context", {}).get(
        "event") == "reconcile.finished"]
    assert finished, "the job must emit reconcile.finished; the alert is built on it"
    fields = finished[0].context
    assert fields["mismatched"] == 1
    assert fields["checked"] == 1
    assert fields["repaired"] == 1

    assert reconciliation_drift._value.get() == 1, "the gauge must carry the drift count"


async def test_reconcile_main_fails_the_process_when_a_repair_failed(
    session, stripe, monkeypatch, caplog
):
    """A failed repair no longer stops the run -- that is the point -- but the
    scheduler must still see the job fail. Otherwise a customer stuck on the
    wrong tier every night looks exactly like a clean run."""
    from app.jobs import reconcile as job

    await seed(session, tier=Tier.FREE.value)
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    async def stripe_down(customer_id):
        raise ConnectionError("stripe is down")

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", stripe_down)
    monkeypatch.setattr(job, "get_sessionmaker", lambda: _FakeSessionmaker(session))
    monkeypatch.setattr(job, "dispose_engine", _noop)
    monkeypatch.setattr(job, "configure_logging", lambda **kw: None)

    with caplog.at_level("INFO"):
        assert await job.main() == 1

    finished = [r for r in caplog.records if getattr(r, "context", {}).get(
        "event") == "reconcile.finished"]
    assert finished and finished[0].context["failed"] == 1


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


# ==========================================================================
# the two jobs, together
# ==========================================================================
#
# They were only ever tested apart, and apart they were both correct. Together
# they undid each other every night: expire_grace wrote `tier = free`, which
# is the Stripe mirror and not its to write, and reconcile -- correctly
# comparing that mirror against Stripe -- called it drift and wrote it back.
# Two audit rows per subscriber per night, and `reconciliation_drift` pinned
# above zero, so the alarm for missed webhooks could never fall silent.


async def test_the_two_nightly_jobs_do_not_undo_each_other(session, stripe):
    """Three nights. The stored mirror holds, the served tier holds, drift stays
    at zero, and exactly one audit row is written."""
    await seed(session, **past_due(days_ago=10))
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="past_due", price_id="price_pro_m")

    for night in range(3):
        await expire_grace_windows(session)
        report = await reconcile(session)
        assert report.mismatched == 0, f"night {night + 1}: the drift alert would be firing"

    row = (await session.execute(
        select(Subscription).where(Subscription.user_id == "u1"))).scalar_one()
    await session.refresh(row)
    assert row.tier == Tier.PRO.value, "the mirror is Stripe's; nothing else may write it"
    assert (await resolve_entitlements(session, "u1"))["tier"] == Tier.FREE.value

    audits = (await session.execute(select(SubscriptionAudit).where(
        SubscriptionAudit.reason == "dunning.grace_expired"))).scalars().all()
    assert len(audits) == 1, f"one event, one row -- got {len(audits)} over three nights"


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


# ==========================================================================
# the cache cannot outlive the grace boundary
# ==========================================================================


def _payload(seconds_left: float | None) -> dict:
    if seconds_left is None:
        return {"tier": "pro", "grace_ends_at": None}
    when = datetime.now(UTC) + timedelta(seconds=seconds_left)
    return {"tier": "pro", "grace_ends_at": when.isoformat()}


def test_a_cached_entitlement_never_outlives_the_grace_window():
    """Crossing the boundary is the passage of time, not a write, so no
    invalidation can reach the cached entry. The TTL has to do it."""
    configured = get_settings().entitlement_cache_ttl
    assert _ttl_for(_payload(None)) == configured, "no window, no cap"
    assert _ttl_for(_payload(9999)) == configured, "distant window, no cap"
    for remaining in (5, 30, 59):
        assert _ttl_for(_payload(remaining)) <= remaining, (
            f"a {remaining}s window cached for longer would serve paid access past it"
        )


def test_a_sub_second_window_is_not_cached_at_all():
    """A TTL of 1 outlives the boundary; a TTL of 0 means *never expire* to some
    backends. Declining to cache is the only answer wrong in neither direction."""
    assert _ttl_for(_payload(0.5)) == 0


def test_a_boundary_already_passed_needs_no_cap():
    """Past the window the answer is stable again -- free, and staying free."""
    assert _ttl_for(_payload(-100)) == get_settings().entitlement_cache_ttl
