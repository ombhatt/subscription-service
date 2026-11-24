"""Buying credit packs: who may, the checkout, and the grant.

Policy in docs/adr/0001-prepaid-credits.md; vocabulary in GLOSSARY.md.

`grant_credits_from_checkout` is the only writer of purchase batches, the way
`sync_subscription_from_stripe` is the only writer of subscription state -- and
nothing here writes subscription state. Checkout grants nothing on its own: the
webhook does, once Stripe says the money has moved.

Nothing in this module commits. Callers own the transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import stripe_client, timeutil
from app.billing_provider import RemoteCheckoutSession
from app.config import get_settings
from app.errors import BillingError
from app.models import (
    CreditBatch,
    CreditLedgerEntry,
    CreditLedgerKind,
    CreditSource,
    SubscriptionStatus,
)
from app.observability import event as log_event
from app.plans import CREDIT_PACK_TIERS, CREDIT_PACKS, Tier, credit_pack_price_id
from app.services.credits import expiry_for
from app.services.entitlements import resolve_entitlements
from app.services.subscriptions import get_subscription

log = logging.getLogger(__name__)

# `metadata.kind` on a credit pack's Checkout Session and PaymentIntent. The
# webhook routes on it; a subscription checkout carries no `kind`.
CREDIT_PACK_KIND = "credit_pack"

_BUYING_STATUSES = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING})


async def purchase_refusal(session: AsyncSession, user_id: str) -> str | None:
    """Why this user may not buy a credit pack right now, or None if they may.

    Only an active or trialing Plus or Pro *subscription* qualifies:

    - Past due: they owe money already; selling them more first is backwards.
    - Grant only: a complimentary plan has no customer relationship to sell on.
    - Free: the paywall offers the upgrade instead.
    - Enterprise (by a grant over a Plus/Pro subscription): no caps to top up.
    """
    sub = await get_subscription(session, user_id)
    status = SubscriptionStatus(sub.status) if sub is not None else SubscriptionStatus.FREE
    if status is SubscriptionStatus.PAST_DUE:
        return (
            "Your last payment failed. Update your card in Manage billing, "
            "then you can buy credits."
        )
    if sub is None or status not in _BUYING_STATUSES or Tier(sub.tier) not in CREDIT_PACK_TIERS:
        return "Credit packs are available with an active Plus or Pro subscription."

    effective = await resolve_entitlements(session, user_id)
    if effective.tier not in CREDIT_PACK_TIERS:
        return f"{effective.display_name} has no daily caps for credits to extend."
    return None


async def can_buy_credits(session: AsyncSession, user_id: str) -> bool:
    return await purchase_refusal(session, user_id) is None


async def start_credit_checkout(
    session: AsyncSession, *, user_id: str, email: str | None, pack_id: str
) -> dict:
    """A hosted, one-time Checkout for a pack. Grants nothing.

    Flushes the customer id onto the subscription row if Stripe had to create
    one -- which an eligible buyer, being a subscriber, never needs.
    """
    pack = CREDIT_PACKS.get(pack_id)
    if pack is None:
        raise BillingError(f"no credit pack named {pack_id!r}", code=404)
    price_id = credit_pack_price_id(pack.id)
    if not price_id:
        raise BillingError(
            "Credit packs are not available right now. Your plan is unaffected.", code=503
        )

    refusal = await purchase_refusal(session, user_id)
    if refusal is not None:
        raise BillingError(refusal, code=409)

    sub = await get_subscription(session, user_id)
    assert sub is not None  # purchase_refusal requires one
    customer_id = await stripe_client.ensure_customer(
        user_id=user_id, email=email, existing_id=sub.stripe_customer_id
    )
    if sub.stripe_customer_id != customer_id:
        sub.stripe_customer_id = customer_id
        await session.flush()

    settings = get_settings()
    # The credit count travels with the session, so a pack resized between
    # checkout and payment grants what the customer was shown and paid for.
    metadata = {
        "kind": CREDIT_PACK_KIND,
        "pack_id": pack.id,
        "user_id": user_id,
        "credits": str(pack.credits),
    }
    return await stripe_client.create_credit_checkout_session(
        customer_id=customer_id,
        price_id=price_id,
        user_id=user_id,
        metadata=metadata,
        success_url=settings.credits_success_url,
        cancel_url=settings.credits_cancel_url,
        idempotency_key=_idempotency_key(
            user_id=user_id,
            customer_id=customer_id,
            price_id=price_id,
            metadata=metadata,
            success_url=settings.credits_success_url,
            cancel_url=settings.credits_cancel_url,
            automatic_tax=settings.automatic_tax,
        ),
    )


def _idempotency_key(**params: object) -> str:
    """Hashed from the request, like subscription checkout's key -- but scoped
    to the minute, not the day.

    A subscription is bought once, so a day-long key costs nothing. A pack is
    bought again: with a day-long key, the second purchase of the same pack
    would get the first, already-paid session back from Stripe's idempotency
    cache, and the customer could not buy it. A minute still absorbs a
    double-click; at worst one straddling the boundary opens a second session,
    which costs nothing unless it is also paid.
    """
    user_id = params["user_id"]
    payload = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"credit-checkout:{user_id}:{timeutil.utcnow():%Y-%m-%dT%H:%M}:{digest}"


@dataclass(frozen=True)
class Grant:
    batch: CreditBatch
    # False when this checkout had already been granted: a replayed event, or
    # `async_payment_succeeded` after a paid `completed`.
    created: bool


async def grant_credits_from_checkout(
    session: AsyncSession, checkout: RemoteCheckoutSession, *, reference: str
) -> Grant | None:
    """Turn a paid credit pack checkout into a batch and a `purchase` ledger row.

    The only writer of purchase batches. Idempotent by the checkout session id,
    which is unique on the batch: the same payment can arrive as a redelivered
    event, as `completed` and then `async_payment_succeeded`, or (later) from
    reconcile, and it pays out once. Returns None for a session that is not a
    credit pack or has not been paid yet; `reference` is the cause for the
    ledger, normally the Stripe event id.
    """
    metadata = checkout.metadata
    if metadata.get("kind") != CREDIT_PACK_KIND or not checkout.paid:
        return None

    existing = await _batch_for(session, checkout.id)
    if existing is not None:
        return Grant(existing, created=False)

    user_id = metadata.get("user_id")
    pack_id = metadata.get("pack_id")
    credits = _credits_for(metadata)
    if not user_id or credits is None:
        # Loud on purpose: the customer has paid, so the webhook fails and is
        # retried rather than being acknowledged and forgotten.
        raise ValueError(f"credit pack checkout {checkout.id} carries no user or credit count")

    try:
        async with session.begin_nested():
            batch = CreditBatch(
                user_id=user_id,
                source=CreditSource.PURCHASE.value,
                pack_id=pack_id,
                credits_granted=credits,
                remaining=credits,
                expires_at=expiry_for(timeutil.utcnow()),
                stripe_checkout_session_id=checkout.id,
                stripe_payment_intent_id=checkout.payment_intent_id,
                amount_paid=checkout.amount_total,
            )
            session.add(batch)
            await session.flush()
            session.add(
                CreditLedgerEntry(
                    user_id=user_id,
                    batch_id=batch.id,
                    kind=CreditLedgerKind.PURCHASE.value,
                    amount=credits,
                    reference=reference,
                )
            )
            await session.flush()
    except IntegrityError:
        # The same payment granted concurrently (two event types at once); the
        # winner's batch is the grant.
        existing = await _batch_for(session, checkout.id)
        if existing is None:
            raise
        return Grant(existing, created=False)

    log_event(
        log,
        "credits.purchased",
        user_id=user_id,
        pack_id=pack_id,
        credits=credits,
        checkout_session_id=checkout.id,
        amount_paid=checkout.amount_total,
    )
    return Grant(batch, created=True)


def _credits_for(metadata) -> int | None:
    raw = metadata.get("credits")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None
    pack = CREDIT_PACKS.get(metadata.get("pack_id") or "")
    return pack.credits if pack else None


async def _batch_for(session: AsyncSession, checkout_session_id: str) -> CreditBatch | None:
    result = await session.execute(
        select(CreditBatch).where(CreditBatch.stripe_checkout_session_id == checkout_session_id)
    )
    return result.scalar_one_or_none()


async def purchase_for(
    session: AsyncSession, *, user_id: str, checkout_session_id: str
) -> CreditBatch | None:
    """The batch a checkout produced, if it has landed -- and only if it is this
    user's. The success page polls this."""
    batch = await _batch_for(session, checkout_session_id)
    if batch is None or batch.user_id != user_id:
        return None
    return batch
