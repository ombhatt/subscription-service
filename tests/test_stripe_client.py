"""Conversion tests against real StripeObjects.

The Stripe fake in conftest hands back plain dicts, which is right for testing
our logic but means nothing there exercises the SDK's actual return types. That
gap shipped a bug: `dict(stripe_object)` raises in stripe-python 8+, so every
read helper failed on the first real API call while the whole suite stayed
green. These tests use the SDK's own object type so that cannot recur.
"""

from __future__ import annotations

from stripe import StripeObject

from app.stripe_client import _as_dict, subscription_period, subscription_price


def as_stripe_object(payload: dict) -> StripeObject:
    return StripeObject.construct_from(payload, "sk_test_fake")


def test_stripe_objects_convert_all_the_way_down():
    obj = as_stripe_object(
        {
            "id": "sub_1",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_1", "metadata": {"tier": "pro"}}}]},
        }
    )
    converted = _as_dict(obj)

    assert isinstance(converted, dict)
    assert isinstance(converted["items"], dict)
    assert isinstance(converted["items"]["data"], list)
    assert isinstance(converted["items"]["data"][0], dict)
    assert isinstance(converted["items"]["data"][0]["price"], dict)
    assert converted["items"]["data"][0]["price"]["metadata"]["tier"] == "pro"


def test_plain_dicts_pass_through_unchanged():
    payload = {"id": "sub_1", "items": {"data": [{"price": {"id": "price_1"}}]}}
    assert _as_dict(payload) == payload


def test_price_and_period_read_from_a_converted_object():
    """The two helpers that actually walk the nested structure."""
    obj = as_stripe_object(
        {
            "id": "sub_1",
            "current_period_start": 1_700_000_000,
            "current_period_end": 1_702_592_000,
            "items": {"data": [{"price": {"id": "price_pro_m"}}]},
        }
    )
    converted = _as_dict(obj)

    assert subscription_price(converted)["id"] == "price_pro_m"
    assert subscription_period(converted) == (1_700_000_000, 1_702_592_000)


def test_period_falls_back_to_the_subscription_item():
    """Recent Stripe API versions moved the period onto the items."""
    converted = _as_dict(
        as_stripe_object(
            {
                "id": "sub_1",
                "items": {
                    "data": [
                        {
                            "current_period_start": 1_700_000_000,
                            "current_period_end": 1_702_592_000,
                            "price": {"id": "price_pro_m"},
                        }
                    ]
                },
            }
        )
    )
    assert subscription_period(converted) == (1_700_000_000, 1_702_592_000)
