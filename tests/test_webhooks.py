"""The tests that matter.

Stripe delivers at least once and out of order. Every case here is a delivery
pattern that breaks naive webhook code, asserted against the state it should
converge on.
"""

from __future__ import annotations

from sqlalchemy import select

from app import stripe_client
from app.models import ProcessedEvent, Subscription, SubscriptionAudit
from tests.conftest import webhook_event


async def post(client, payload: str):
    return await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": "t=1,v1=fake", "content-type": "application/json"},
    )


async def seed_customer(session, stripe, user_id="u1", customer_id="cus_1") -> str:
    session.add(Subscription(user_id=user_id, stripe_customer_id=customer_id))
    await session.commit()
    stripe.customers[customer_id] = {"id": customer_id, "metadata": {"user_id": user_id}}
    return customer_id


async def read_sub(session, user_id="u1") -> Subscription:
    """Read the row and close the transaction.

    SQLite holds a shared lock for the life of a read transaction, so leaving
    one open here would block the next request's write.
    """
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    sub = result.scalar_one()
    await session.refresh(sub)
    await session.commit()
    return sub


async def test_subscription_created_grants_the_tier(client, session, stripe):
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")

    response = await post(
        client, webhook_event("evt_1", "customer.subscription.created", {"customer": customer})
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    sub = await read_sub(session)
    assert sub.tier == "pro"
    assert sub.status == "active"
    assert sub.stripe_subscription_id == "sub_test"


async def test_duplicate_delivery_is_a_no_op(client, session, stripe):
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_plus_m")
    event = webhook_event("evt_dup", "customer.subscription.updated", {"customer": customer})

    first = await post(client, event)
    second = await post(client, event)

    assert first.json()["status"] == "ok"
    assert second.json()["status"] == "duplicate"

    rows = (await session.execute(select(SubscriptionAudit))).scalars().all()
    await session.commit()
    # One transition to plus, not two.
    assert len([row for row in rows if row.to_tier == "plus"]) == 1


async def test_out_of_order_delivery_converges(client, session, stripe):
    """The cancellation arrives before the creation.

    Because the handler re-reads Stripe rather than applying the payload as a
    delta, arrival order cannot change where we end up.
    """
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")

    await post(
        client, webhook_event("evt_late", "customer.subscription.deleted", {"customer": customer})
    )
    await post(
        client, webhook_event("evt_early", "customer.subscription.created", {"customer": customer})
    )

    sub = await read_sub(session)
    assert sub.tier == "pro"
    assert sub.status == "active"


async def test_cancellation_drops_to_free(client, session, stripe):
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")
    await post(
        client, webhook_event("evt_a", "customer.subscription.created", {"customer": customer})
    )

    stripe.subscriptions.pop(customer)
    await post(
        client, webhook_event("evt_b", "customer.subscription.deleted", {"customer": customer})
    )

    sub = await read_sub(session)
    assert sub.tier == "free"
    assert sub.status == "free"
    assert sub.stripe_subscription_id is None


async def test_a_cancelled_subscription_still_listed_is_treated_as_gone(client, session, stripe):
    """Stripe keeps returning a cancelled subscription rather than dropping it.

    Found by the test-clock suite: this path and the "no subscription at all"
    path must produce identical local state, or whether a user looks subscribed
    depends on how long the provider keeps the corpse around.
    """
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")
    created = webhook_event("evt_h1", "customer.subscription.created", {"customer": customer})
    await post(client, created)

    stripe.set_subscription(customer, status="canceled", price_id="price_pro_m")
    deleted = webhook_event("evt_h2", "customer.subscription.deleted", {"customer": customer})
    await post(client, deleted)

    sub = await read_sub(session)
    assert sub.tier == "free"
    assert sub.status == "free"
    assert sub.stripe_subscription_id is None, "a dead subscription id must not linger"
    assert sub.current_period_end is None
    assert sub.cancel_at_period_end is False


async def test_failed_payment_starts_the_grace_window_once(client, session, stripe):
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="past_due", price_id="price_plus_m")

    await post(client, webhook_event("evt_f1", "invoice.payment_failed", {"customer": customer}))
    sub = await read_sub(session)
    first_seen = sub.past_due_since
    assert first_seen is not None
    assert sub.tier == "plus", "grace keeps access while the card is retried"

    # A second retry fails. The window must not restart.
    await post(client, webhook_event("evt_f2", "invoice.payment_failed", {"customer": customer}))
    sub = await read_sub(session)
    assert sub.past_due_since == first_seen


async def test_recovery_clears_past_due(client, session, stripe):
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="past_due", price_id="price_plus_m")
    await post(client, webhook_event("evt_g1", "invoice.payment_failed", {"customer": customer}))

    stripe.set_subscription(customer, status="active", price_id="price_plus_m")
    await post(client, webhook_event("evt_g2", "invoice.paid", {"customer": customer}))

    sub = await read_sub(session)
    assert sub.status == "active"
    assert sub.past_due_since is None


async def test_grandfathered_price_resolves_through_metadata(client, session, stripe):
    """A subscriber on a price we have since retired keeps their tier."""
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(
        customer,
        status="active",
        price_id="price_old_pro_2024",
        price_metadata={"tier": "pro"},
    )
    await post(
        client, webhook_event("evt_old", "customer.subscription.updated", {"customer": customer})
    )

    sub = await read_sub(session)
    assert sub.tier == "pro"


async def test_unresolvable_price_grants_nothing(client, session, stripe):
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_mystery")
    await post(
        client, webhook_event("evt_x", "customer.subscription.updated", {"customer": customer})
    )

    sub = await read_sub(session)
    assert sub.tier == "free", "an unknown price must never be guessed upward"
    assert sub.status == "active"


async def test_unhandled_event_type_is_acknowledged(client, session, stripe):
    customer = await seed_customer(session, stripe)
    response = await post(
        client, webhook_event("evt_noise", "customer.created", {"customer": customer})
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


async def test_missing_signature_is_rejected(client):
    response = await client.post("/v1/webhooks/stripe", content="{}")
    assert response.status_code == 400


async def test_a_failed_event_can_be_retried(client, session, stripe, monkeypatch):
    """A 500 must leave the event re-processable -- Stripe will send it again."""
    customer = await seed_customer(session, stripe)
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")

    async def boom(customer_id):
        raise RuntimeError("stripe unreachable")

    monkeypatch.setattr(stripe_client, "fetch_current_subscription", boom)

    event = webhook_event("evt_retry", "customer.subscription.updated", {"customer": customer})
    assert (await post(client, event)).status_code == 500

    row = await session.get(ProcessedEvent, "evt_retry")
    await session.refresh(row)
    assert row.status == "failed"
    await session.commit()

    monkeypatch.setattr(
        stripe_client, "fetch_current_subscription", stripe.fetch_current_subscription
    )
    retried = await post(client, event)
    assert retried.status_code == 200
    assert retried.json()["status"] == "ok"

    sub = await read_sub(session)
    assert sub.tier == "pro"
