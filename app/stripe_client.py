"""The only module that imports `stripe`.

Everything above this line talks to Stripe through the dozen functions below, so
swapping provider (or stubbing one out in tests) is a rewrite of this file and
nothing else. The Stripe SDK is synchronous; each call is pushed to a worker
thread so it cannot block the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import stripe

from app.config import get_settings


def _client() -> Any:
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    if settings.stripe_api_version:
        stripe.api_version = settings.stripe_api_version
    return stripe


async def _call(fn, /, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _as_dict(value: Any) -> Any:
    """Convert a Stripe response into plain dicts and lists, all the way down.

    `dict(stripe_object)` raised in older SDKs' successors: since stripe-python
    8 a StripeObject is not a mapping, and 15.x removed `to_dict_recursive`,
    leaving only a shallow `to_dict()` that would hand back nested StripeObjects
    for the parts we actually read (subscription items, their price, metadata).
    Walking it ourselves keeps this independent of which of those the installed
    SDK offers.
    """
    if hasattr(value, "to_dict") and not isinstance(value, dict):
        value = value.to_dict()
    if isinstance(value, dict):
        return {key: _as_dict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_dict(item) for item in value]
    return value


# --------------------------------------------------------------------------
# customers
# --------------------------------------------------------------------------


async def ensure_customer(*, user_id: str, email: str | None, existing_id: str | None) -> str:
    """Return a Stripe customer id, creating one on first use.

    `metadata.user_id` is the join key back to our database and the first thing
    to check when a webhook arrives for a customer we do not recognise.
    """
    s = _client()
    if existing_id:
        return existing_id
    customer = await _call(
        s.Customer.create,
        email=email,
        metadata={"user_id": user_id},
        idempotency_key=f"customer:{user_id}",
    )
    return customer["id"]


# --------------------------------------------------------------------------
# checkout + portal
# --------------------------------------------------------------------------


async def create_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    user_id: str,
    success_url: str,
    cancel_url: str,
    idempotency_key: str,
    trial_period_days: int = 0,
    promo_code: str | None = None,
) -> dict:
    s = get_settings()
    api = _client()

    subscription_data: dict[str, Any] = {"metadata": {"user_id": user_id}}
    if trial_period_days:
        subscription_data["trial_period_days"] = trial_period_days

    params: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer_id,
        "client_reference_id": user_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "subscription_data": subscription_data,
    }
    if s.automatic_tax:
        params["automatic_tax"] = {"enabled": True}
        # Stripe refuses automatic tax on an existing customer without this.
        params["customer_update"] = {"address": "auto"}
    if promo_code:
        params["discounts"] = [{"promotion_code": promo_code}]
    else:
        params["allow_promotion_codes"] = True

    session = await _call(api.checkout.Session.create, idempotency_key=idempotency_key, **params)
    return _as_dict(session)


async def create_portal_session(*, customer_id: str, return_url: str) -> dict:
    api = _client()
    session = await _call(
        api.billing_portal.Session.create, customer=customer_id, return_url=return_url
    )
    return _as_dict(session)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

# Ordered by how much they should win when a customer somehow has more than one
# subscription object; the first match is the one we mirror.
_STATUS_PRIORITY = ("active", "trialing", "past_due", "unpaid", "paused", "incomplete")


async def fetch_current_subscription(customer_id: str) -> dict | None:
    """The customer's current subscription as Stripe sees it, or None.

    Deliberately re-reads rather than trusting a webhook payload: events arrive
    out of order, and the answer to "what is true now" is only ever here.
    """
    api = _client()
    result = await _call(
        api.Subscription.list,
        customer=customer_id,
        status="all",
        limit=100,
        # One expand deep enough to reach the coupon: the discount itself
        # carries only an id and an end date, and the amount lives on the
        # coupon. Without this it would take a second API call per sync.
        expand=["data.items.data.price", "data.discounts.source.coupon"],
    )
    subscriptions = _as_dict(result).get("data", [])
    if not subscriptions:
        return None

    for status_name in _STATUS_PRIORITY:
        for sub in subscriptions:
            if sub.get("status") == status_name:
                return sub
    # Everything is cancelled/expired: return the most recent so the caller can
    # see *why* the customer has no access rather than an ambiguous None.
    return max(subscriptions, key=lambda s: s.get("created") or 0)


async def list_subscriptions_page(starting_after: str | None = None, limit: int = 100) -> dict:
    """One page of every subscription on the account, for reconciliation."""
    api = _client()
    params: dict[str, Any] = {
        "status": "all",
        "limit": limit,
        "expand": ["data.items.data.price", "data.discounts.source.coupon"],
    }
    if starting_after:
        params["starting_after"] = starting_after
    return _as_dict(await _call(api.Subscription.list, **params))


async def retrieve_price(price_id: str) -> dict:
    api = _client()
    return _as_dict(await _call(api.Price.retrieve, price_id))


async def retrieve_customer(customer_id: str) -> dict:
    api = _client()
    return _as_dict(await _call(api.Customer.retrieve, customer_id))


async def retrieve_charge(charge_id: str) -> dict:
    """Only needed to find the customer behind a dispute -- dispute objects do
    not carry one."""
    api = _client()
    return _as_dict(await _call(api.Charge.retrieve, charge_id))


# --------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------


def construct_event(payload: bytes, signature: str) -> dict:
    """Verify and parse. Raises stripe.SignatureVerificationError on a forgery."""
    api = _client()
    settings = get_settings()
    event = api.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    return _as_dict(event)


# --------------------------------------------------------------------------
# field helpers
# --------------------------------------------------------------------------


def subscription_period(sub: dict) -> tuple[int | None, int | None]:
    """(start, end) as unix timestamps.

    Recent Stripe API versions moved the billing period from the subscription
    onto its items; older ones keep it on the subscription. Read whichever is
    present so an account's API version upgrade does not silently null out every
    renewal date in our database.
    """
    start = sub.get("current_period_start")
    end = sub.get("current_period_end")
    if start is not None and end is not None:
        return start, end

    items = (sub.get("items") or {}).get("data") or []
    if items:
        first = items[0]
        return first.get("current_period_start"), first.get("current_period_end")
    return None, None


def subscription_discount(sub: dict) -> dict | None:
    """The discount on a subscription, flattened into something storable.

    Stripe moved from a single `discount` to a `discounts` array holding ids,
    and the amount is not on the discount at all -- it lives on the coupon the
    discount points at. Both shapes are read here so an API version change
    cannot silently drop every customer's discount.

    Only the first discount is kept. Stacking several is possible and would need
    a real decision about how to display them; one is what a promotion code
    produces.
    """
    discounts = sub.get("discounts") or []
    raw = discounts[0] if discounts else sub.get("discount")
    if not raw or isinstance(raw, str):
        # Unexpanded: an id tells us a discount exists but nothing about it.
        return None

    source = raw.get("source") or {}
    coupon = source.get("coupon") if isinstance(source.get("coupon"), dict) else None
    # Older shape put the coupon directly on the discount.
    coupon = coupon or (raw.get("coupon") if isinstance(raw.get("coupon"), dict) else None)
    if coupon is None:
        return None

    return {
        "coupon_id": coupon.get("id"),
        "name": coupon.get("name"),
        "percent_off": coupon.get("percent_off"),
        "amount_off": coupon.get("amount_off"),
        "currency": coupon.get("currency"),
        "duration": coupon.get("duration"),
        "duration_in_months": coupon.get("duration_in_months"),
        "promotion_code": raw.get("promotion_code"),
        "ends_at": raw.get("end"),
    }


def subscription_price(sub: dict) -> dict | None:
    """The price object of the first line item, expanded or not."""
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        return None
    price = items[0].get("price")
    if isinstance(price, str):
        return {"id": price}
    return price
