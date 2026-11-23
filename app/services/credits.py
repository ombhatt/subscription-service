"""Prepaid credits: batches, the ledger, and the balance read.

Policy in docs/adr/0001-prepaid-credits.md; vocabulary in GLOSSARY.md.

The balance is read from Postgres on every call and never cached. It is not an
entitlement -- the tier is -- so no write here marks anyone or needs
`commit_and_invalidate`. If it is ever cached, every credit write must mark and
commit through `commit_and_invalidate` (see GLOSSARY.md), or a granted or
revoked credit is invisible for a full TTL.

Expiry is read-time: a batch counts while `expires_at > now`. No job enforces
it, so none can fall behind. An `expire` ledger entry may be written later for
the books; nothing here depends on one.

Nothing in this module commits. Callers own the transaction.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import timeutil
from app.models import CreditBatch, CreditLedgerEntry, CreditLedgerKind, CreditSource
from app.plans import CREDIT_VALIDITY_MONTHS
from app.timeutil import as_utc


@dataclass(frozen=True)
class Balance:
    credits: int
    # When the soonest-expiring batch that still holds credits lapses. None when
    # there is nothing to spend.
    next_expiry_at: datetime | None


def add_months(moment: datetime, months: int) -> datetime:
    """Same day and time `months` later, clamped to the end of a shorter month.

    A grant on 29 Feb expires on 28 Feb the next year, not 1 Mar: "12 months"
    should never come out longer than a year.
    """
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def expiry_for(granted_at: datetime) -> datetime:
    return add_months(granted_at, CREDIT_VALIDITY_MONTHS)


async def grant_admin_credits(
    session: AsyncSession, *, user_id: str, credits: int, reason: str, actor: str
) -> CreditBatch:
    """An operator's grant: a batch with the standard expiry, and its ledger row.

    Goodwill, a won dispute, an outage make-good. Flushes so the caller can read
    the batch's id; does not commit.
    """
    if credits <= 0:
        raise ValueError("a grant must add credits")
    batch = CreditBatch(
        user_id=user_id,
        source=CreditSource.ADMIN.value,
        credits_granted=credits,
        remaining=credits,
        expires_at=expiry_for(timeutil.utcnow()),
        reason=reason,
    )
    session.add(batch)
    await session.flush()
    session.add(
        CreditLedgerEntry(
            user_id=user_id,
            batch_id=batch.id,
            kind=CreditLedgerKind.ADMIN.value,
            amount=credits,
            reference=f"admin:{actor}",
        )
    )
    await session.flush()
    return batch


async def balance(session: AsyncSession, user_id: str) -> Balance:
    """Spendable credits now, and when the soonest of them expire.

    Expired batches and spent-out batches are both left out -- an empty batch
    expiring tomorrow is not news.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(CreditBatch.remaining), 0), func.min(CreditBatch.expires_at))
        .where(
            CreditBatch.user_id == user_id,
            CreditBatch.remaining > 0,
            CreditBatch.expires_at > timeutil.utcnow(),
        )
    )
    total, soonest = result.one()
    return Balance(credits=int(total), next_expiry_at=as_utc(soonest))


async def batches(session: AsyncSession, user_id: str) -> list[CreditBatch]:
    """Every batch this user has had, newest first, expired ones included.
    For support, who need to explain where a balance went."""
    result = await session.execute(
        select(CreditBatch)
        .where(CreditBatch.user_id == user_id)
        .order_by(CreditBatch.created_at.desc(), CreditBatch.id)
    )
    return list(result.scalars().all())
