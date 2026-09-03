"""End to end, over HTTP, the way a customer actually moves through it:
free -> hits a wall -> checks out -> webhook grants -> uses what they paid for.
"""

from __future__ import annotations

from tests.conftest import webhook_event

USER = {"X-User-Id": "alice", "X-User-Email": "alice@example.com"}
ADMIN = {"X-Admin-Key": "test-admin-key", "X-Admin-Actor": "support@example.com"}


async def webhook(client, event_id: str, event_type: str, customer: str):
    return await client.post(
        "/v1/webhooks/stripe",
        content=webhook_event(event_id, event_type, {"customer": customer}),
        headers={"stripe-signature": "t=1,v1=fake", "content-type": "application/json"},
    )


async def test_new_user_is_free_without_any_setup(client, stripe):
    response = await client.get("/v1/entitlements", headers=USER)
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "free"
    assert body["display_name"] == "Free"
    assert any(q["key"] == "messages_per_day" and q["limit"] == 20 for q in body["quotas"])


async def test_unauthenticated_requests_are_rejected(client):
    assert (await client.get("/v1/entitlements")).status_code == 401


async def test_a_locked_model_names_the_tier_that_unlocks_it(client, stripe):
    response = await client.post(
        "/v1/chat", json={"model": "reasoning", "message": "hi"}, headers=USER
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "feature_not_entitled"
    assert body["required_tier"] == "pro"


async def test_full_upgrade_flow(client, stripe):
    # 1. free tier works, within its cap
    chat = await client.post("/v1/chat", json={"model": "small", "message": "hi"}, headers=USER)
    assert chat.status_code == 200
    assert chat.json()["quota"]["limit"] == 20

    # 2. checkout -- grants nothing on its own
    checkout = await client.post(
        "/v1/billing/checkout", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )
    assert checkout.status_code == 200
    assert checkout.json()["checkout_url"].startswith("https://checkout.stripe.test")
    assert (await client.get("/v1/entitlements", headers=USER)).json()["tier"] == "free"

    # 3. the webhook is what grants
    customer = stripe.checkout_sessions[0]["customer_id"]
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")
    granted = await webhook(client, "evt_up", "checkout.session.completed", customer)
    assert granted.status_code == 200

    ents = (await client.get("/v1/entitlements", headers=USER)).json()
    assert ents["tier"] == "pro"
    assert ents["source"] == "subscription"

    # 4. the thing they paid for now works
    upgraded = await client.post(
        "/v1/chat", json={"model": "reasoning", "message": "hi"}, headers=USER
    )
    assert upgraded.status_code == 200
    assert upgraded.json()["quota"]["limit"] == 1500

    # 5. and the portal is where every later change happens
    portal = await client.post("/v1/billing/portal", headers=USER)
    assert portal.status_code == 200
    assert portal.json()["portal_url"].startswith("https://portal.stripe.test")


async def test_a_second_checkout_is_refused_while_subscribed(client, stripe):
    await client.post(
        "/v1/billing/checkout", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )
    customer = stripe.checkout_sessions[0]["customer_id"]
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")
    await webhook(client, "evt_s1", "customer.subscription.created", customer)

    again = await client.post(
        "/v1/billing/checkout", json={"tier": "plus", "interval": "monthly"}, headers=USER
    )
    assert again.status_code == 409, "plan changes belong in the portal, not a second subscription"


async def test_cancellation_returns_the_user_to_free(client, stripe):
    await client.post(
        "/v1/billing/checkout", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )
    customer = stripe.checkout_sessions[0]["customer_id"]
    stripe.set_subscription(customer, status="active", price_id="price_pro_m")
    await webhook(client, "evt_c1", "customer.subscription.created", customer)
    # Read once so the entitlement set is cached; cancelling must invalidate it
    # even though neither tier nor status changes.
    assert (await client.get("/v1/entitlements", headers=USER)).json()["tier"] == "pro"

    # Cancelled at period end: still Pro until the boundary.
    stripe.set_subscription(
        customer, status="active", price_id="price_pro_m", cancel_at_period_end=True
    )
    await webhook(client, "evt_c2", "customer.subscription.updated", customer)
    ents = (await client.get("/v1/entitlements", headers=USER)).json()
    assert ents["tier"] == "pro"
    assert ents["cancel_at_period_end"] is True

    # Boundary reached.
    stripe.subscriptions.pop(customer)
    await webhook(client, "evt_c3", "customer.subscription.deleted", customer)
    assert (await client.get("/v1/entitlements", headers=USER)).json()["tier"] == "free"


async def test_admin_can_comp_and_revoke(client, stripe):
    grant = await client.post(
        "/v1/admin/grants",
        json={"user_id": "alice", "tier": "pro", "reason": "conference giveaway"},
        headers=ADMIN,
    )
    assert grant.status_code == 200
    assert (await client.get("/v1/entitlements", headers=USER)).json()["source"] == "grant"

    revoke = await client.delete(f"/v1/admin/grants/{grant.json()['id']}", headers=ADMIN)
    assert revoke.status_code == 200
    assert (await client.get("/v1/entitlements", headers=USER)).json()["tier"] == "free"


async def test_plans_carry_limits_and_amounts(client, stripe):
    """The pricing page's whole payload: limits from config, amounts from Stripe.

    If either had to be duplicated in the frontend, it would eventually lie.
    """
    plans = (await client.get("/v1/billing/plans")).json()
    assert [p["tier"] for p in plans] == ["free", "plus", "pro"]

    free, plus, pro = plans
    assert free["purchasable"] is False
    assert free["prices"] == {}

    assert plus["purchasable"] is True
    assert plus["prices"]["monthly"]["unit_amount"] == 2000
    assert plus["prices"]["monthly"]["currency"] == "usd"
    assert plus["prices"]["annual"]["unit_amount"] == 20000

    assert any(q["key"] == "messages_per_day" and q["limit"] == 1500 for q in pro["quotas"])
    assert "reasoning" in pro["features"]["models"]


async def test_plans_survive_stripe_being_down(client, stripe, monkeypatch):
    """A pricing page without amounts is bad; one that 500s is worse."""
    from app import stripe_client

    async def unavailable(price_id):
        raise RuntimeError("stripe unreachable")

    monkeypatch.setattr(stripe_client, "retrieve_price", unavailable)

    response = await client.get("/v1/billing/plans")
    assert response.status_code == 200
    plus = next(p for p in response.json() if p["tier"] == "plus")
    assert plus["prices"]["monthly"]["price_id"] == "price_plus_m"
    assert plus["prices"]["monthly"]["unit_amount"] is None


async def test_idempotency_key_tracks_the_request(client, stripe):
    """Same request twice is one session; a different request is not blocked.

    Stripe rejects a key reused with different parameters, so a key that ignores
    them turns "retry with a promo code" into a hard failure for 24 hours.
    """
    base = {"tier": "pro", "interval": "monthly"}
    await client.post("/v1/billing/checkout", json=base, headers=USER)
    await client.post("/v1/billing/checkout", json=base, headers=USER)
    await client.post(
        "/v1/billing/checkout", json={**base, "promo_code": "promo_launch"}, headers=USER
    )

    keys = [s["idempotency_key"] for s in stripe.checkout_sessions]
    assert keys[0] == keys[1], "a double-click must collapse to one session"
    assert keys[2] != keys[0], "adding a promo code is a different request"


async def test_a_stripe_rejection_is_not_a_500(client, stripe, monkeypatch):
    """Stripe's message is the actionable part; a bare 500 sends you to the logs.

    The real case that motivated this: automatic tax enabled on an account with
    no head office address, which rejects every Checkout Session.
    """
    import stripe as stripe_sdk

    from app import stripe_client

    async def reject(**kwargs):
        raise stripe_sdk.InvalidRequestError(
            "You must have a valid head office address to enable automatic tax", None
        )

    monkeypatch.setattr(stripe_client, "create_checkout_session", reject)

    response = await client.post(
        "/v1/billing/checkout", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "stripe_error"
    assert body["type"] == "InvalidRequestError"
    assert "head office address" in body["message"]


async def test_admin_endpoints_need_the_key(client):
    response = await client.post(
        "/v1/admin/grants",
        json={"user_id": "alice", "tier": "pro", "reason": "nope"},
    )
    assert response.status_code == 403
