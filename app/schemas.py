from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import SubscriptionStatus
from app.plans import BillingInterval, Quota, QuotaWindow, Tier

# Every response model here is also the web app's type for it:
# web/lib/openapi.gen.ts is generated from this app's OpenAPI schema
# (`make api-types`), and CI fails if the committed copy is stale. A field typed
# `dict[str, Any]` reaches the frontend as `unknown`, so shapes worth branching
# on are spelled out.


class Response(BaseModel):
    """Base for what the API sends.

    A field with a default is optional to whoever *sends* it, but a response
    always carries it. Without this the schema calls those fields optional, and
    the frontend's types make every reader handle `undefined` that never comes.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Features(Response):
    """What a tier includes beyond its counted quotas.

    Spelled out rather than `dict[str, Any]` so the pricing page and the
    paywall get real types. A feature added to a tier in plans.py has to be
    added here too, which tests/test_api_types.py enforces.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: tuple[str, ...]
    context_tokens: int | None = Field(description="null means unlimited")
    history_retention_days: int | None = Field(description="null means forever")
    api_access: bool
    support_sla: str


class QuotaLimit(Response):
    """A tier's cap on one counter. The usage against it is QuotaState."""

    model_config = ConfigDict(frozen=True)

    key: str
    limit: int | None = Field(description="null means unlimited")
    window: QuotaWindow

    @classmethod
    def of(cls, quota: Quota) -> QuotaLimit:
        return cls(key=quota.key, limit=quota.limit, window=quota.window)


class QuotaState(Response):
    key: str
    limit: int | None = Field(description="null means unlimited")
    used: int
    remaining: int | None
    reset_at: datetime


class EntitlementResponse(Response):
    """The only shape the product should ever branch on."""

    user_id: str
    tier: Tier
    display_name: str
    status: SubscriptionStatus
    source: Literal["subscription", "grant", "default"]
    features: Features
    quotas: list[QuotaState]
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    grace_ends_at: datetime | None = Field(
        default=None, description="set while past_due; access ends here"
    )


class CheckoutRequest(BaseModel):
    tier: Tier
    interval: BillingInterval = BillingInterval.MONTHLY
    success_url: str | None = None
    cancel_url: str | None = None
    promo_code: str | None = None


class CheckoutResponse(Response):
    checkout_url: str
    session_id: str


class PortalRequest(BaseModel):
    """Optional: which plan the customer asked for.

    The portal opens on its home page without this, which is fine for "Manage
    billing" and wrong for a button that named a tier.
    """

    tier: Tier | None = None
    interval: BillingInterval = BillingInterval.MONTHLY


class PortalResponse(Response):
    portal_url: str


class PlanPrice(Response):
    price_id: str
    unit_amount: int | None = Field(
        description="in the currency's smallest unit; null if Stripe was unreachable"
    )
    currency: str | None


class PlanResponse(Response):
    """One card on the pricing page: limits from plans.py, amounts from Stripe."""

    tier: Tier
    display_name: str
    purchasable: bool
    sales_led: bool = Field(description='sold by a conversation: "Custom" and Contact sales')
    features: Features
    quotas: list[QuotaLimit]
    prices: dict[BillingInterval, PlanPrice]


class Discount(Response):
    """A discount mirrored from Stripe; see stripe_client._discount."""

    coupon_id: str | None = None
    name: str | None = None
    percent_off: float | None = None
    amount_off: int | None = None
    currency: str | None = None
    duration: str | None = None
    duration_in_months: int | None = None
    promotion_code: str | None = None
    ends_at: int | None = Field(default=None, description="unix seconds; null if it never ends")


class SubscriptionSummary(Response):
    user_id: str
    tier: Tier
    status: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    stripe_price_id: str | None
    billing_interval: str | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    trial_end: datetime | None
    past_due_since: datetime | None
    disputed_at: datetime | None
    # What they pay, kept off the entitlements payload on purpose: that one is
    # the hot path and answers what a user may *do*, not what they were charged.
    discount: Discount | None = None


class GrantRequest(BaseModel):
    user_id: str
    tier: Tier
    reason: str
    expires_at: datetime | None = None


class GrantResponse(Response):
    id: str
    user_id: str
    tier: Tier
    reason: str
    expires_at: datetime | None
    created_by: str
    created_at: datetime


class AuditEntry(Response):
    created_at: datetime
    reason: str
    from_tier: str | None
    to_tier: str | None
    from_status: str | None
    to_status: str | None
    stripe_event_id: str | None


class ContactSalesRequest(BaseModel):
    """An Enterprise inquiry.

    Every field is length-bounded. This endpoint is public by necessity -- the
    pricing page is -- so the request body is attacker-controlled, and an
    unbounded string here is a way to fill a database for free.
    """

    # A bounded pattern rather than pydantic's EmailStr, which would pull in
    # email-validator and dnspython for one field on a lead form. This rejects
    # the obviously malformed; a human reads these before replying anyway, and
    # deliverability is not something a regex settles.
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    company: str | None = Field(default=None, max_length=200)
    seats: int | None = Field(default=None, ge=1, le=1_000_000)
    message: str | None = Field(default=None, max_length=2000)
    source: Literal["pricing_page", "paywall", "billing_page"] = "pricing_page"


class ContactSalesResponse(Response):
    id: str
    status: Literal["received"] = "received"


class SalesInquirySummary(Response):
    id: str
    user_id: str | None
    email: str
    company: str | None
    seats: int | None
    message: str | None
    source: str
    current_tier: str | None
    handled_at: datetime | None
    created_at: datetime


class ChatQuota(Response):
    key: str
    limit: int | None
    used: int
    remaining: int | None


class ChatReply(Response):
    model: str
    reply: str
    quota: ChatQuota
