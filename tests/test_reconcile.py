"""The nightly reconcile job.

It had no test anywhere, so a change that broke it passed every check in the
repo. Reconciliation is the safety net for missed webhooks -- the thing you
rely on precisely when something else has already failed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app import stripe_client
from app.jobs.reconcile import reconcile
from app.models import Subscription, SubscriptionStatus
from app.plans import Tier
from tests.rows import reload, seed


async def test_a_row_that_already_agrees_with_stripe_is_left_alone(session, stripe):
    await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)
    stripe.set_subscription("cus_1", user_id="u1", status="active", price_id="price_pro_m")

    report = await reconcile(session)

    assert report.checked == 1
    assert report.mismatched == 0, "a matching row must not be reported as drift"
    assert report.repaired == 0


async def test_a_missed_webhook_shows_up_as_drift_and_is_repaired(session, stripe):
    """The whole reason this job exists: local says free, Stripe says paid."""
    sub = await seed(session, tier=Tier.FREE.value, status=SubscriptionStatus.FREE.value)
    stripe.set_subscription("cus_1", user_id="u1", status="active", price_id="price_pro_m")

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
    stripe.set_subscription("cus_1", user_id="u1", status="canceled", price_id="price_pro_m")

    report = await reconcile(session)

    assert report.mismatched == 1
    await reload(session, sub)
    assert sub.tier == Tier.FREE.value, "cancelled in Stripe must revoke locally"


async def test_dry_run_reports_without_touching_anything(session, stripe):
    """So you can look at drift before letting a job rewrite rows."""
    sub = await seed(session, tier=Tier.FREE.value)
    stripe.set_subscription("cus_1", user_id="u1", status="active", price_id="price_pro_m")

    report = await reconcile(session, dry_run=True)

    assert report.mismatched == 1
    assert report.repaired == 0
    await reload(session, sub)
    assert sub.tier == Tier.FREE.value, "dry run must not write"


async def test_a_customer_stripe_knows_about_and_we_do_not(session, stripe):
    """A checkout whose webhook never arrived leaves exactly this shape."""
    stripe.set_subscription("cus_ghost", user_id="ghost-user", status="active",
                            price_id="price_plus_m")

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
        stripe.set_subscription(f"cus_{n}", user_id=f"u{n}", status="active",
                                price_id="price_pro_m", subscription_id=f"sub_{n}")
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
        stripe.set_subscription(f"cus_{n}", user_id=f"u{n}", status="active",
                                price_id="price_pro_m", subscription_id=f"sub_{n}")

    async def flaky(customer_id):
        if customer_id == "cus_b":
            raise ConnectionError("stripe timed out for this one customer")
        return await stripe.fetch_current_subscription(customer_id)

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
        stripe.set_subscription(f"cus_{n}", user_id=f"u{n}", status="active",
                                price_id="price_pro_m", subscription_id=f"sub_{n}")

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


# --------------------------------------------------------------------------
# reconcile: subscriptions whose Supabase account has been deleted
# --------------------------------------------------------------------------


class Accounts:
    """An account directory in which the given users have been deleted."""

    def __init__(self, deleted=(), *, can_tell=True):
        self.deleted = set(deleted)
        self.can_tell = can_tell

    async def missing(self, session, user_ids):
        if not self.can_tell:
            return None
        return {u for u in user_ids if u in self.deleted}


class Outbox:
    """A notifier that keeps what it was asked to send, or refuses to send."""

    def __init__(self, *, down=False):
        self.sent: list[tuple[str, str]] = []
        self.down = down

    async def send(self, subject, body):
        if self.down:
            raise ConnectionError("smtp is down")
        self.sent.append((subject, body))


async def test_a_deleted_accounts_subscription_is_reported_not_resynced(session, stripe):
    sub = await seed(session, tier=Tier.FREE.value)
    stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1",
                            cancel_at_period_end=True)

    report = await reconcile(session, accounts=Accounts(deleted={"u1"}), notifier=Outbox())

    [orphan] = report.orphaned
    assert {k: orphan[k] for k in ("user_id", "stripe_customer_id", "stripe_subscription_id",
                                    "status", "cancel_at_period_end")} == {
        "user_id": "u1",
        "stripe_customer_id": "cus_1",
        "stripe_subscription_id": "sub_1",
        "status": "active",
        "cancel_at_period_end": True,
    }
    assert report.mismatched == 0, "an orphan is its own finding, not drift"
    assert report.repaired == 0
    assert (await reload(session, sub)).tier == Tier.FREE.value, "nothing is written for it"


async def test_an_orphan_is_marked_in_stripe_where_someone_would_act_on_it(session, stripe):
    await seed(session)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")

    report = await reconcile(session, accounts=Accounts(deleted={"u1"}), notifier=Outbox())

    marked_at = remote["metadata"]["orphaned_at"]
    assert datetime.fromisoformat(marked_at).tzinfo is not None
    assert report.orphaned[0]["orphaned_at"] == marked_at


async def test_no_row_is_invented_for_a_deleted_account(session, stripe):
    stripe.set_subscription("cus_9", user_id="u9", subscription_id="sub_1")

    report = await reconcile(session, accounts=Accounts(deleted={"u9"}), notifier=Outbox())

    assert [o["user_id"] for o in report.orphaned] == ["u9"]
    assert report.unknown_customers == [], "it is not a customer to sync"
    rows = (await session.execute(select(Subscription))).scalars().all()
    assert rows == []


async def test_the_user_is_found_on_the_subscription_when_the_customer_lacks_it(session, stripe):
    # Checkout stamps user_id on the subscription; the customer may not have it.
    stripe.customers["cus_9"] = {"id": "cus_9", "metadata": {}}
    stripe.set_subscription("cus_9", status="active", price_id="price_pro_m")
    stripe.subscriptions["cus_9"]["metadata"] = {"user_id": "u9"}

    report = await reconcile(session, accounts=Accounts(deleted={"u9"}), notifier=Outbox())

    assert [o["user_id"] for o in report.orphaned] == ["u9"]


async def test_an_ended_subscription_of_a_deleted_account_is_only_counted(session, stripe):
    await seed(session)  # a cancelled subscription mirrors as free
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1",
                                     status="canceled")
    outbox = Outbox()

    report = await reconcile(session, accounts=Accounts(deleted={"u1"}), notifier=outbox)

    assert report.orphaned == [], "nothing can be charged, so nothing to act on"
    assert report.orphaned_closed == 1
    assert outbox.sent == [] and "orphaned_at" not in remote.get("metadata", {})


async def test_a_live_account_in_the_same_run_is_still_repaired(session, stripe):
    await seed(session, tier=Tier.FREE.value)
    await seed(session, user_id="u2", stripe_customer_id="cus_2", tier=Tier.FREE.value)
    stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    stripe.set_subscription("cus_2", user_id="u2", subscription_id="sub_2")

    report = await reconcile(session, accounts=Accounts(deleted={"u1"}), notifier=Outbox())

    assert [o["user_id"] for o in report.orphaned] == ["u1"]
    assert report.repaired == 1
    query = select(Subscription).where(Subscription.user_id == "u2")
    u2 = (await session.execute(query)).scalar_one()
    assert u2.tier == Tier.PRO.value


async def test_cannot_tell_is_never_read_as_deleted(session, stripe):
    await seed(session, tier=Tier.FREE.value)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    remote["metadata"] = {"orphaned_at": "2026-09-01T00:00:00+00:00"}

    report = await reconcile(
        session, accounts=Accounts(deleted={"u1"}, can_tell=False), notifier=Outbox()
    )

    assert report.orphaned == []
    assert report.repaired == 1, "without an answer, reconcile behaves as it always did"
    assert remote["metadata"]["orphaned_at"], "and leaves any earlier mark alone"


async def test_a_mark_on_an_account_that_exists_is_cleared(session, stripe):
    await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    remote["metadata"] = {"user_id": "u1", "orphaned_at": "2026-09-01T00:00:00+00:00",
                          "orphan_notified_at": "2026-09-01T00:00:05+00:00"}

    await reconcile(session, accounts=Accounts(deleted=set()), notifier=Outbox())

    assert remote["metadata"] == {"user_id": "u1"}, "only our two keys are removed"


async def test_orphans_are_reported_on_a_dry_run_without_marking_or_email(session, stripe):
    await seed(session, tier=Tier.FREE.value)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    outbox = Outbox()

    report = await reconcile(session, dry_run=True, accounts=Accounts(deleted={"u1"}),
                             notifier=outbox)

    assert len(report.orphaned) == 1
    assert "orphaned_at" not in remote.get("metadata", {})
    assert outbox.sent == []


# --------------------------------------------------------------------------
# reconcile: telling someone about orphans
# --------------------------------------------------------------------------


async def test_new_orphans_are_emailed_once_in_a_single_message(session, stripe):
    await seed(session)
    await seed(session, user_id="u2", stripe_customer_id="cus_2")
    stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    stripe.set_subscription("cus_2", user_id="u2", subscription_id="sub_2",
                            cancel_at_period_end=True)
    outbox = Outbox()

    report = await reconcile(session, accounts=Accounts(deleted={"u1", "u2"}), notifier=outbox)

    [(subject, body)] = outbox.sent
    assert "2 subscriptions are still charging a deleted account" in subject
    assert "sub_1: active, renews" in body
    assert "sub_2: active, cancels at period end" in body
    links = [line.strip() for line in body.splitlines() if line.strip().startswith("https://")]
    assert links[0] == "https://dashboard.stripe.com/test/subscriptions/sub_1"
    assert sorted(report.notified) == ["sub_1", "sub_2"]
    assert report.unnotified == []


async def test_an_orphan_already_emailed_about_is_not_emailed_again(session, stripe):
    await seed(session)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    outbox = Outbox()
    accounts = Accounts(deleted={"u1"})

    await reconcile(session, accounts=accounts, notifier=outbox)
    assert remote["metadata"]["orphan_notified_at"]
    second = await reconcile(session, accounts=accounts, notifier=outbox)

    assert len(outbox.sent) == 1
    assert len(second.orphaned) == 1, "still reported every night until it ends"
    assert second.notified == [] and second.unnotified == []


async def test_without_email_configured_the_orphan_is_left_unnotified(session, stripe):
    await seed(session)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")

    report = await reconcile(session, accounts=Accounts(deleted={"u1"}), notifier=None)

    assert report.unnotified == ["sub_1"]
    assert "orphaned_at" in remote["metadata"], "the Stripe mark does not depend on email"
    assert "orphan_notified_at" not in remote["metadata"]


async def test_a_failed_send_is_retried_on_the_next_run(session, stripe):
    await seed(session)
    remote = stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    accounts = Accounts(deleted={"u1"})

    first = await reconcile(session, accounts=accounts, notifier=Outbox(down=True))
    assert first.unnotified == ["sub_1"]
    assert "orphan_notified_at" not in remote["metadata"], "never marked for an unsent email"

    outbox = Outbox()
    second = await reconcile(session, accounts=accounts, notifier=outbox)
    assert len(outbox.sent) == 1 and second.notified == ["sub_1"]


async def test_a_webhook_does_not_bring_a_deleted_account_back(session, stripe):
    """Marking an orphan in Stripe fires customer.subscription.updated, and a
    webhook for a customer with no row used to create one."""
    from app.services.subscriptions import sync_subscription_from_stripe

    stripe.set_subscription("cus_9", user_id="u9", subscription_id="sub_1")
    await reconcile(session, accounts=Accounts(deleted={"u9"}), notifier=Outbox())

    # The webhook the mark just fired. No account lookup on this path.
    result = await sync_subscription_from_stripe(session, stripe_customer_id="cus_9")

    assert result is None
    assert (await session.execute(select(Subscription))).scalars().all() == []


async def test_the_default_directory_cannot_tell_on_sqlite(session):
    from app.accounts import SupabaseAccounts

    assert await SupabaseAccounts().missing(session, ["u1"]) is None


# ==========================================================================
# the entrypoint the cron actually runs
# ==========================================================================
#
# `main()` is what `docker run <image> reconcile` executes. It is also where
# the alertable signal lives -- DEPLOY.md tells you to alert on the
# `reconcile.finished` log line and its `mismatched` field, because the Gauge
# is set in a process nothing scrapes. An untested main() means that contract
# is only a comment.


async def test_reconcile_main_reports_drift_on_the_line_the_alert_watches(
    session, stripe, run_main
):
    from app.jobs import reconcile as job
    from app.observability import REGISTRY

    await seed(session, tier=Tier.FREE.value)
    stripe.set_subscription("cus_1", user_id="u1", status="active", price_id="price_pro_m")
    stripe.set_subscription("cus_ghost", user_id="ghost-user", status="active",
                            price_id="price_plus_m", subscription_id="sub_ghost")

    code, events = await run_main(job)

    assert code == 0, "drift that was repaired is a successful run"
    assert "reconcile.finished" in events, (
        "the job must emit reconcile.finished; the alert is built on it"
    )
    fields = events["reconcile.finished"]
    assert fields["checked"] == 2
    assert fields["mismatched"] == 1, "an unknown customer is not drift"
    assert fields["unknown_customers"] == 1
    assert fields["repaired"] == 2, "the drifted row and the unknown customer were both synced"
    assert fields["failed"] == 0

    assert REGISTRY.get_sample_value("reconciliation_drift") == 1, (
        "the gauge must carry the drift count"
    )


async def test_reconcile_main_fails_the_process_when_a_repair_failed(
    session, stripe, monkeypatch, run_main
):
    """A failed repair no longer stops the run -- that is the point -- but the
    scheduler must still see the job fail. Otherwise a customer stuck on the
    wrong tier every night looks exactly like a clean run."""
    from app.jobs import reconcile as job

    await seed(session, tier=Tier.FREE.value)
    stripe.set_subscription("cus_1", user_id="u1", status="active", price_id="price_pro_m")

    async def stripe_down(customer_id):
        raise ConnectionError("stripe is down")

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", stripe_down)

    code, events = await run_main(job)

    assert code == 1
    assert events["reconcile.finished"]["failed"] == 1


async def test_reconcile_main_reports_orphans_on_the_line_the_alert_watches(
    session, stripe, monkeypatch, caplog, run_main
):
    from app.jobs import reconcile as job

    await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)
    stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")
    outbox = Outbox()

    monkeypatch.setattr(job, "SupabaseAccounts", lambda: Accounts(deleted={"u1"}))
    monkeypatch.setattr(job, "email_notifier", lambda: outbox)

    code, events = await run_main(job)

    assert code == 0, "an orphan someone was told about is a clean run"
    assert events["reconcile.finished"]["orphaned"] == 1
    assert events["reconcile.finished"]["notified"] == 1
    assert any("ORPHANED" in r.getMessage() and "sub_1" in r.getMessage()
               for r in caplog.records if r.levelname == "ERROR")


async def test_reconcile_main_fails_when_nobody_could_be_told(
    session, stripe, monkeypatch, run_main
):
    from app.jobs import reconcile as job

    await seed(session, tier=Tier.PRO.value, status=SubscriptionStatus.ACTIVE.value)
    stripe.set_subscription("cus_1", user_id="u1", subscription_id="sub_1")

    monkeypatch.setattr(job, "SupabaseAccounts", lambda: Accounts(deleted={"u1"}))
    monkeypatch.setattr(job, "email_notifier", lambda: None)

    code, _ = await run_main(job)

    assert code == 1, "an orphan nobody was told about must not look like a clean run"
