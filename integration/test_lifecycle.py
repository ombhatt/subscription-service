"""Subscription lifecycle against real Stripe objects, driven by test clocks.

The pytest suite in `tests/` proves our logic is right given a *fake* Stripe.
This proves our reading of the *real* one is right: real API version, real field
placement, real status transitions, real renewal invoices.

Every case ends in `sync_subscription_from_stripe` and asserts what landed in
our own table, because that function is the only thing that ever changes a
subscription.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.jobs.expire_grace import expire_grace_windows
from app.models import SubscriptionStatus
from app.policy import grace_ends_at, grace_expired
from app.services.entitlements import resolve_entitlements
from app.services.subscriptions import get_subscription, sync_subscription_from_stripe


def a_user(prefix: str) -> str:
    return f"clock-{prefix}-{uuid.uuid4().hex[:8]}"


async def sync(session, customer_id: str):
    sub = await sync_subscription_from_stripe(session, stripe_customer_id=customer_id)
    await session.commit()
    return sub


async def test_a_new_subscription_grants_its_tier(session, clock, prices):
    """The baseline: a real subscription, read through our own resolver."""
    user_id = a_user("new")
    customer = clock.customer(user_id)
    clock.subscribe(customer["id"], prices["pro_monthly"])

    sub = await sync(session, customer["id"])

    assert sub is not None, "sync should resolve the user from customer metadata"
    assert sub.user_id == user_id
    assert sub.tier == "pro"
    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.current_period_end is not None, "period must survive Stripe's API shape"
    assert sub.billing_interval == "monthly"

    ents = await resolve_entitlements(session, user_id)
    assert ents["tier"] == "pro"
    assert "reasoning" in ents["features"]["models"]


async def test_renewal_moves_the_period_forward(session, clock, prices):
    user_id = a_user("renew")
    customer = clock.customer(user_id)
    subscription = clock.subscribe(customer["id"], prices["pro_monthly"])

    first = await sync(session, customer["id"])
    first_period_end = first.current_period_end

    clock.advance_past_renewal(subscription["id"])
    renewed = await sync(session, customer["id"])

    assert renewed.status == SubscriptionStatus.ACTIVE.value, "a paid renewal stays active"
    assert renewed.tier == "pro"
    assert renewed.current_period_end > first_period_end, (
        "the renewal must move our period end forward, or every quota keyed to the "
        "billing period silently stops resetting"
    )


async def test_a_failed_renewal_opens_the_grace_window(session, clock, prices):
    """The card worked at signup and fails at renewal -- the common case.

    This is the scenario the whole dunning design exists for, and the one the
    fake can only approximate.
    """
    user_id = a_user("dunning")
    customer = clock.customer(user_id)
    subscription = clock.subscribe(customer["id"], prices["pro_monthly"])

    active = await sync(session, customer["id"])
    assert active.status == SubscriptionStatus.ACTIVE.value

    # The card on file starts declining.
    clock.set_payment_method(customer["id"], "pm_card_chargeCustomerFail")
    clock.advance_past_renewal(subscription["id"])

    failed = await sync(session, customer["id"])
    assert failed.status == SubscriptionStatus.PAST_DUE.value
    assert failed.past_due_since is not None
    assert failed.tier == "pro", "grace keeps paid access while the card is retried"
    assert grace_ends_at(failed) is not None

    ents = await resolve_entitlements(session, user_id)
    assert ents["tier"] == "pro"
    assert ents["grace_ends_at"] is not None


async def test_the_grace_window_closes_on_our_clock_not_stripes(session, clock, prices):
    """Worth stating explicitly: a test clock moves *Stripe's* time, not ours.

    `past_due_since` is stamped with real wall-clock time, so no amount of
    advancing the simulation ages our grace window. Anything time-based that we
    own has to be tested on our own terms -- which is exactly what this does.
    """
    user_id = a_user("grace")
    customer = clock.customer(user_id)
    subscription = clock.subscribe(customer["id"], prices["pro_monthly"])
    await sync(session, customer["id"])

    clock.set_payment_method(customer["id"], "pm_card_chargeCustomerFail")
    clock.advance_past_renewal(subscription["id"])
    sub = await sync(session, customer["id"])
    assert sub.tier == "pro"

    # Age the window past its limit, the only way our own policy can be moved.
    sub.past_due_since = datetime.now(UTC) - timedelta(days=30)
    await session.commit()
    assert grace_expired(sub)

    # The read path revokes immediately, without waiting for the nightly job.
    ents = await resolve_entitlements(session, user_id)
    assert ents["tier"] == "free"

    # And the job makes it durable.
    expired = await expire_grace_windows(session)
    assert user_id in expired
    assert (await get_subscription(session, user_id)).tier == "free"


async def test_cancelling_keeps_access_until_the_boundary(session, clock, prices):
    import stripe

    user_id = a_user("cancel")
    customer = clock.customer(user_id)
    subscription = clock.subscribe(customer["id"], prices["pro_monthly"])
    await sync(session, customer["id"])

    stripe.Subscription.modify(subscription["id"], cancel_at_period_end=True)
    cancelling = await sync(session, customer["id"])

    assert cancelling.cancel_at_period_end is True
    assert cancelling.tier == "pro", "they paid through the end of the period"
    assert cancelling.status == SubscriptionStatus.ACTIVE.value

    clock.advance_past_renewal(subscription["id"])
    ended = await sync(session, customer["id"])

    assert ended.tier == "free"
    assert ended.status == SubscriptionStatus.FREE.value
    assert ended.stripe_subscription_id is None
    assert (await resolve_entitlements(session, user_id))["tier"] == "free"


async def test_a_trial_grants_access_then_converts(session, clock, prices):
    user_id = a_user("trial")
    customer = clock.customer(user_id)
    subscription = clock.subscribe(
        customer["id"],
        prices["pro_monthly"],
        trial_end=clock.now + 7 * 24 * 60 * 60,
    )

    trialing = await sync(session, customer["id"])
    assert trialing.status == SubscriptionStatus.TRIALING.value
    assert trialing.tier == "pro", "a trial grants the tier it is a trial of"
    assert trialing.trial_end is not None
    assert (await resolve_entitlements(session, user_id))["tier"] == "pro"

    clock.advance_past_renewal(subscription["id"])
    converted = await sync(session, customer["id"])

    assert converted.status == SubscriptionStatus.ACTIVE.value
    assert converted.tier == "pro"


async def test_an_upgrade_is_reflected_immediately(session, clock, prices):
    """Plus to Pro mid-cycle, the way the portal would do it."""
    import stripe

    user_id = a_user("upgrade")
    customer = clock.customer(user_id)
    subscription = clock.subscribe(customer["id"], prices["plus_monthly"])

    started = await sync(session, customer["id"])
    assert started.tier == "plus"

    item_id = subscription["items"]["data"][0]["id"]
    stripe.Subscription.modify(
        subscription["id"],
        items=[{"id": item_id, "price": prices["pro_monthly"]}],
        proration_behavior="always_invoice",
    )

    upgraded = await sync(session, customer["id"])
    assert upgraded.tier == "pro"
    assert upgraded.stripe_price_id == prices["pro_monthly"]
    assert (await resolve_entitlements(session, user_id))["tier"] == "pro"
