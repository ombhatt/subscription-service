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

Spending (`spend`) locks the user's spendable batches with a plain
`FOR UPDATE` under a short `lock_timeout`. Not `SKIP LOCKED`: with one batch,
a concurrent spender would skip the only row and see no credits at all.

Nothing in this module commits. Callers own the transaction.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import timeutil
from app.config import get_settings
from app.models import CreditBatch, CreditLedgerEntry, CreditLedgerKind, CreditSource
from app.plans import CREDIT_VALIDITY_MONTHS
from app.timeutil import as_utc


class CreditsExhausted(Exception):
    """Not enough unexpired credits to pay. Carries what the user does hold."""

    def __init__(self, balance: int) -> None:
        self.balance = balance
        super().__init__(f"not enough credits: {balance} left")


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


async def spend(session: AsyncSession, *, user_id: str, credits: int, reference: str) -> int:
    """Take `credits` from the user's oldest unexpired batches; return what is left.

    Raises `CreditsExhausted` and changes nothing if the user holds fewer.
    Writes one `spend` ledger row per batch it takes from. Flushes; does not
    commit -- the caller's commit is what makes the spend real.

    Every spendable batch of the user is locked, not just the oldest, because
    one spend may take from several: a cost above one, or an oldest batch with
    less left than the cost. Taking them in one sorted statement locks them in
    the same order for every spender, so concurrent spends cannot deadlock, and
    costs nothing extra -- concurrent spenders queue on the oldest batch anyway.
    A request that waited on a batch another one emptied does not see it:
    Postgres re-checks `remaining > 0` on the newly locked row and drops it.

    A lock wait past `credit_lock_timeout_seconds` raises the driver's
    lock-not-available error, which the caller turns into a refusal.
    """
    if credits <= 0:
        raise ValueError("a spend must take credits")
    await _bound_lock_wait(session)
    result = await session.execute(
        select(CreditBatch)
        .where(
            CreditBatch.user_id == user_id,
            CreditBatch.remaining > 0,
            CreditBatch.expires_at > timeutil.utcnow(),
        )
        # Oldest grant or purchase first.
        .order_by(CreditBatch.created_at, CreditBatch.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    held = list(result.scalars().all())
    total = sum(batch.remaining for batch in held)
    if total < credits:
        raise CreditsExhausted(total)

    owed = credits
    for batch in held:
        take = min(owed, batch.remaining)
        batch.remaining -= take
        session.add(
            CreditLedgerEntry(
                user_id=user_id,
                batch_id=batch.id,
                kind=CreditLedgerKind.SPEND.value,
                amount=-take,
                reference=reference,
            )
        )
        owed -= take
        if owed == 0:
            break
    await session.flush()
    return total - credits


async def _bound_lock_wait(session: AsyncSession) -> None:
    """Limit how long the next statements wait for a row lock, for this
    transaction only. `set_config(..., true)` is transaction-local."""
    bind = session.get_bind()
    if getattr(bind, "dialect", None) is None or bind.dialect.name != "postgresql":
        return  # SQLite has no row locks to wait for
    milliseconds = int(get_settings().credit_lock_timeout_seconds * 1000)
    await session.execute(
        text("select set_config('lock_timeout', :value, true)"),
        {"value": str(milliseconds)},
    )
