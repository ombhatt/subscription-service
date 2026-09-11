"""Invalidation must happen after the commit, not before it.

The defect this guards was measured against real Postgres and real Redis, in a
single process with one event loop -- `WEB_CONCURRENCY=1` does not protect you,
because the `await` on the Stripe call inside `sync` is a yield point:

    [3.79s] writer: sync() done -- flushed and invalidated, NOT committed
    [4.37s] reader: resolved tier='free' and cached it for 60s
    [5.89s] writer: COMMIT
            database says pro, the API serves free

A customer paid, the webhook landed, and they sat on free for the next minute --
in the exact window the "access is granted on the webhook, never on the
redirect" design exists to protect.

These tests interleave deterministically rather than racing on timing. The
interleaving is the point; the wall clock is not.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.jobs.reconcile import reconcile
from app.models import Subscription
from app.observability import entitlement_invalidations
from app.services.entitlements import (
    commit_and_invalidate,
    mark_entitlements_stale,
    resolve_entitlements,
)
from app.services.subscriptions import sync_subscription_from_stripe

USER = "alice"
ADMIN = {"X-Admin-Key": "test-admin-key"}


def counter(outcome: str) -> float:
    return entitlement_invalidations.labels(outcome=outcome)._value.get()


async def test_a_reader_between_the_write_and_the_commit_does_not_win(
    sessionmaker_, stripe
):
    """The interleaving that used to serve a paying customer their old tier.

    Two sessions, as two concurrent requests would be. The reader runs while
    the writer's transaction is open, so it sees the pre-commit row and caches
    it -- that part is unavoidable and fine. What matters is what happens next:
    the commit must drop that entry, and it can only do so if the invalidation
    comes after it.
    """
    async with sessionmaker_() as setup:
        setup.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        await setup.commit()

    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": USER}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    writer = sessionmaker_()
    reader = sessionmaker_()
    try:
        # The customer has paid; the webhook is being handled.
        await sync_subscription_from_stripe(writer, stripe_customer_id="cus_1")

        # A concurrent request lands mid-transaction and caches what it sees.
        stale = await resolve_entitlements(reader, USER)
        assert stale["tier"] == "free", "precondition: the write is not committed yet"

        # The handler finishes.
        await commit_and_invalidate(writer)

        # The cached answer must be gone, not merely outdated.
        fresh = await resolve_entitlements(reader, USER)
        assert fresh["tier"] == "pro", (
            "a paying customer is still being served their old tier -- the "
            "invalidation ran before the commit and a reader repopulated it"
        )
    finally:
        await writer.close()
        await reader.close()


async def test_sync_does_not_touch_the_cache_itself(sessionmaker_, stripe):
    """It cannot: it does not commit. Its caller does, a frame up."""
    async with sessionmaker_() as setup:
        setup.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        await setup.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": USER}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    async with sessionmaker_() as writer:
        await resolve_entitlements(writer, USER)  # populate
        await sync_subscription_from_stripe(writer, stripe_customer_id="cus_1")
        # Marked, not deleted.
        assert writer.sync_session.info.get("entitlements_to_invalidate") == {USER}


async def test_a_rolled_back_write_invalidates_nothing(sessionmaker_, stripe):
    """`session.info` is not transactional, so marks outlive a rollback on
    their own. The webhook failure path rolls back and then commits an error
    record -- without discarding them it would invalidate for a write that
    never happened."""
    async with sessionmaker_() as setup:
        setup.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        await setup.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": USER}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    async with sessionmaker_() as writer:
        await sync_subscription_from_stripe(writer, stripe_customer_id="cus_1")
        assert writer.sync_session.info.get("entitlements_to_invalidate")
        await writer.rollback()
        assert not writer.sync_session.info.get("entitlements_to_invalidate"), (
            "a rolled-back write changes nothing and so invalidates nothing"
        )


@pytest.mark.allow_undrained
async def test_a_plain_commit_under_a_marking_write_is_reported(
    sessionmaker_, stripe, caplog
):
    """The detector. Someone adds a marking write beneath an existing
    `session.commit()` and nothing else would say so."""
    async with sessionmaker_() as setup:
        setup.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        await setup.commit()

    before = counter("undrained")
    async with sessionmaker_() as s:
        mark_entitlements_stale(s, USER)
        with caplog.at_level(logging.ERROR):
            await s.commit()  # the wrong call, deliberately

    assert counter("undrained") == before + 1
    assert any("commit_and_invalidate" in r.message for r in caplog.records), (
        "the detector must name the call the author should have used"
    )


async def test_a_successful_invalidation_is_counted(sessionmaker_, stripe):
    before = counter("ok")
    async with sessionmaker_() as s:
        s.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        mark_entitlements_stale(s, USER)
        await commit_and_invalidate(s)
    assert counter("ok") == before + 1


async def test_a_cache_failure_after_commit_does_not_lose_the_write(
    sessionmaker_, monkeypatch
):
    """Redis is down. The row is durably written; raising here would invite a
    retry of a completed operation, and for a webhook it would make Stripe
    redeliver an event we already processed."""
    from app.services import entitlements as ents

    async def boom(user_id):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(ents, "invalidate_entitlements", boom)
    before = counter("failed")

    async with sessionmaker_() as s:
        s.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        mark_entitlements_stale(s, USER)
        await ents.commit_and_invalidate(s)   # must not raise

    assert counter("failed") == before + 1
    async with sessionmaker_() as check:
        row = (await check.execute(
            select(Subscription).where(Subscription.user_id == USER))).scalar_one_or_none()
        assert row is not None, "the write must survive a cache failure"


# --------------------------------------------------------------------------
# The two write paths #42 missed. Both are repairs for a webhook that never
# arrived, so both run exactly when a customer is already on the wrong tier --
# and both used to fix the row while the API went on serving the old answer.
# --------------------------------------------------------------------------


async def _paid_but_cached_as_free(sessionmaker_, stripe) -> None:
    """A customer paid, the webhook was lost, and their free tier is cached."""
    async with sessionmaker_() as setup:
        setup.add(Subscription(user_id=USER, stripe_customer_id="cus_1"))
        await setup.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": USER}}

    async with sessionmaker_() as reader:
        assert (await resolve_entitlements(reader, USER))["tier"] == "free"

    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")


async def test_an_admin_resync_invalidates_after_its_commit(client, sessionmaker_, stripe):
    """Support's fix for a missed webhook. The response said "resynced" and
    `tier: pro` while the customer went on seeing free until the entry expired
    -- so the fix looked like it had not worked, at exactly the moment someone
    was watching."""
    await _paid_but_cached_as_free(sessionmaker_, stripe)

    response = await client.post(f"/v1/admin/users/{USER}/resync", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["tier"] == "pro"

    async with sessionmaker_() as reader:
        assert (await resolve_entitlements(reader, USER))["tier"] == "pro", (
            "the resync repaired the row but the API still serves the cached tier"
        )


async def test_a_reconcile_repair_invalidates_after_its_commit(sessionmaker_, stripe):
    """The nightly safety net for missed webhooks, with the same gap."""
    await _paid_but_cached_as_free(sessionmaker_, stripe)

    async with sessionmaker_() as job:
        report = await reconcile(job)
    assert report.repaired == 1

    async with sessionmaker_() as reader:
        assert (await resolve_entitlements(reader, USER))["tier"] == "pro", (
            "reconcile repaired the row but the API still serves the cached tier"
        )
