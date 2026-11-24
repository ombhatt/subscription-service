"""Buying credit packs (#137): who may, the checkout, and the webhook grant.

Over HTTP where a customer or Stripe would be: the billing endpoints, and
webhooks delivered the way Stripe delivers them -- twice, late, and as
`completed` followed by `async_payment_succeeded`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app import stripe_client
from app.models import CreditBatch, CreditLedgerEntry, Subscription, SubscriptionAudit
from app.services import credit_purchases
from app.timeutil import as_utc
from tests.fakes.stripe import credit_pack_session, deliver, webhook_event

ALICE = {"X-User-Id": "alice"}
ADMIN = {"X-Admin-Key": "test-admin-key", "X-Admin-Actor": "support-sam"}


async def subscriber(session, *, tier="plus", status="active", user_id="alice", customer="cus_1"):
    session.add(
        Subscription(user_id=user_id, stripe_customer_id=customer, tier=tier, status=status)
    )
    await session.commit()
    return customer


async def checkout(client, pack_id="small", headers=ALICE):
    return await client.post(
        "/v1/billing/credits/checkout", json={"pack_id": pack_id}, headers=headers
    )


async def batches(session) -> list[CreditBatch]:
    rows = (await session.execute(select(CreditBatch))).scalars().all()
    await session.commit()
    return list(rows)


async def ledger(session) -> list[CreditLedgerEntry]:
    rows = (await session.execute(select(CreditLedgerEntry))).scalars().all()
    await session.commit()
    return list(rows)


async def paid_checkout(client, stripe, pack_id="small", **paid) -> dict:
    """A pack checked out through the API, then paid: the session as the
    `completed` webhook carries it."""
    response = await checkout(client, pack_id)
    assert response.status_code == 200, response.text
    return credit_pack_session(stripe.credit_checkout_sessions[-1], **paid)


# --------------------------------------------------------------------------
# who can buy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["plus", "pro"])
@pytest.mark.parametrize("status", ["active", "trialing"])
async def test_an_active_or_trialing_plus_or_pro_subscriber_can_check_out(
    client, session, stripe, tier, status
):
    customer = await subscriber(session, tier=tier, status=status)

    response = await checkout(client, "medium")

    assert response.status_code == 200, response.text
    assert response.json()["checkout_url"] == "https://checkout.stripe.test/credits"
    (created,) = stripe.credit_checkout_sessions
    assert created["customer"] == customer  # reused, not a second customer
    assert created["client_reference_id"] == "alice"
    assert created["price_id"] == "price_credits_medium"
    assert created["metadata"] == {
        "kind": "credit_pack",
        "pack_id": "medium",
        "user_id": "alice",
        "credits": "2500",
    }
    assert created["success_url"].endswith(
        "/billing/credits/success?session_id={CHECKOUT_SESSION_ID}"
    )
    assert stripe.checkout_sessions == []  # never a subscription checkout


async def free_user(session):
    pass


async def past_due_plus(session):
    await subscriber(session, tier="plus", status="past_due")


async def grant_only_pro(session):
    await subscriber(session, tier="free", status="free")


async def enterprise_over_pro(session):
    await subscriber(session, tier="pro", status="active")


async def paused_pro(session):
    await subscriber(session, tier="pro", status="paused")


async def incomplete_plus(session):
    await subscriber(session, tier="plus", status="incomplete")


@pytest.mark.parametrize(
    "setup, grant",
    [
        (free_user, None),
        (past_due_plus, None),
        (grant_only_pro, "pro"),
        (enterprise_over_pro, "enterprise"),
        (paused_pro, None),
        (incomplete_plus, None),
    ],
    ids=["free", "past-due", "grant-only", "enterprise", "paused", "incomplete"],
)
async def test_everyone_else_gets_a_409_and_no_checkout(client, session, stripe, setup, grant):
    await setup(session)
    if grant:
        response = await client.post(
            "/v1/admin/grants",
            json={"user_id": "alice", "tier": grant, "reason": "test"},
            headers=ADMIN,
        )
        assert response.status_code == 200, response.text

    response = await checkout(client)

    assert response.status_code == 409, response.text
    assert stripe.credit_checkout_sessions == []
    packs = await client.get("/v1/billing/credits", headers=ALICE)
    assert packs.json()["can_buy"] is False
    assert packs.json()["reason"] == response.json()["detail"]


async def test_past_due_is_told_to_fix_the_card_first(client, session, stripe):
    await past_due_plus(session)
    response = await checkout(client)
    assert "payment failed" in response.json()["detail"]


async def test_an_unknown_pack_is_a_404(client, session, stripe):
    await subscriber(session)
    response = await checkout(client, "enormous")
    assert response.status_code == 404
    assert stripe.credit_checkout_sessions == []


async def test_a_pack_with_no_price_configured_is_a_503(client, session, stripe, monkeypatch):
    await subscriber(session)
    monkeypatch.setattr(credit_purchases, "credit_pack_price_id", lambda pack_id: None)
    response = await checkout(client)
    assert response.status_code == 503
    assert stripe.credit_checkout_sessions == []


async def test_the_billing_page_reads_packs_and_eligibility(client, session, stripe):
    await subscriber(session)
    response = await client.get("/v1/billing/credits", headers=ALICE)
    assert response.status_code == 200
    body = response.json()
    assert body["can_buy"] is True
    assert body["reason"] is None
    assert [(p["id"], p["credits"]) for p in body["packs"]] == [
        ("small", 500),
        ("medium", 2500),
        ("large", 7500),
    ]
    # Amounts come from Stripe, not from this repo.
    assert all(p["unit_amount"] == 2000 and p["currency"] == "usd" for p in body["packs"])


# --------------------------------------------------------------------------
# the idempotency key
# --------------------------------------------------------------------------


async def test_a_double_click_reuses_the_key_and_a_different_pack_does_not(
    client, session, stripe, clock
):
    await subscriber(session)
    await checkout(client, "small")
    await checkout(client, "small")
    await checkout(client, "large")
    first, again, other = (s["idempotency_key"] for s in stripe.credit_checkout_sessions)
    assert first == again
    assert other != first
    assert first.startswith("credit-checkout:alice:")
    # Never the subscription checkout's namespace.
    assert not first.startswith("checkout:")


async def test_buying_the_same_pack_again_later_gets_a_fresh_session(
    client, session, stripe, clock
):
    """A day-long key would hand the second purchase the first, already-paid
    session back from Stripe's idempotency cache."""
    await subscriber(session)
    await checkout(client, "small")
    clock.advance(minutes=5)
    await checkout(client, "small")
    first, later = (s["idempotency_key"] for s in stripe.credit_checkout_sessions)
    assert first != later


