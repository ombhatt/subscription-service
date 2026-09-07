from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import stripe_client
from app.auth import CurrentUser, get_current_user, get_current_user_optional
from app.cache import get_json, set_json
from app.db import get_session
from app.errors import BillingError
from app.flags import is_enabled
from app.models import SalesInquiry
from app.observability import event as log_event
from app.observability import sales_inquiries
from app.plans import CATALOG, TIER_RANK, BillingInterval, Tier, price_id_for
from app.schemas import (
    CheckoutRequest,
    CheckoutResponse,
    ContactSalesRequest,
    ContactSalesResponse,
    PortalResponse,
    SubscriptionSummary,
)
from app.services.entitlements import resolve_entitlements
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
                "purchasable": definition.purchasable,
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
    # The kill switch. During a Stripe incident the alternative is every
    # purchase failing somewhere inside their API, at a different point each
    # time, with a different error. One clear 503 is better for the customer
    # and much better for whoever is reading the logs.
    if not await is_enabled("checkout-enabled", user_id=user.id):
        log_event(log, "checkout.disabled", user_id=user.id, tier=body.tier)
        raise BillingError(
            "Checkout is temporarily unavailable. Your plan and access are unaffected.",
            code=503,
        )

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
        discount=sub.discount,
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


@router.post("/contact-sales", response_model=ContactSalesResponse, status_code=201)
async def contact_sales(
    body: ContactSalesRequest,
    user: CurrentUser | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> ContactSalesResponse:
    """Record an Enterprise inquiry.

    Deliberately public. The pricing page is unauthenticated, and the leads
    worth having are often from people evaluating before they sign up --
    requiring a login here would filter out exactly those.

    That makes this the only unauthenticated write in the service, and there is
    no rate limiting: the field lengths in ContactSalesRequest are the only
    thing bounding what a script can insert. `sales_inquiries_total` is the
    signal to watch, and edge rate limiting is on the list in the README before
    this takes real traffic.

    Nothing is emailed from here. Recording it durably is the job; who gets
    notified is a workflow decision, and a background send would be one more
    thing to fail inside a request the customer is waiting on.
    """
    # A signed-in subscriber's current tier is the most useful thing on the
    # record: someone already paying for Pro who asks about Enterprise is a
    # different conversation from a visitor browsing the pricing page.
    current_tier = None
    if user is not None:
        current_tier = (await resolve_entitlements(session, user.id))["tier"]

    inquiry = SalesInquiry(
        user_id=user.id if user else None,
        email=body.email,
        company=body.company,
        seats=body.seats,
        message=body.message,
        source=body.source,
        current_tier=current_tier,
    )
    session.add(inquiry)
    await session.commit()

    sales_inquiries.labels(source=body.source, current_tier=current_tier or "anonymous").inc()
    log_event(
        log,
        "sales.inquiry",
        inquiry_id=inquiry.id,
        source=body.source,
        seats=body.seats,
        current_tier=current_tier,
        has_company=bool(body.company),
    )
    return ContactSalesResponse(id=inquiry.id)
