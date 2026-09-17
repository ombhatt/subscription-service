"""The portal session, and the deep link into one plan change.

Two things this file holds. The portal only offers the plans its *configuration*
lists -- that lives in Stripe and is built by scripts/configure_portal.py, so it
cannot be asserted here. What can be: that a button naming a tier produces a
session deep-linked to that tier, and that the fallbacks are silent rather than
loud, because a portal that opens on the wrong page still beats an error.
"""

from __future__ import annotations

import json

USER = {"X-User-Id": "alice"}


async def subscribe(client, stripe, price_id: str = "price_plus_m") -> str:
    await client.post(
        "/v1/billing/checkout", json={"tier": "plus", "interval": "monthly"}, headers=USER
    )
    customer = stripe.checkout_sessions[0]["customer_id"]
    stripe.set_subscription(customer, status="active", price_id=price_id)
    await client.post(
        "/v1/webhooks/stripe",
        content=json.dumps(
            {
                "id": "evt_p",
                "type": "customer.subscription.created",
                "data": {"object": {"customer": customer}},
            }
        ),
        headers={"stripe-signature": "t=1,v1=fake"},
    )
    return customer


async def test_a_named_tier_deep_links_to_its_confirmation(client, stripe):
    """A button naming Pro should land on Pro, not on a list to search."""
    await subscribe(client, stripe)

    response = await client.post(
        "/v1/billing/portal", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )

    assert response.status_code == 200
    flow = stripe.portal_sessions[-1]["flow"]
    assert flow["type"] == "subscription_update_confirm"
    confirm = flow["subscription_update_confirm"]
    assert confirm["subscription"] == "sub_test"
    assert confirm["items"][0]["id"] == "si_sub_test", "the item Stripe will move"
    assert confirm["items"][0]["price"] == "price_pro_m", "the price the button named"
    assert confirm["items"][0]["quantity"] == 1


async def test_the_annual_interval_reaches_stripe(client, stripe):
    await subscribe(client, stripe)

    await client.post(
        "/v1/billing/portal", json={"tier": "pro", "interval": "annual"}, headers=USER
    )

    confirm = stripe.portal_sessions[-1]["flow"]["subscription_update_confirm"]
    assert confirm["items"][0]["price"] == "price_pro_y"


async def test_manage_billing_opens_the_portal_itself(client, stripe):
    """No tier named, no deep link: this is the "Manage billing" button."""
    await subscribe(client, stripe)

    await client.post("/v1/billing/portal", headers=USER)

    assert stripe.portal_sessions[-1]["flow"] is None


async def test_the_plan_they_are_already_on_is_not_a_change(client, stripe):
    await subscribe(client, stripe)

    await client.post(
        "/v1/billing/portal", json={"tier": "plus", "interval": "monthly"}, headers=USER
    )

    assert stripe.portal_sessions[-1]["flow"] is None, "confirming a no-op change is not a flow"


async def test_a_sales_led_tier_has_no_price_to_confirm(client, stripe):
    """Enterprise is not purchasable, so the portal cannot move anyone onto it."""
    await subscribe(client, stripe)

    response = await client.post(
        "/v1/billing/portal", json={"tier": "enterprise", "interval": "monthly"}, headers=USER
    )

    assert response.status_code == 200, "still opens the portal"
    assert stripe.portal_sessions[-1]["flow"] is None


async def test_no_billing_account_is_a_404_not_a_broken_portal(client, stripe):
    response = await client.post(
        "/v1/billing/portal", json={"tier": "pro", "interval": "monthly"}, headers=USER
    )
    assert response.status_code == 404
