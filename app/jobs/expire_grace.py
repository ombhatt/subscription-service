"""Record that a dunning grace window has closed.

Deliberately does *not* touch Stripe. Stripe owns the retry schedule and may
still recover the payment; a late success re-grants the tier through the normal
sync.

It also, deliberately, no longer writes `subscriptions.tier`.

`subscriptions.tier` is the **mirrored tier** -- what Stripe says this customer
pays for -- and `_apply_remote` is its only writer. What a customer may
actually use is the **effective tier**, derived on every read by
`resolve_entitlements`, which applies `grace_expired` itself. This job writing
`tier = free` made it a second writer of the mirror, holding a value Stripe had
never said, and `reconcile` -- correctly comparing the mirror against Stripe --
saw that as drift and wrote it back. The two ran nightly and undid each other
forever: two audit rows per subscriber per night, and `reconciliation_drift`
pinned above zero, so the alarm built to catch missed webhooks could never
fall silent.

No customer was ever affected, because the read path never trusted the column
in the first place. That is the whole point: the column was not the answer, and
this job was answering with it.

So the job is what its docstring always claimed -- durability and reporting.
It is not the thing that stops a lapsed subscriber; the read path is.

Run nightly:  python -m app.jobs.expire_grace
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import dispose_engine, get_sessionmaker
from app.models import Subscription, SubscriptionAudit, SubscriptionStatus
from app.observability import configure_logging
from app.observability import event as log_event
from app.plans import Tier
from app.policy import grace_expired
from app.services import audit
from app.services.entitlements import invalidate_entitlements

log = logging.getLogger(__name__)

REASON = "dunning.grace_expired"


async def _already_reported(session: AsyncSession, sub: Subscription) -> bool:
    """Have we already written the audit row for *this* dunning cycle?

    Compares timestamps rather than reading `past_due_since` out of the audit
    row's JSON `detail`: plain columns behave identically on the SQLite the
    suite runs on and the Postgres production runs on, and this uses the
    `ix_audit_user_created` index that already exists.

    The semantics are the same. `past_due_since` is stamped once when a
    subscriber enters dunning and cleared when they leave, so "was a row
    written after this cycle began" is exactly "have we reported this cycle".
    """
    if sub.past_due_since is None:
        return False
    found = await session.execute(
        select(SubscriptionAudit.id)
        .where(
            SubscriptionAudit.user_id == sub.user_id,
            SubscriptionAudit.reason == REASON,
            SubscriptionAudit.created_at > sub.past_due_since,
        )
        .limit(1)
    )
    return found.scalar_one_or_none() is not None


async def expire_grace_windows(session: AsyncSession) -> list[str]:
    result = await session.execute(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.PAST_DUE.value,
            # Nothing to revoke from someone the mirror already shows on free.
            # This is a semantic guard, not idempotency -- idempotency is
            # `_already_reported`, because this job no longer changes the row
            # it selects on and so cannot filter itself out by writing to it.
            Subscription.tier != Tier.FREE.value,
        )
    )

    expired: list[str] = []
    for sub in result.scalars().all():
        if not grace_expired(sub):
            continue
        if await _already_reported(session, sub):
            continue

        # The effective tier is what moved: this customer could use Pro
        # yesterday and cannot today. The mirrored tier is untouched and stays
        # whatever Stripe last said. Support's question is "why did I lose
        # access last night", and this row answers it.
        await audit.record(
            session,
            user_id=sub.user_id,
            before=audit.snapshot(sub),
            after=(Tier.FREE.value, sub.status),
            reason=REASON,
            detail={
                "past_due_since": str(sub.past_due_since),
                "mirrored_tier": sub.tier,
                "note": "effective tier only; subscriptions.tier mirrors Stripe and is unchanged",
            },
        )
        expired.append(sub.user_id)

    if expired:
        await session.commit()
        # Redundant for entries written after the TTL cap shipped -- those
        # cannot outlive the boundary. Kept for the ones cached before it, and
        # because an unnecessary DELETE is cheaper than a subscriber holding
        # paid access on a stale key.
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
