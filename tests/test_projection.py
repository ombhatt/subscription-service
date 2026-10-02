"""Sync and reconcile share one projection, so they cannot disagree.

When each carried its own copy of the rules, a case the two answered
differently was reported as drift and "repaired" by the sync every night --
the same flip-flop PR #41 fixed for the grace window. The matrix below is every
Stripe status against every kind of price: whatever sync writes, reconcile
must find nothing to repair.
"""

from __future__ import annotations

import pytest

from app.jobs.reconcile import reconcile
from app.models import Subscription, SubscriptionStatus
from app.plans import Tier
from app.services.entitlements import commit_and_invalidate
from app.services.subscriptions import NO_SUBSCRIPTION, project, sync_subscription_from_stripe
from app.stripe_client import parse_subscription

STATUSES = [
    "active",
    "trialing",
    "past_due",
    "unpaid",
    "paused",
    "incomplete",
    "incomplete_expired",
    "canceled",
    "some_new_status",
]
PRICES = {
    "configured": {"price_id": "price_pro_m"},
    "grandfathered": {"price_id": "price_retired", "price_metadata": {"tier": "plus"}},
    "unknown": {"price_id": "price_from_nowhere"},
}


@pytest.mark.parametrize("price", PRICES)
@pytest.mark.parametrize("status", STATUSES)
async def test_reconcile_agrees_with_whatever_sync_wrote(session, stripe, status, price):
    session.add(Subscription(user_id="u1", stripe_customer_id="cus_1"))
    await session.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status=status, **PRICES[price])

    await sync_subscription_from_stripe(session, stripe_customer_id="cus_1")
    await commit_and_invalidate(session)
    report = await reconcile(session, dry_run=True)

    assert report.mismatched == 0, report.details


def remote(status: str, price: dict | None = None):
    raw = {"id": "sub_1", "customer": "cus_1", "status": status}
    if price is not None:
        raw["items"] = {"data": [{"id": "si_1", "price": price}]}
    return parse_subscription(raw)


def test_no_subscription_and_an_ended_one_project_the_same():
    """Stripe keeps listing a cancelled subscription; the row must not keep
    its id, or local state would depend on whether Stripe still lists it."""
    assert project(None) == NO_SUBSCRIPTION
    assert project(remote("canceled", {"id": "price_pro_m"})) == NO_SUBSCRIPTION
    assert project(remote("incomplete_expired", {"id": "price_pro_m"})) == NO_SUBSCRIPTION


def test_only_a_status_that_pays_carries_a_tier():
    paused = project(remote("paused", {"id": "price_pro_m"}))
    assert (paused.tier, paused.status) == (Tier.FREE, SubscriptionStatus.PAUSED)
    assert paused.stripe_price_id == "price_pro_m", "still mirrored, just not granted"

    active = project(remote("active", {"id": "price_pro_m"}))
    assert active.tier is Tier.PRO


def test_an_unrecognised_price_is_never_guessed_upward():
    state = project(remote("active", {"id": "price_from_nowhere"}))
    assert (state.tier, state.status) == (Tier.FREE, SubscriptionStatus.ACTIVE)


def test_a_retired_price_resolves_through_its_tier_tag():
    retired = {"id": "price_retired", "recurring": {"interval": "year"}, "metadata": {"tier": "plus"}}
    state = project(remote("active", retired))
    assert state.tier is Tier.PLUS
    assert state.billing_interval.value == "annual"
