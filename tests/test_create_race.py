"""The create race for a customer we have no row for yet.

With no row there is nothing to lock, so `sync_subscription_from_stripe` reads
Stripe first and inserts after. When two events for that customer race, one
insert wins and the loser falls back to the winner's row -- taking the row lock
only *then*. Whatever it read from Stripe before that lock may be older than
what the winner has just committed, so the loser must re-read under the lock,
exactly as the normal path does. Writing its earlier answer would overwrite
newer state with older: a customer who has paid is mirrored as unpaid.
"""

from __future__ import annotations

from app.plans import Tier
from app.services import subscriptions
from app.services.entitlements import commit_and_invalidate
from app.services.subscriptions import get_subscription, sync_subscription_from_stripe

CUSTOMER = "cus_dashboard"
USER = "bob"


async def test_the_loser_of_the_create_race_does_not_write_its_stale_read(
    sessionmaker_, stripe, monkeypatch
):
    # A subscription created outside checkout, so there is no local row. It
    # starts incomplete; the first payment clears a moment later.
    stripe.set_subscription(CUSTOMER, user_id=USER, status="incomplete", price_id="price_pro_m")

    real_retrieve = stripe.retrieve_customer
    raced = False

    async def retrieve_then_lose_the_race(customer_id):
        # Runs between the loser's read of Stripe and its insert. Meanwhile the
        # payment succeeds, and a second event's sync reads the new state,
        # creates the row and commits.
        nonlocal raced
        if not raced:
            raced = True
            stripe.set_subscription(CUSTOMER, user_id=USER, status="active", price_id="price_pro_m")
            async with sessionmaker_() as winner:
                await sync_subscription_from_stripe(
                    winner, stripe_customer_id=CUSTOMER, reason="invoice.paid"
                )
                await commit_and_invalidate(winner)
        return await real_retrieve(customer_id)

    monkeypatch.setattr(
        subscriptions.stripe_client, "retrieve_customer", retrieve_then_lose_the_race
    )

    async with sessionmaker_() as loser:
        await sync_subscription_from_stripe(
            loser, stripe_customer_id=CUSTOMER, reason="customer.subscription.created"
        )
        await commit_and_invalidate(loser)

    assert raced
    async with sessionmaker_() as check:
        sub = await get_subscription(check, USER)
        assert sub.status == "active"
        assert sub.tier == Tier.PRO.value
