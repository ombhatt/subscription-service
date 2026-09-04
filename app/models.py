from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


# Portable JSON: JSONB on Postgres, plain JSON on SQLite (used by the test suite).
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


class SubscriptionStatus(StrEnum):
    """Internal status. Deliberately *not* Stripe's vocabulary.

    Only TRIALING, ACTIVE and PAST_DUE grant paid entitlements -- PAST_DUE keeps
    access until the grace window closes, because most failed renewals are
    expired cards and cutting a willing payer off converts them to churn.
    """

    FREE = "free"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    INCOMPLETE = "incomplete"
    PAUSED = "paused"


PAID_STATUSES = (
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
)


class Subscription(Base):
    """One row per user, for the life of the account -- created at signup on the
    free tier and updated in place. History lives in SubscriptionAudit.

    The uniqueness of user_id is what makes a double-clicked checkout unable to
    leave a customer holding two subscriptions.
    """

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    tier: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="free")

    # Unique so a Stripe customer can never fan out to two local users; NULL for
    # everyone who has not reached checkout, which both engines allow to repeat.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True, unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(64))
    billing_interval: Mapped[str | None] = mapped_column(String(16))

    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set the first time we see a failed renewal; the grace window counts from here.
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when a chargeback arrives. Support looks at this before issuing anything.
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Mirrored from Stripe so the billing page and support can see a discount
    # without an API call. A promotion code produces one of these; the shape
    # is whatever stripe_client.subscription_discount() flattens it into.
    discount: Mapped[dict | None] = mapped_column(JSONType)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProcessedEvent(Base):
    """Webhook de-duplication.

    The provider's event id is inserted *before* the event is handled; a primary
    key conflict means we have already seen it. Processors deliver at least once.
    """

    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageCounter(Base):
    """Durable mirror of the Redis counters.

    Redis is the enforcement path because it is atomic and fast; this table is
    what support and analytics query, and what survives a cache flush.
    """

    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("user_id", "key", "window_start", name="uq_usage_window"),
        Index("ix_usage_user_key", "user_id", "key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EntitlementGrant(Base):
    """A tier handed out by a human: comps, support make-goods, staff accounts.

    Resolution takes the higher of (subscription tier, active grant tier), so a
    grant can lift a free user to Pro without inventing a fake subscription.
    """

    __tablename__ = "entitlement_grants"
    __table_args__ = (Index("ix_grants_user_active", "user_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionAudit(Base):
    """Append-only. Every state transition, and the event that caused it.

    When a customer says "I was charged after I cancelled", this table is the
    answer, and it is the reason nothing else in this schema needs history.
    """

    __tablename__ = "subscription_audit"
    __table_args__ = (Index("ix_audit_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    from_tier: Mapped[str | None] = mapped_column(String(32))
    to_tier: Mapped[str | None] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    stripe_event_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
