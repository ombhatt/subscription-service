"""One Stripe account, more than one application.

This service shares its Stripe account with another product. Their customers
carry `metadata.user_id` as well -- it is a common convention, not ours alone --
which is exactly the key this service resolves an unknown customer by. Without a
further test, a webhook for their subscription creates a row here, the nightly
reconcile finds that row every night and re-syncs it forever, and their user ids
accumulate in this database.

Ownership is the price: ours are the prices we sell, plus any price carrying the
`tier` metadata the seed script stamps, which is what keeps a grandfathered
price working after it leaves the configuration.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select

from app.jobs.reconcile import reconcile
from app.models import Subscription
from app.services.subscriptions import get_subscription, sync_subscription_from_stripe

USER = {"X-User-Id": "alice"}
# A price this service does not sell, with no tier metadata -- the shape another
# application's prices actually have.
THEIRS = "price_someone_elses_product"


async def count_rows(session) -> int:
    return (await session.execute(select(func.count()).select_from(Subscription))).scalar_one()


def foreign_customer(stripe, customer_id: str = "cus_theirs", user_id: str = "their-user") -> str:
    """Their customer: names a user, exactly as ours do."""
    stripe.customers[customer_id] = {"id": customer_id, "metadata": {"user_id": user_id}}
    stripe.set_subscription(customer_id, status="active", price_id=THEIRS)
    return customer_id


async def test_a_subscription_we_do_not_sell_creates_no_row(session, stripe):
    customer = foreign_customer(stripe)

    result = await sync_subscription_from_stripe(session, stripe_customer_id=customer)

    assert result is None
    assert await count_rows(session) == 0, "their user must not appear in our table"


async def test_their_webhook_is_acknowledged_and_ignored(client, session, stripe):
    """Stripe delivers account-wide. Returning an error would make it retry
    someone else's event forever."""
    customer = foreign_customer(stripe)

    response = await client.post(
        "/v1/webhooks/stripe",
        content=json.dumps(
            {
                "id": "evt_theirs",
                "type": "customer.subscription.updated",
                "data": {"object": {"customer": customer}},
            }
        ),
        headers={"stripe-signature": "t=1,v1=fake"},
    )

    assert response.status_code == 200
    assert await count_rows(session) == 0


async def test_a_grandfathered_price_is_still_ours(session, stripe):
    """A price we no longer sell but once did: not in the catalogue, but stamped
    with its tier. That fallback is what keeps existing subscribers working, and
    the guard must not break it."""
    stripe.customers["cus_old"] = {"id": "cus_old", "metadata": {"user_id": "long-standing"}}
    stripe.set_subscription(
        "cus_old", status="active", price_id="price_retired_pro", price_metadata={"tier": "pro"}
    )

    sub = await sync_subscription_from_stripe(session, stripe_customer_id="cus_old")

    assert sub is not None
    assert sub.tier == "pro"


async def test_a_customer_we_already_know_is_never_dropped(session, stripe):
    """Ownership is only asked when there is no row yet. An existing subscriber
    whose price we cannot resolve is our problem to report, not a stranger."""
    session.add(Subscription(user_id="ours", stripe_customer_id="cus_ours"))
    await session.commit()
    stripe.customers["cus_ours"] = {"id": "cus_ours", "metadata": {"user_id": "ours"}}
    stripe.set_subscription("cus_ours", status="active", price_id="price_unrecognised")

    sub = await sync_subscription_from_stripe(session, stripe_customer_id="cus_ours")

    assert sub is not None
    assert sub.status == "active", "still mirrored"
    assert sub.tier == "free", "an unresolved price grants nothing, and is logged"


async def test_reconcile_walks_past_another_application(session, stripe):
    """The nightly job lists every subscription in the account."""
    session.add(Subscription(user_id="u1", stripe_customer_id="cus_1", tier="free", status="free"))
    await session.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")
    for n in range(3):
        foreign_customer(stripe, customer_id=f"cus_theirs{n}", user_id=f"their-user{n}")

    report = await reconcile(session)

    assert report.checked == 4
    assert report.ignored == 3, "three belong to the other application"
    assert report.unknown_customers == [], "not ours, so not unknown customers of ours"
    assert report.repaired == 1, "only our own drift was repaired"
    assert (await get_subscription(session, "u1")).tier == "pro"
    assert await count_rows(session) == 1, "no rows invented for the other application"
