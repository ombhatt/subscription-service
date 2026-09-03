from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.plans import BillingInterval, Tier


class QuotaState(BaseModel):
    key: str
    limit: int | None = Field(description="null means unlimited")
    used: int
    remaining: int | None
    reset_at: datetime


class EntitlementResponse(BaseModel):
    """The only shape the product should ever branch on."""

    user_id: str
    tier: Tier
    display_name: str
    status: str
    source: str = Field(description="subscription | grant | default")
    features: dict[str, Any]
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


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class PortalResponse(BaseModel):
    portal_url: str


class SubscriptionSummary(BaseModel):
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


class GrantRequest(BaseModel):
    user_id: str
    tier: Tier
    reason: str
    expires_at: datetime | None = None


class GrantResponse(BaseModel):
    id: str
    user_id: str
    tier: Tier
    reason: str
    expires_at: datetime | None
    created_by: str
    created_at: datetime


class AuditEntry(BaseModel):
    created_at: datetime
    reason: str
    from_tier: str | None
    to_tier: str | None
    from_status: str | None
    to_status: str | None
    stripe_event_id: str | None
