"""Close the dunning grace window.

Deliberately does *not* touch Stripe. Stripe owns the retry schedule and may
still recover the payment; this job only revokes local access and writes the
audit line, so a late success re-grants the tier through the normal sync.

The read path applies the same rule live (see `policy.grace_expired`), so this
job is about durability and reporting, not about being the thing that stops a
lapsed subscriber -- it is not a hole if it is late.

Run nightly:  python -m app.jobs.expire_grace
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import dispose_engine, get_sessionmaker
from app.models import Subscription, SubscriptionStatus
from app.observability import configure_logging
from app.observability import event as log_event
from app.plans import Tier
from app.policy import grace_expired
from app.services import audit
from app.services.entitlements import invalidate_entitlements

log = logging.getLogger(__name__)


async def expire_grace_windows(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.PAST_DUE.value,
            Subscription.tier != Tier.FREE.value,
        )
    )
    expired: list[str] = []
    for sub in result.scalars().all():
        if not grace_expired(sub):
            continue
        before = audit.snapshot(sub)
        sub.tier = Tier.FREE.value
        await audit.record(
            session,
            user_id=sub.user_id,
            before=before,
            after=audit.snapshot(sub),
            reason="dunning.grace_expired",
            detail={"past_due_since": str(sub.past_due_since)},
        )
        expired.append(sub.user_id)

    if expired:
        await session.commit()
        for user_id in expired:
            await invalidate_entitlements(user_id)
    return expired


async def main() -> None:
    from app.config import get_settings

    configure_logging(json_logs=get_settings().log_json)
    async with get_sessionmaker()() as session:
        expired = await expire_grace_windows(session)
    log_event(log, "grace.expired", count=len(expired), user_ids=expired)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
