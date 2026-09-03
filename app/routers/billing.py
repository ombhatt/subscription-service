from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import stripe_client
from app.auth import CurrentUser, get_current_user
from app.cache import get_json, set_json
from app.db import get_session
from app.errors import BillingError
from app.plans import CATALOG, TIER_RANK, BillingInterval, Tier, price_id_for
from app.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PortalResponse,
    SubscriptionSummary,
)
from app.services.subscriptions import get_or_create_subscription, open_portal, start_checkout

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/billing", tags=["billing"])


_PLANS_CACHE_KEY = "plans:v1"
_PLANS_CACHE_TTL = 300


@router.get("/plans")
async def plans() -> list[dict]:
    """The catalog, for the pricing page.

    Limits come from plans.py; amounts come from Stripe, which owns what a price
    costs. Neither is duplicated in the frontend -- a pricing page that
    hard-codes "$20/mo" is a pricing page that will eventually lie.

    Cached, because this is public and unauthenticated, and because it should
    not put a Stripe API call in front of every visitor.
    """
    cached = await get_json(_PLANS_CACHE_KEY)
    if cached is not None:
        return cached

    out = []
    for tier in sorted(TIER_RANK, key=lambda t: TIER_RANK[t]):
        definition = CATALOG[tier]
        prices = {}
        for interval in BillingInterval:
            price_id = price_id_for(tier, interval)
            if not price_id:
                continue
            prices[interval.value] = {"price_id": price_id, **await _price_amount(price_id)}

        out.append(
            {
                "tier": tier.value,
                "display_name": definition.display_name,
                "purchasable": tier is not Tier.FREE,
                "features": definition.features,
                "quotas": [
                    {"key": q.key, "limit": q.limit, "window": q.window.value}
                    for q in definition.quotas.values()
                ],
                "prices": prices,
            }
        )

    await set_json(_PLANS_CACHE_KEY, out, _PLANS_CACHE_TTL)
    return out


async def _price_amount(price_id: str) -> dict:
    """Amount and currency for a price, or nulls if Stripe is unreachable.

    A pricing page that renders without amounts is bad; one that 500s is worse.
    """
    try:
        price = await stripe_client.retrieve_price(price_id)
    except Exception:
        log.exception("could not read price %s from Stripe", price_id)
        return {"unit_amount": None, "currency": None}
    return {"unit_amount": price.get("unit_amount"), "currency": price.get("currency")}


@router.post("/checkout", response_model=CheckoutResponse)
async def checkout(
    body: CheckoutRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CheckoutResponse:
    """Start a hosted checkout. Grants nothing -- the webhook does that."""
    stripe_session = await start_checkout(
        session,
        user_id=user.id,
        email=user.email,
        tier=body.tier,
        interval=body.interval,
        success_url=body.success_url,
        cancel_url=body.cancel_url,
        promo_code=body.promo_code,
    )
    await session.commit()
    return CheckoutResponse(checkout_url=stripe_session["url"], session_id=stripe_session["id"])


@router.post("/portal", response_model=PortalResponse)
async def portal(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PortalResponse:
    """Change plan, cancel, update card, download invoices.

    All of it is Stripe's hosted portal, which is why this service has no
    billing UI of its own and no proration code.
    """
    url = await open_portal(session, user_id=user.id)
    return PortalResponse(portal_url=url)


@router.get("/subscription", response_model=SubscriptionSummary)
async def my_subscription(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionSummary:
    sub = await get_or_create_subscription(session, user.id)
    await session.commit()
    return SubscriptionSummary(
        user_id=sub.user_id,
        tier=Tier(sub.tier),
        status=sub.status,
        stripe_customer_id=sub.stripe_customer_id,
        stripe_subscription_id=sub.stripe_subscription_id,
        stripe_price_id=sub.stripe_price_id,
        billing_interval=sub.billing_interval,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        trial_end=sub.trial_end,
        past_due_since=sub.past_due_since,
        disputed_at=sub.disputed_at,
    )


@router.get("/health")
async def billing_health() -> dict:
    """Cheap config check: are the prices this service needs actually set?"""
    missing = [
        f"{tier.value}/{interval.value}"
        for tier in (Tier.PLUS, Tier.PRO)
        for interval in BillingInterval
        if not price_id_for(tier, interval)
    ]
    if missing and len(missing) == 4:
        raise BillingError("no Stripe prices configured; run scripts/seed_stripe.py", code=503)
    return {"status": "ok", "unconfigured_prices": missing}
