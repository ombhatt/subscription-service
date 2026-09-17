"""Cancelling from the app.

The rule this file exists to hold: cancelling schedules the end of the paid
period and changes nothing else. A customer who cancels an hour after paying
keeps what they paid for until the boundary, and only the
`customer.subscription.deleted` webhook moves them to free.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.models import SubscriptionAudit

USER = {"X-User-Id": "alice"}


async def subscribe(client, stripe) -> str:
    """Take alice through checkout and land the webhook, as the real flow does."""
    await client.post(
        "/v1/billing/checkout", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )
    customer = stripe.checkout_sessions[0]["customer_id"]
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")
    await client.post(
        "/v1/webhooks/stripe",
        content=json.dumps(
            {
                "id": "evt_sub",
                "type": "customer.subscription.created",
                "data": {"object": {"customer": customer}},
            }
        ),
        headers={"stripe-signature": "t=1,v1=fake"},
    )
    return customer


async def test_cancelling_keeps_the_plan_until_the_period_ends(client, stripe):
    await subscribe(client, stripe)
    # Read first, so the entitlement set is cached: cancelling must invalidate
    # it even though neither tier nor status changes.
    assert (await client.get("/v1/entitlements", headers=USER)).json()["tier"] == "pro"

    response = await client.post("/v1/billing/cancel", headers=USER)

    assert response.status_code == 200
    body = response.json()
    assert body["cancel_at_period_end"] is True
    assert body["tier"] == "pro", "they paid through the end of the period"
    assert body["status"] == "active"

    ents = (await client.get("/v1/entitlements", headers=USER)).json()
    assert ents["tier"] == "pro"
    assert ents["cancel_at_period_end"] is True, "the cached answer must have been dropped"


async def test_the_boundary_is_what_moves_them_to_free(client, stripe):
    customer = await subscribe(client, stripe)
    await client.post("/v1/billing/cancel", headers=USER)

    stripe.subscriptions.pop(customer)
    await client.post(
        "/v1/webhooks/stripe",
        content=json.dumps(
            {
                "id": "evt_gone",
                "type": "customer.subscription.deleted",
                "data": {"object": {"customer": customer}},
            }
        ),
        headers={"stripe-signature": "t=1,v1=fake"},
    )

    assert (await client.get("/v1/entitlements", headers=USER)).json()["tier"] == "free"


async def test_cancelling_twice_asks_stripe_once(client, stripe):
    await subscribe(client, stripe)

    first = await client.post("/v1/billing/cancel", headers=USER)
    second = await client.post("/v1/billing/cancel", headers=USER)

    assert first.status_code == second.status_code == 200
    assert second.json()["cancel_at_period_end"] is True
    assert stripe.cancel_calls == 1, "a second click must not reach the provider again"


async def test_the_cancellation_is_audited(client, stripe, session):
    await subscribe(client, stripe)
    await client.post("/v1/billing/cancel", headers=USER)

    reasons = (
        (
            await session.execute(
                select(SubscriptionAudit.reason).where(SubscriptionAudit.user_id == "alice")
            )
        )
        .scalars()
        .all()
    )
    assert "customer.cancel_at_period_end" in reasons, (
        "support answers 'when did I cancel' from this table"
    )


async def test_a_free_user_has_nothing_to_cancel(client, stripe):
    response = await client.post("/v1/billing/cancel", headers=USER)
    assert response.status_code == 404
    assert "no subscription" in response.json()["detail"]


async def test_cancelling_needs_a_session(client, stripe):
    assert (await client.post("/v1/billing/cancel")).status_code == 401


# --------------------------------------------------------------------------
# changing their mind
# --------------------------------------------------------------------------


async def test_resuming_before_the_boundary_keeps_the_subscription(client, stripe):
    await subscribe(client, stripe)
    await client.post("/v1/billing/cancel", headers=USER)
    assert (await client.get("/v1/entitlements", headers=USER)).json()["cancel_at_period_end"]

    response = await client.post("/v1/billing/resume", headers=USER)

    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is False
    assert response.json()["tier"] == "pro"
    ents = (await client.get("/v1/entitlements", headers=USER)).json()
    assert ents["cancel_at_period_end"] is False, "the cached answer must have been dropped"


async def test_resuming_a_subscription_that_was_never_cancelled_changes_nothing(client, stripe):
    await subscribe(client, stripe)
    calls_before = stripe.cancel_calls

    response = await client.post("/v1/billing/resume", headers=USER)

    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is False
    assert stripe.cancel_calls == calls_before, "nothing to undo, so nothing to ask Stripe"


async def test_there_is_nothing_to_resume_once_the_period_has_ended(client, stripe):
    customer = await subscribe(client, stripe)
    await client.post("/v1/billing/cancel", headers=USER)
    stripe.subscriptions.pop(customer)
    await client.post(
        "/v1/webhooks/stripe",
        content=json.dumps(
            {
                "id": "evt_end",
                "type": "customer.subscription.deleted",
                "data": {"object": {"customer": customer}},
            }
        ),
        headers={"stripe-signature": "t=1,v1=fake"},
    )

    response = await client.post("/v1/billing/resume", headers=USER)

    assert response.status_code == 404
    assert "subscribe again" in response.json()["detail"]