# --------------------------------------------------------------------------
# the grant
# --------------------------------------------------------------------------


async def test_a_paid_completed_checkout_grants_exactly_one_batch(client, session, stripe, clock):
    await subscriber(session)
    paid = await paid_checkout(client, stripe, "small", amount_total=538, payment_intent="pi_9")

    response = await deliver(client, webhook_event("evt_paid", "checkout.session.completed", paid))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    (batch,) = await batches(session)
    assert (batch.user_id, batch.source, batch.pack_id) == ("alice", "purchase", "small")
    assert (batch.credits_granted, batch.remaining) == (500, 500)
    assert batch.stripe_checkout_session_id == paid["id"]
    assert batch.stripe_payment_intent_id == "pi_9"
    assert batch.amount_paid == 538  # what was charged, tax included
    assert as_utc(batch.expires_at) == datetime(2028, 1, 15, 8, tzinfo=UTC)
    (entry,) = await ledger(session)
    assert (entry.kind, entry.amount, entry.batch_id) == ("purchase", 500, batch.id)
    assert entry.reference == "evt_paid"

    entitlements = await client.get("/v1/entitlements", headers=ALICE)
    assert entitlements.json()["credits"]["balance"] == 500


async def test_a_replayed_event_does_not_grant_twice(client, session, stripe):
    await subscriber(session)
    paid = await paid_checkout(client, stripe)
    event = webhook_event("evt_once", "checkout.session.completed", paid)

    await deliver(client, event)
    second = await deliver(client, event)

    assert second.json()["status"] == "duplicate"
    assert len(await batches(session)) == 1
    assert len(await ledger(session)) == 1


async def test_the_same_payment_under_another_event_id_does_not_grant_twice(
    client, session, stripe
):
    """What `processed_events` cannot catch: a different event (or, later,
    reconcile) for a checkout that has already been granted."""
    await subscriber(session)
    paid = await paid_checkout(client, stripe)

    await deliver(client, webhook_event("evt_a", "checkout.session.completed", paid))
    await deliver(client, webhook_event("evt_b", "checkout.session.completed", paid))
    await deliver(client, webhook_event("evt_c", "checkout.session.async_payment_succeeded", paid))

    assert len(await batches(session)) == 1
    assert len(await ledger(session)) == 1


