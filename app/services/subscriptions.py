"""The write path.

Everything that can change a subscription funnels through
`sync_subscription_from_stripe`. It takes no event payload and applies no
deltas: it asks Stripe what is true for a customer and writes that down. That is
what makes duplicate delivery a no-op and out-of-order delivery harmless.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import stripe_client
from app.config import get_settings
from app.errors import BillingError
from app.models import PAID_STATUSES, Subscription, SubscriptionStatus
from app.plans import BillingInterval, Tier, price_catalog, price_id_for
from app.services import audit
from app.services.entitlements import invalidate_entitlements

log = logging.getLogger(__name__)

# Stripe's vocabulary -> ours. Anything unlisted is treated as no paid access.
STATUS_MAP: dict[str, SubscriptionStatus] = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "paused": SubscriptionStatus.PAUSED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.FREE,
    "canceled": SubscriptionStatus.FREE,
}


def _ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value else None


def _fingerprint(sub: Subscription) -> tuple:
    """Everything the cached entitlement payload exposes.

    Broader than the audit snapshot on purpose: cancelling changes neither tier
    nor status, but a customer who just cancelled should not keep seeing
    "renews on the 3rd" for another cache TTL.
    """
    return (
        sub.tier,
        sub.status,
        sub.stripe_price_id,
        sub.current_period_start,
        sub.current_period_end,
        sub.cancel_at_period_end,
        sub.trial_end,
        sub.past_due_since,
        # A discount ending changes what they pay and must invalidate the cache.
        None if sub.discount is None else tuple(sorted(sub.discount.items())),
    )


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------


async def get_subscription(session: AsyncSession, user_id: str) -> Subscription | None:
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def get_or_create_subscription(session: AsyncSession, user_id: str) -> Subscription:
    """Every account has a row, from signup. Nothing downstream has to handle
    the 'user with no subscription' case."""
    sub = await get_subscription(session, user_id)
    if sub is not None:
        return sub
    sub = Subscription(
        user_id=user_id,
        tier=Tier.FREE.value,
        status=SubscriptionStatus.FREE.value,
    )
    session.add(sub)
    await session.flush()
    return sub


async def _find_by_customer(
    session: AsyncSession, customer_id: str, *, lock: bool = False
) -> Subscription | None:
    """Find a subscription by provider customer id, optionally locking the row.

    `lock=True` emits `SELECT ... FOR UPDATE`, so a concurrent sync for the same
    customer waits rather than interleaving. SQLite silently omits the clause,
    which is why the test suite still runs against it.
    """
    stmt = select(Subscription).where(Subscription.stripe_customer_id == customer_id)
    if lock:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --------------------------------------------------------------------------
# tier resolution
# --------------------------------------------------------------------------


def resolve_tier(price: dict | None) -> tuple[Tier | None, BillingInterval | None]:
    """Which tier a Stripe price represents.

    Configured price ids win. A price we no longer configure -- the old one a
    grandfathered subscriber is still on -- resolves through the `tier` metadata
    the seed script stamps on every price. A price with neither resolves to
    nothing, and the caller records that loudly rather than guessing.
    """
    if not price:
        return None, None

    price_id = price.get("id")
    catalog = price_catalog()
    if price_id in catalog:
        return catalog[price_id]

    interval = None
    recurring = price.get("recurring") or {}
    if recurring.get("interval") == "month":
        interval = BillingInterval.MONTHLY
    elif recurring.get("interval") == "year":
        interval = BillingInterval.ANNUAL

    meta_tier = (price.get("metadata") or {}).get("tier")
    if meta_tier:
        try:
            return Tier(meta_tier), interval
        except ValueError:
            pass
    return None, interval


# --------------------------------------------------------------------------
# the one sync function
# --------------------------------------------------------------------------


async def sync_subscription_from_stripe(
    session: AsyncSession,
    *,
    stripe_customer_id: str,
    stripe_event_id: str | None = None,
    reason: str = "stripe.sync",
) -> Subscription | None:
    """Re-read the customer's subscription from Stripe and mirror it locally.

    Idempotent by construction: calling it twice with the same Stripe state
    produces the same row and only one audit entry.

    Concurrent-safe by row lock. Stripe delivers several events for one customer
    at once -- a checkout completing produces `checkout.session.completed`,
    `customer.subscription.created` and `invoice.paid` within the same second --
    and without a lock two workers both read the row, both call Stripe, and both
    write. They usually agree, but if one call is slower it can land *after* the
    other and overwrite newer state with older.

    The order matters and is the whole point: take the lock **before** asking
    Stripe. Locking afterwards would let both workers fetch the same stale
    answer and merely serialise the writes, which fixes nothing. Locking first
    means the second worker queries Stripe only once the first has finished, so
    it necessarily sees state at least as new.

    The cost is holding a row lock across a network call to Stripe -- a few
    hundred milliseconds, and only ever contended by events for the same
    customer.
    """
    sub = await _find_by_customer(session, stripe_customer_id, lock=True)
    if sub is None:
        # First webhook for this customer, or a customer created outside our
        # checkout. The join key is the metadata we set at creation time.
        customer = await stripe_client.retrieve_customer(stripe_customer_id)
        user_id = (customer.get("metadata") or {}).get("user_id")
        if not user_id:
            log.error("stripe customer %s has no user_id metadata; skipping", stripe_customer_id)
            return None

        # There is no row to lock yet, so the burst of events that follows a
        # first checkout can all arrive here at once. The unique constraints
        # make that safe -- one insert wins -- but without this the losers raise,
        # the webhook 500s, and every new customer leaves failures behind until
        # Stripe retries. A savepoint keeps the failure local, then we take the
        # row the winner created.
        try:
            async with session.begin_nested():
                sub = await get_or_create_subscription(session, user_id)
                sub.stripe_customer_id = stripe_customer_id
                await session.flush()
        except IntegrityError:
            sub = await _find_by_customer(session, stripe_customer_id, lock=True)
            if sub is None:
                # Lost the race on user_id rather than customer id: the row
                # exists but does not carry this customer yet.
                sub = await get_subscription(session, user_id)
            if sub is None:
                raise
            sub.stripe_customer_id = stripe_customer_id
            log.info("lost the create race for %s; using the existing row", stripe_customer_id)

    before = audit.snapshot(sub)
    before_fingerprint = _fingerprint(sub)
    remote = await stripe_client.fetch_current_subscription(stripe_customer_id)

    if remote is None:
        _apply_no_subscription(sub)
    else:
        _apply_remote(sub, remote)

    after = audit.snapshot(sub)
    changed = before != after
    if changed or reason != "stripe.sync":
        await audit.record(
            session,
            user_id=sub.user_id,
            before=before,
            after=after,
            reason=reason,
            stripe_event_id=stripe_event_id,
            detail={"stripe_subscription_id": sub.stripe_subscription_id},
        )

    await session.flush()
    if _fingerprint(sub) != before_fingerprint:
        await invalidate_entitlements(sub.user_id)
    return sub


def _apply_no_subscription(sub: Subscription) -> None:
    sub.tier = Tier.FREE.value
    sub.status = SubscriptionStatus.FREE.value
    sub.stripe_subscription_id = None
    sub.stripe_price_id = None
    sub.billing_interval = None
    sub.current_period_start = None
    sub.current_period_end = None
    sub.cancel_at_period_end = False
    sub.trial_end = None
    sub.past_due_since = None
    sub.discount = None


def _apply_remote(sub: Subscription, remote: dict) -> None:
    status = STATUS_MAP.get(remote.get("status", ""), SubscriptionStatus.FREE)

    if status is SubscriptionStatus.FREE:
        # Terminal: cancelled, or an abandoned checkout that expired. Stripe
        # keeps returning the dead object, so without this the row would hold a
        # subscription id that can never be charged again -- and local state
        # would differ depending on whether the provider still lists it. The
        # transition is preserved in the audit trail either way.
        _apply_no_subscription(sub)
        return

    price = stripe_client.subscription_price(remote)
    tier, interval = resolve_tier(price)

    if status in PAID_STATUSES:
        if tier is None:
            # An unrecognised price on a live subscription. Do not guess a tier
            # upward -- drop to free and let the audit trail raise the alarm.
            log.error(
                "unresolved price %s on subscription %s",
                (price or {}).get("id"),
                remote.get("id"),
            )
            sub.tier = Tier.FREE.value
        else:
            sub.tier = tier.value
    else:
        sub.tier = Tier.FREE.value

    # past_due_since is set once, on entry, and cleared on the way out: the grace
    # window must not restart every time a retry fails.
    if status is SubscriptionStatus.PAST_DUE:
        if sub.past_due_since is None:
            sub.past_due_since = datetime.now(UTC)
    else:
        sub.past_due_since = None

    period_start, period_end = stripe_client.subscription_period(remote)

    sub.status = status.value
    sub.stripe_subscription_id = remote.get("id")
    sub.stripe_price_id = (price or {}).get("id")
    sub.billing_interval = interval.value if interval else None
    sub.current_period_start = _ts(period_start)
    sub.current_period_end = _ts(period_end)
    sub.cancel_at_period_end = bool(remote.get("cancel_at_period_end"))
    sub.trial_end = _ts(remote.get("trial_end"))
    sub.discount = stripe_client.subscription_discount(remote)


async def mark_disputed(
    session: AsyncSession, *, stripe_customer_id: str, stripe_event_id: str | None
) -> Subscription | None:
    sub = await _find_by_customer(session, stripe_customer_id)
    if sub is None:
        return None
    sub.disputed_at = datetime.now(UTC)
    await audit.record(
        session,
        user_id=sub.user_id,
        before=audit.snapshot(sub),
        after=audit.snapshot(sub),
        reason="charge.dispute.created",
        stripe_event_id=stripe_event_id,
    )
    await session.flush()
    return sub


# --------------------------------------------------------------------------
# customer-facing operations
# --------------------------------------------------------------------------


async def start_checkout(
    session: AsyncSession,
    *,
    user_id: str,
    email: str | None,
    tier: Tier,
    interval: BillingInterval,
    success_url: str | None = None,
    cancel_url: str | None = None,
    promo_code: str | None = None,
) -> dict:
    settings = get_settings()
    if tier is Tier.FREE:
        raise BillingError("free is not a purchasable tier")

    price_id = price_id_for(tier, interval)
    if not price_id:
        raise BillingError(f"no price configured for {tier.value}/{interval.value}")

    sub = await get_or_create_subscription(session, user_id)
    if sub.status in (s.value for s in PAID_STATUSES):
        # An existing subscriber changes plan in the portal, where Stripe handles
        # proration; a second checkout would create a second subscription.
        raise BillingError(
            "already subscribed -- use the billing portal to change plan",
            code=409,
        )

    customer_id = await stripe_client.ensure_customer(
        user_id=user_id, email=email, existing_id=sub.stripe_customer_id
    )
    if sub.stripe_customer_id != customer_id:
        sub.stripe_customer_id = customer_id
        await session.flush()

    resolved_success = success_url or settings.checkout_success_url
    resolved_cancel = cancel_url or settings.checkout_cancel_url

    stripe_session = await stripe_client.create_checkout_session(
        customer_id=customer_id,
        price_id=price_id,
        user_id=user_id,
        success_url=resolved_success,
        cancel_url=resolved_cancel,
        idempotency_key=_checkout_idempotency_key(
            user_id=user_id,
            price_id=price_id,
            success_url=resolved_success,
            cancel_url=resolved_cancel,
            promo_code=promo_code,
            trial_period_days=settings.trial_period_days,
            automatic_tax=settings.automatic_tax,
        ),
        trial_period_days=settings.trial_period_days,
        promo_code=promo_code,
    )
    return stripe_session


def _checkout_idempotency_key(**params: object) -> str:
    """Identical request -> same key; different request -> different key.

    Stripe rejects a key reused with *different* parameters, so a key scoped to
    just the user and price turns any changed parameter into a hard failure for
    the next 24 hours -- a customer who retries with a promo code, or a config
    change like toggling automatic tax, both hit it. Hashing the parameters
    keeps the double-click protection while letting a genuinely different
    request through.

    The date keeps it explicit that a session is a day's worth of intent, rather
    than relying on Stripe expiring keys after 24h.
    """
    user_id = params["user_id"]
    payload = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"checkout:{user_id}:{datetime.now(UTC):%Y-%m-%d}:{digest}"


async def open_portal(session: AsyncSession, *, user_id: str, return_url: str | None = None) -> str:
    settings = get_settings()
    sub = await get_subscription(session, user_id)
    if sub is None or not sub.stripe_customer_id:
        raise BillingError("no billing account yet -- subscribe first", code=404)
    portal = await stripe_client.create_portal_session(
        customer_id=sub.stripe_customer_id,
        return_url=return_url or settings.portal_return_url,
    )
    return portal["url"]
