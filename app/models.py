from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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

    @property
    def grants_access(self) -> bool:
        return self in PAID_STATUSES


PAID_STATUSES = (
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
)
_PAID_VALUES = frozenset(s.value for s in PAID_STATUSES)


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

    @property
    def grants_access(self) -> bool:
        """Whether the mirrored status is one that pays for a tier. Not the
        whole answer to "may they use it": the grace window is the read path's
        to apply (see services/entitlements.py)."""
        return self.status in _PAID_VALUES


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


class SalesInquiry(Base):
    """Someone asked to be contacted about Enterprise.

    Durable rather than a log line, because this is a work queue: sales has to
    be able to find who asked, when, and what they wanted. A structured log is
    queryable but nobody works a shift out of a log aggregator.

    `user_id` is nullable on purpose. The pricing page is public, so the most
    valuable inquiries -- someone evaluating before they sign up -- arrive with
    no account attached. Requiring a login here would filter out exactly the
    leads worth having.
    """

    __tablename__ = "sales_inquiries"
    __table_args__ = (Index("ix_sales_inquiry_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    company: Mapped[str | None] = mapped_column(String(200))
    # What they say they need. The single most useful field on this table: it
    # is the evidence for whether Enterprise means "bigger caps" or "seats",
    # which is a schema decision nobody should make on a hunch.
    seats: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(2000))
    # Where the click came from -- the pricing page, or a paywall a subscriber
    # hit. A Pro subscriber who ran out of messages is a different lead from a
    # visitor browsing plans, and the follow-up should differ.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="pricing_page")
    current_tier: Mapped[str | None] = mapped_column(String(32))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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


class CreditSource(StrEnum):
    """Where a credit batch came from."""

    PURCHASE = "purchase"
    ADMIN = "admin"


class CreditLedgerKind(StrEnum):
    """Why credits moved. The sign of the amount follows from the kind:
    `purchase` and `admin` add, everything else takes away."""

    PURCHASE = "purchase"
    SPEND = "spend"
    REFUND = "refund"
    DISPUTE = "dispute"
    ADMIN = "admin"
    EXPIRE = "expire"


class CreditBatch(Base):
    """One grant or purchase of credits, spendable until `expires_at`.

    `remaining` is the spendable balance of this batch; a user's balance is the
    sum of `remaining` over batches that have not expired. Expiry is decided at
    read time, never by a job, so a batch past `expires_at` is simply not
    counted -- there is nothing to fall behind.

    `user_id` has no foreign key, like `subscriptions`: the account lives in
    Supabase, and these rows must survive its deletion for the books.

    The purchase columns (`pack_id`, the Stripe ids, `amount_paid`) stay empty
    on admin batches. `stripe_checkout_session_id` is unique so a replayed grant
    webhook cannot create a second batch for one payment.
    """

    __tablename__ = "credit_batches"
    __table_args__ = (
        CheckConstraint("credits_granted > 0", name="ck_credit_batches_granted_positive"),
        CheckConstraint(
            "remaining >= 0 AND remaining <= credits_granted",
            name="ck_credit_batches_remaining_in_range",
        ),
        CheckConstraint("source IN ('purchase', 'admin')", name="ck_credit_batches_source"),
        Index("ix_credit_batches_user_expires", "user_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    pack_id: Mapped[str | None] = mapped_column(String(32))
    credits_granted: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # In the currency's smallest unit, as Stripe reports it. USD only.
    amount_paid: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CreditLedgerEntry(Base):
    """Append-only. Every movement of credits, signed, and what caused it.

    Finance's record: it survives account deletion, and the running service may
    only insert into it (migration 0008 grants app_service SELECT and INSERT,
    nothing else). `reference` names the cause -- a Stripe event id, or
    `admin:<actor>` for an operator grant.
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_credit_ledger_amount_nonzero"),
        CheckConstraint(
            "kind IN ('purchase', 'spend', 'refund', 'dispute', 'admin', 'expire')",
            name="ck_credit_ledger_kind",
        ),
        Index("ix_credit_ledger_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("credit_batches.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