async def test_an_unpaid_completed_grants_nothing_until_the_async_payment_succeeds(
    client, session, stripe
):
    await subscriber(session)
    pending = await paid_checkout(client, stripe, payment_status="unpaid", payment_intent="pi_ach")

    response = await deliver(
        client, webhook_event("evt_pending", "checkout.session.completed", pending)
    )
    assert response.status_code == 200
    assert await batches(session) == []
    status = await client.get(f"/v1/billing/credits/checkout/{pending['id']}", headers=ALICE)
    assert status.json()["granted"] is False

    cleared = {**pending, "payment_status": "paid"}
    response = await deliver(
        client, webhook_event("evt_cleared", "checkout.session.async_payment_succeeded", cleared)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    (batch,) = await batches(session)
    assert batch.stripe_payment_intent_id == "pi_ach"
    (entry,) = await ledger(session)
    assert entry.reference == "evt_cleared"


async def test_a_concurrent_grant_of_the_same_checkout_yields_the_winners_batch(
    session, sessionmaker_, monkeypatch
):
    """Two events for one payment, granted at the same moment: both pass the
    look-up, one insert wins, and the other takes its batch."""
    remote = stripe_client.parse_checkout_session(
        credit_pack_session(
            {
                "id": "cs_race",
                "customer": "cus_1",
                "metadata": {"kind": "credit_pack", "pack_id": "small", "user_id": "alice"},
            }
        )
    )
    async with sessionmaker_() as winner:
        await credit_purchases.grant_credits_from_checkout(winner, remote, reference="evt_1")
        await winner.commit()

    real = credit_purchases._batch_for
    calls = 0

    async def missed_once(s, checkout_session_id):
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real(s, checkout_session_id)

    monkeypatch.setattr(credit_purchases, "_batch_for", missed_once)
    grant = await credit_purchases.grant_credits_from_checkout(session, remote, reference="evt_2")
    await session.commit()

    assert grant is not None and grant.created is False
    assert len(await batches(session)) == 1
    assert len(await ledger(session)) == 1


async def test_a_paid_pack_with_no_user_fails_loudly_so_stripe_retries(client, session, stripe):
    broken = credit_pack_session(
        {"id": "cs_broken", "customer": "cus_1", "metadata": {"kind": "credit_pack"}}
    )
    response = await deliver(
        client, webhook_event("evt_broken", "checkout.session.completed", broken)
    )
    assert response.status_code == 500
    assert await batches(session) == []


async def test_the_credit_count_paid_for_is_the_one_granted(client, session, stripe, monkeypatch):
    """A pack resized between checkout and payment grants what was shown."""
    await subscriber(session)
    paid = await paid_checkout(client, stripe, "small")
    monkeypatch.setitem(
        credit_purchases.CREDIT_PACKS, "small", credit_purchases.CREDIT_PACKS["medium"]
    )
    await deliver(client, webhook_event("evt_resized", "checkout.session.completed", paid))
    (batch,) = await batches(session)
    assert batch.credits_granted == 500


# --------------------------------------------------------------------------
# subscription state is not touched
# --------------------------------------------------------------------------


def _state(sub: Subscription) -> tuple:
    return (
        sub.tier,
        sub.status,
        sub.stripe_customer_id,
        sub.stripe_subscription_id,
        sub.stripe_price_id,
        sub.current_period_end,
        sub.cancel_at_period_end,
    )


async def test_credit_pack_events_leave_subscription_state_alone(client, session, stripe):
    """The re-sync `completed` still triggers is the existing one, unchanged: it
    asks Stripe and writes down the same answer. It audits that it looked, as
    every webhook sync does, and changes nothing."""
    await subscriber(session, tier="free", status="free")
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "alice"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_plus_m")
    await deliver(
        client, webhook_event("evt_sub", "customer.subscription.created", {"customer": "cus_1"})
    )

    async def state():
        sub = (await session.execute(select(Subscription))).scalar_one()
        await session.refresh(sub)
        await session.commit()
        return _state(sub)

    before = await state()
    assert before[:2] == ("plus", "active")

    pending = await paid_checkout(client, stripe, payment_status="unpaid")
    await deliver(client, webhook_event("evt_c1", "checkout.session.completed", pending))
    paid = {**pending, "payment_status": "paid"}
    await deliver(client, webhook_event("evt_c2", "checkout.session.async_payment_succeeded", paid))

    assert await state() == before
    assert len(await batches(session)) == 1
    audits = (
        (
            await session.execute(
                select(SubscriptionAudit).where(
                    SubscriptionAudit.stripe_event_id.in_(["evt_c1", "evt_c2"])
                )
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    assert all((a.from_tier, a.from_status) == (a.to_tier, a.to_status) for a in audits), (
        "a credit pack event changed the subscription"
    )


async def test_a_subscription_checkout_still_syncs_and_grants_no_credits(client, session, stripe):
    await subscriber(session, tier="free", status="free")
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "alice"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    response = await deliver(
        client,
        webhook_event(
            "evt_sub_checkout",
            "checkout.session.completed",
            {"id": "cs_sub", "customer": "cus_1", "mode": "subscription", "payment_status": "paid"},
        ),
    )

    assert response.json()["status"] == "ok"
    sub = (await session.execute(select(Subscription))).scalar_one()
    await session.refresh(sub)
    assert (sub.tier, sub.status) == ("pro", "active")
    assert await batches(session) == []


async def test_async_payment_success_for_a_subscription_checkout_is_ignored(
    client, session, stripe
):
    response = await deliver(
        client,
        webhook_event(
            "evt_sub_async",
            "checkout.session.async_payment_succeeded",
            {"id": "cs_sub", "customer": "cus_1", "mode": "subscription"},
        ),
    )
    assert response.json()["status"] == "ignored"


# --------------------------------------------------------------------------
# the success page's poll
# --------------------------------------------------------------------------


async def test_the_success_page_sees_the_grant_and_the_new_balance(client, session, stripe):
    await subscriber(session)
    paid = await paid_checkout(client, stripe, "large")
    url = f"/v1/billing/credits/checkout/{paid['id']}"

    before = (await client.get(url, headers=ALICE)).json()
    assert before == {
        "granted": False,
        "credits": None,
        "balance": {"balance": 0, "next_expiry_at": None},
    }

    await deliver(client, webhook_event("evt_large", "checkout.session.completed", paid))

    after = (await client.get(url, headers=ALICE)).json()
    assert after["granted"] is True
    assert after["credits"] == 7500
    assert after["balance"]["balance"] == 7500


async def test_someone_elses_checkout_reads_as_not_granted(client, session, stripe):
    await subscriber(session)
    paid = await paid_checkout(client, stripe)
    await deliver(client, webhook_event("evt_alice", "checkout.session.completed", paid))

    response = await client.get(
        f"/v1/billing/credits/checkout/{paid['id']}", headers={"X-User-Id": "mallory"}
    )
    assert response.json()["granted"] is False
    assert response.json()["credits"] is None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_a_checkout_session_parses_with_expanded_references():
    remote = stripe_client.parse_checkout_session(
        {
            "id": "cs_1",
            "customer": {"id": "cus_1"},
            "payment_intent": {"id": "pi_1"},
            "payment_status": "paid",
            "amount_total": 2000,
            "currency": "usd",
            "metadata": {"kind": "credit_pack"},
        }
    )
    assert (remote.customer_id, remote.payment_intent_id, remote.paid) == ("cus_1", "pi_1", True)
    assert remote.amount_total == 2000


@pytest.mark.parametrize("status", ["unpaid", "no_payment_required", None])
def test_only_paid_is_paid(status):
    remote = stripe_client.parse_checkout_session({"id": "cs_1", "payment_status": status})
    assert remote.paid is False


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_the_seed_script_prices_every_pack_as_the_prd_says():
    from app.plans import CREDIT_PACKS, credit_pack_env_name
    from scripts.seed_stripe import CREDIT_PACK_AMOUNTS

    packs = {pid: (p.credits, CREDIT_PACK_AMOUNTS[pid]) for pid, p in CREDIT_PACKS.items()}
    assert packs == {"small": (500, 500), "medium": (2500, 2000), "large": (7500, 5000)}
    assert credit_pack_env_name("medium") == "STRIPE_PRICE_CREDITS_MEDIUM"


async def test_billing_health_names_an_unconfigured_pack(client, monkeypatch):
    from app.routers import billing

    monkeypatch.setattr(
        billing, "credit_pack_price_id", lambda pack_id: None if pack_id == "large" else "price_x"
    )
    response = await client.get("/v1/billing/health")
    assert response.json()["unconfigured_prices"] == ["credits/large"]
