"""Reading a promotion code's discount off a subscription.

The payload shapes below were copied from real Stripe responses, not invented.
Stripe moved from a single `discount` object to a `discounts` array of ids, and
the amount is not on the discount at all -- it lives on the coupon the discount
points at, which only appears if you expand two levels deep.
"""

from __future__ import annotations

from app.stripe_client import subscription_discount

# What Stripe returns with expand=["data.discounts.source.coupon"].
EXPANDED = {
    "id": "sub_1",
    "discounts": [
        {
            "id": "di_1",
            "object": "discount",
            "end": 1796415914,
            "start": 1788553514,
            "promotion_code": "promo_1",
            "source": {
                "type": "coupon",
                "coupon": {
                    "id": "DzxbTbdd",
                    "object": "coupon",
                    "name": "Launch 25",
                    "percent_off": 25.0,
                    "amount_off": None,
                    "currency": None,
                    "duration": "repeating",
                    "duration_in_months": 3,
                },
            },
        }
    ],
}

# The same subscription fetched without the expand: ids only.
UNEXPANDED = {"id": "sub_1", "discounts": ["di_1"]}

# The older shape, still worth reading so an API version change cannot silently
# drop every customer's discount.
LEGACY = {
    "id": "sub_1",
    "discount": {
        "id": "di_1",
        "end": 1796415914,
        "promotion_code": "promo_1",
        "coupon": {
            "id": "OLDCOUPON",
            "name": "Legacy 10",
            "percent_off": 10.0,
            "amount_off": None,
            "currency": None,
            "duration": "forever",
            "duration_in_months": None,
        },
    },
}


def test_an_expanded_discount_is_flattened():
    d = subscription_discount(EXPANDED)
    assert d["coupon_id"] == "DzxbTbdd"
    assert d["percent_off"] == 25.0
    assert d["duration"] == "repeating"
    assert d["duration_in_months"] == 3
    assert d["promotion_code"] == "promo_1"
    assert d["ends_at"] == 1796415914


def test_an_unexpanded_discount_is_ignored_rather_than_guessed():
    """A bare id says a discount exists but nothing about it. Storing a
    half-populated record would be worse than storing none."""
    assert subscription_discount(UNEXPANDED) is None


def test_the_legacy_singular_shape_still_reads():
    d = subscription_discount(LEGACY)
    assert d["coupon_id"] == "OLDCOUPON"
    assert d["percent_off"] == 10.0
    assert d["duration"] == "forever"
    assert d["ends_at"] == 1796415914


def test_no_discount_is_none():
    assert subscription_discount({"id": "sub_1"}) is None
    assert subscription_discount({"id": "sub_1", "discounts": []}) is None
    assert subscription_discount({"id": "sub_1", "discount": None}) is None


def test_an_amount_off_coupon_reads_its_currency():
    payload = {
        "discounts": [
            {
                "id": "di_2",
                "end": None,
                "source": {
                    "type": "coupon",
                    "coupon": {
                        "id": "TENOFF",
                        "name": "$10 off",
                        "percent_off": None,
                        "amount_off": 1000,
                        "currency": "usd",
                        "duration": "once",
                    },
                },
            }
        ]
    }
    d = subscription_discount(payload)
    assert d["amount_off"] == 1000
    assert d["currency"] == "usd"
    assert d["percent_off"] is None
    assert d["ends_at"] is None, "a `once` coupon has no end date"


async def test_a_discount_is_mirrored_and_then_cleared(client, session, stripe):
    """It has to survive a sync and disappear when the subscription does."""
    from sqlalchemy import select

    from app.models import Subscription
    from tests.conftest import webhook_event

    session.add(Subscription(user_id="u1", stripe_customer_id="cus_1"))
    await session.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}

    sub = stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")
    sub["discounts"] = EXPANDED["discounts"]

    await client.post(
        "/v1/webhooks/stripe",
        content=webhook_event("evt_d1", "customer.subscription.updated", {"customer": "cus_1"}),
        headers={"stripe-signature": "t=1,v1=fake", "content-type": "application/json"},
    )

    row = (await session.execute(select(Subscription))).scalar_one()
    await session.refresh(row)
    await session.commit()
    assert row.discount["percent_off"] == 25.0
    assert row.discount["coupon_id"] == "DzxbTbdd"

    stripe.subscriptions.pop("cus_1")
    await client.post(
        "/v1/webhooks/stripe",
        content=webhook_event("evt_d2", "customer.subscription.deleted", {"customer": "cus_1"}),
        headers={"stripe-signature": "t=1,v1=fake", "content-type": "application/json"},
    )

    row = (await session.execute(select(Subscription))).scalar_one()
    await session.refresh(row)
    await session.commit()
    assert row.discount is None, "a cancelled subscription carries no discount"
