"""The per-process cap on concurrent syncs.

A sync holds a database connection across its Stripe calls, so when Stripe is
slow it is syncs that fill the pool, and every API request's entitlement read
queues behind them. The cap keeps that from happening: past it, a sync waits a
moment for a slot and then fails -- before it touches the database or Stripe --
and the webhook that asked for it returns 500 for Stripe to redeliver.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import stripe_client
from app.config import get_settings
from app.models import ProcessedEvent, Subscription
from app.services import subscriptions
from app.services.entitlements import commit_and_invalidate
from app.services.subscriptions import SyncCapacityExceeded, sync_subscription_from_stripe
from tests.fakes.stripe import deliver, webhook_event
from tests.metrics import counted


@pytest.fixture
def one_slot(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_concurrent_syncs", 1)
    monkeypatch.setattr(get_settings(), "sync_slot_timeout_seconds", 0.2)


@pytest.fixture
def slow_stripe(stripe, monkeypatch):
    """Stripe answers every customer at once, except `cus_slow`, which waits
    until the test releases it. Records who was asked."""
    asked: list[str] = []
    asking = asyncio.Event()
    release = asyncio.Event()

    async def fetch(customer_id):
        asked.append(customer_id)
        if customer_id == "cus_slow":
            asking.set()
            await release.wait()
        return await stripe.fetch_current_subscription(customer_id)

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", fetch)
    for customer in ("cus_slow", "cus_other"):
        stripe.set_subscription(customer, subscription_id=f"sub_{customer}")
    return asked, asking, release


async def seed(sessionmaker_):
    async with sessionmaker_() as s:
        s.add_all(
            [
                Subscription(user_id="u_slow", stripe_customer_id="cus_slow"),
                Subscription(user_id="u_other", stripe_customer_id="cus_other"),
            ]
        )
        await s.commit()


async def sync_and_commit(sessionmaker_, customer_id):
    async with sessionmaker_() as s:
        sub = await sync_subscription_from_stripe(s, stripe_customer_id=customer_id)
        await commit_and_invalidate(s)
        return sub


async def test_a_sync_past_the_cap_waits_then_fails_without_asking_stripe(
    sessionmaker_, one_slot, slow_stripe
):
    asked, asking, release = slow_stripe
    await seed(sessionmaker_)
    rejected = counted("sync_rejections_total")

    holder = asyncio.create_task(sync_and_commit(sessionmaker_, "cus_slow"))
    await asyncio.wait_for(asking.wait(), timeout=5)

    started = time.monotonic()
    with pytest.raises(SyncCapacityExceeded):
        await asyncio.wait_for(sync_and_commit(sessionmaker_, "cus_other"), timeout=5)
    waited = time.monotonic() - started

    assert 0.15 <= waited < 2, waited
    assert asked == ["cus_slow"], "refused before asking Stripe"
    assert rejected() == 1

    release.set()
    assert (await holder).tier == "pro"
    # The slot came back: the refused customer now syncs.
    assert (await sync_and_commit(sessionmaker_, "cus_other")).tier == "pro"


async def test_a_sync_waits_for_a_slot_that_frees_in_time(
    sessionmaker_, one_slot, slow_stripe, monkeypatch
):
    asked, asking, release = slow_stripe
    monkeypatch.setattr(get_settings(), "sync_slot_timeout_seconds", 5)
    await seed(sessionmaker_)
    rejected = counted("sync_rejections_total")

    holder = asyncio.create_task(sync_and_commit(sessionmaker_, "cus_slow"))
    await asyncio.wait_for(asking.wait(), timeout=5)
    waiter = asyncio.create_task(sync_and_commit(sessionmaker_, "cus_other"))
    await asyncio.sleep(0.1)
    assert asked == ["cus_slow"], "the second sync is waiting, not running"

    release.set()
    first, second = await asyncio.wait_for(asyncio.gather(holder, waiter), timeout=5)

    assert (first.tier, second.tier) == ("pro", "pro")
    assert asked == ["cus_slow", "cus_other"]
    assert rejected() == 0


async def test_a_sync_that_fails_gives_its_slot_back(sessionmaker_, one_slot, stripe, monkeypatch):
    await seed(sessionmaker_)
    stripe.set_subscription("cus_other", subscription_id="sub_other")

    async def unreachable(customer_id):
        raise RuntimeError("stripe unreachable")

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", unreachable)
    with pytest.raises(RuntimeError, match="unreachable"):
        await sync_and_commit(sessionmaker_, "cus_slow")

    monkeypatch.setattr(
        stripe_client, "fetch_current_subscription", stripe.fetch_current_subscription
    )
    assert (await sync_and_commit(sessionmaker_, "cus_other")).tier == "pro"


async def test_a_webhook_refused_a_slot_fails_so_stripe_redelivers_it(
    client, session, stripe, one_slot
):
    """Every slot busy: the webhook answers 500 and the event stays retryable,
    exactly as for any other failure -- a delay, not a lost event."""
    session.add(Subscription(user_id="u1", stripe_customer_id="cus_1"))
    await session.commit()
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")
    event = webhook_event("evt_busy", "customer.subscription.updated", {"customer": "cus_1"})

    slots = subscriptions._sync_slots()
    await slots.acquire()
    try:
        assert (await deliver(client, event)).status_code == 500
    finally:
        slots.release()

    row = await session.get(ProcessedEvent, "evt_busy")
    await session.refresh(row)
    assert row.status == "failed"
    assert "SyncCapacityExceeded" in row.error
    await session.commit()

    assert (await deliver(client, event)).status_code == 200
