from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Subscription, SubscriptionAudit


def snapshot(sub: Subscription) -> tuple[str, str]:
    return sub.tier, sub.status


async def record(
    session: AsyncSession,
    *,
    user_id: str,
    before: tuple[str | None, str | None],
    after: tuple[str | None, str | None],
    reason: str,
    stripe_event_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append a transition. Called for every change, including no-op syncs that
    carry a reason worth keeping (a dispute, an admin override, a reconciliation
    correction)."""
    session.add(
        SubscriptionAudit(
            user_id=user_id,
            from_tier=before[0],
            to_tier=after[0],
            from_status=before[1],
            to_status=after[1],
            reason=reason,
            stripe_event_id=stripe_event_id,
            detail=detail,
        )
    )
