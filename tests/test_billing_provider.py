"""The contract between the service and its billing provider.

Two things implement it: app.stripe_client, and the suite's FakeStripe. Nothing
used to check that they agreed -- the fake was patched in by name, so a renamed
argument or a new function passed every test and failed against Stripe.
"""

from __future__ import annotations

import inspect

import pytest

from app import stripe_client
from app.billing_provider import PROVIDER_FUNCTIONS, BillingProvider
from app.models import SubscriptionStatus
from app.stripe_client import parse_subscription
from tests.fakes.stripe import FakeStripe


def contract(name: str) -> inspect.Signature:
    signature = inspect.signature(getattr(BillingProvider, name))
    return signature.replace(parameters=list(signature.parameters.values())[1:])  # drop self


def shape(signature: inspect.Signature) -> list[tuple[str, object, object]]:
    """Names, kinds and defaults: what a caller depends on. Annotations are
    left out, because the fake does not repeat them."""
    return [(p.name, p.kind, p.default) for p in signature.parameters.values()]


@pytest.mark.parametrize("name", PROVIDER_FUNCTIONS)
def test_the_stripe_adapter_matches_the_contract(name):
    real = getattr(stripe_client, name)
    assert shape(inspect.signature(real)) == shape(contract(name))
    assert inspect.iscoroutinefunction(real) == inspect.iscoroutinefunction(
        getattr(BillingProvider, name)
    )


@pytest.mark.parametrize("name", PROVIDER_FUNCTIONS)
def test_the_fake_matches_the_contract(name):
    fake = getattr(FakeStripe(), name)
    assert shape(inspect.signature(fake)) == shape(contract(name))
    assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(
        getattr(BillingProvider, name)
    )


def test_every_public_adapter_call_is_in_the_contract():
    """A function added to the adapter but not the contract would not be
    replaced by the fixture, and would reach Stripe from the test suite."""
    public = {
        name
        for name, value in vars(stripe_client).items()
        if inspect.iscoroutinefunction(value) and not name.startswith("_")
    }
    assert public <= set(PROVIDER_FUNCTIONS)


def raw(status: str, **extra) -> dict:
    return {"id": "sub_1", "customer": "cus_1", "status": status, **extra}


@pytest.mark.parametrize(
    "stripe_status, ours, access, ended",
    [
        ("active", SubscriptionStatus.ACTIVE, True, False),
        ("trialing", SubscriptionStatus.TRIALING, True, False),
        ("past_due", SubscriptionStatus.PAST_DUE, True, False),
        ("unpaid", SubscriptionStatus.PAST_DUE, True, False),
        ("paused", SubscriptionStatus.PAUSED, False, False),
        ("incomplete", SubscriptionStatus.INCOMPLETE, False, False),
        ("incomplete_expired", SubscriptionStatus.FREE, False, True),
        ("canceled", SubscriptionStatus.FREE, False, True),
        # A status Stripe adds later grants nothing, but is not "ended" either:
        # it may still take money, so an orphan in it is still reported.
        ("some_new_status", SubscriptionStatus.FREE, False, False),
    ],
)
def test_stripe_statuses_are_translated_once(stripe_status, ours, access, ended):
    remote = parse_subscription(raw(stripe_status))
    assert remote.status is ours
    assert remote.grants_access is access
    assert remote.ended is ended
    assert remote.provider_status == stripe_status


def test_references_may_arrive_expanded_or_as_ids():
    expanded = parse_subscription(
        raw(
            "active",
            customer={"id": "cus_1", "object": "customer"},
            items={"data": [{"id": "si_1", "price": "price_1"}]},
        )
    )
    assert expanded.customer_id == "cus_1"
    assert expanded.price.id == "price_1"
    assert expanded.price.interval is None
    assert expanded.item_ids == ("si_1",)


def test_a_price_carries_its_interval_and_tier_tag():
    remote = parse_subscription(
        raw(
            "active",
            items={
                "data": [
                    {
                        "id": "si_1",
                        "price": {
                            "id": "price_old",
                            "recurring": {"interval": "year"},
                            "metadata": {"tier": "pro"},
                        },
                    }
                ]
            },
        )
    )
    assert remote.price.interval.value == "annual"
    assert remote.price.tier == "pro"
