"""Subscription rows for the nightly-job tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import Subscription, SubscriptionStatus
from app.plans import Tier


async def seed(session, **kwargs) -> Subscription:
    defaults = {"user_id": "u1", "stripe_customer_id": "cus_1", "tier": Tier.FREE.value,
                "status": SubscriptionStatus.FREE.value}
    sub = Subscription(**{**defaults, **kwargs})
    session.add(sub)
    await session.commit()
    return sub


async def reload(session, sub) -> Subscription:
    await session.refresh(sub)
    return sub


def past_due(days_ago: float) -> dict:
    return {
        "tier": Tier.PRO.value,
        "status": SubscriptionStatus.PAST_DUE.value,
        "past_due_since": datetime.now(UTC) - timedelta(days=days_ago),
    }
