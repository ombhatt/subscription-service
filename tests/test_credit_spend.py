"""Credits pay for messages past the daily cap (#136).

The allowance goes first; only a request Redis has already counted over the cap
spends a credit, from the oldest unexpired batch. Spending fails closed; a
Redis outage still fails open and spends nothing.

Most of this runs on SQLite through `/v1/chat`. The lock behaviour -- the
timeout, and concurrent spenders on one batch -- needs real row locks, so those
tests are marked `postgres` and use the fixture in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import event, select, text

from app import plans
from app.cache import get_cache, set_cache
from app.config import get_settings
from app.errors import QuotaExceeded
from app.models import CreditBatch, CreditLedgerEntry, Subscription
from app.services import quota
from app.services.entitlements import resolve_entitlements
from tests.metrics import counted
from tests.test_cache_outage import DeadCache

MESSAGES = "messages_per_day"
UPLOADS = "file_uploads_per_day"


def _batch(
    clock,
    *,
    user_id: str,
    remaining: int,
    age_days: int,
    expires_in_days: int = 300,
    source: str = "admin",
) -> CreditBatch:
    """A batch granted `age_days` ago. `created_at` is set rather than left to
    the server default, which has one-second resolution on SQLite and would
    make "oldest" a coin toss."""
    created = clock.now - timedelta(days=age_days)
    return CreditBatch(
        user_id=user_id,
        source=source,
        credits_granted=max(remaining, 1),
        remaining=remaining,
        created_at=created,
        expires_at=clock.now + timedelta(days=expires_in_days),
        reason="test",
    )


async def _subscribe(session, user_id: str, tier: str = "plus", status: str = "active") -> None:
    session.add(Subscription(user_id=user_id, tier=tier, status=status))
    await session.commit()


async def _use_up_allowance(session, user_id: str, key: str = MESSAGES) -> None:
    """Put today's counter at the cap, as if every included request had run.

    Sets the counter directly: a Plus user's 300 real requests would test
    nothing more and take a while.
    """
    ents = await resolve_entitlements(session, user_id)
    found = ents.quota(key)
    start, _ = quota.window_for(found.window, ents)
    await get_cache().set(quota._counter_key(user_id, key, start), str(found.limit), 86_400)


async def _remaining(sessionmaker_, user_id: str) -> dict[str, int]:
    """Each batch's `remaining`, keyed by batch id."""
    async with sessionmaker_() as fresh:
        rows = (
            await fresh.execute(select(CreditBatch).where(CreditBatch.user_id == user_id))
        ).scalars()
        return {b.id: b.remaining for b in rows}


async def _spends(sessionmaker_, user_id: str) -> list[CreditLedgerEntry]:
    async with sessionmaker_() as fresh:
        return list(
            (
                await fresh.execute(
                    select(CreditLedgerEntry).where(
                        CreditLedgerEntry.user_id == user_id, CreditLedgerEntry.kind == "spend"
                    )
                )
            ).scalars()
        )


async def _chat(client, user_id: str, model: str = "small"):
    return await client.post(
        "/v1/chat", json={"model": model, "message": "hi"}, headers={"X-User-Id": user_id}
    )


# --------------------------------------------------------------------------
# the cost table
# --------------------------------------------------------------------------


def test_only_messages_cost_credits_in_v1():
    assert plans.credit_cost(MESSAGES) == 1
    assert plans.credit_cost(UPLOADS) is None
    # Every priced quota must be a real one, or the entry is a typo that
    # silently leaves the quota hard-capped.
    keys = {k for tier in plans.CATALOG.values() for k in tier.quotas}
    assert set(plans.CREDIT_COST) <= keys


# --------------------------------------------------------------------------
# spending
# --------------------------------------------------------------------------


async def test_an_over_cap_message_spends_one_credit_from_the_oldest_batch(
    client, session, sessionmaker_, clock
):
    await _subscribe(session, "spender")
    older = _batch(clock, user_id="spender", remaining=5, age_days=30)
    newer = _batch(clock, user_id="spender", remaining=5, age_days=2)
    session.add_all([newer, older])
    await session.commit()
    await _use_up_allowance(session, "spender")
    spent = counted("credit_spends_total", quota=MESSAGES, outcome="spent")

    response = await _chat(client, "spender")

    assert response.status_code == 200, response.text
    assert response.json()["quota"]["remaining"] == 0
    assert await _remaining(sessionmaker_, "spender") == {older.id: 4, newer.id: 5}
    (entry,) = await _spends(sessionmaker_, "spender")
    assert (entry.batch_id, entry.amount, entry.reference) == (
        older.id,
        -1,
        "quota:messages_per_day",
    )
    assert spent() == 1

    # The request ran, so the durable usage mirror records it like any other.
    async with sessionmaker_() as fresh:
        (row,) = await quota.counter_rows(fresh, "spender")
    assert row.count == 1


async def test_a_spent_out_oldest_batch_hands_over_to_the_next(
    client, session, sessionmaker_, clock
):
    await _subscribe(session, "handover")
    older = _batch(clock, user_id="handover", remaining=1, age_days=30)
    newer = _batch(clock, user_id="handover", remaining=3, age_days=2)
    session.add_all([older, newer])
    await session.commit()
    await _use_up_allowance(session, "handover")

    assert (await _chat(client, "handover")).status_code == 200
    assert (await _chat(client, "handover")).status_code == 200

    assert await _remaining(sessionmaker_, "handover") == {older.id: 0, newer.id: 2}
    assert sorted(e.batch_id for e in await _spends(sessionmaker_, "handover")) == sorted(
        [older.id, newer.id]
    )


async def test_requests_under_the_cap_never_touch_credits(
    client, session, sessionmaker_, engine, clock
):
    await _subscribe(session, "under")
    batch = _batch(clock, user_id="under", remaining=10, age_days=1)
    session.add(batch)
    await session.commit()

    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        for _ in range(3):
            response = await _chat(client, "under")
            assert response.status_code == 200, response.text
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert response.json()["quota"]["remaining"] == 297
    assert not [s for s in statements if "credit_" in s], "an under-cap request read credits"
    assert await _remaining(sessionmaker_, "under") == {batch.id: 10}
    assert await _spends(sessionmaker_, "under") == []


async def test_expired_batches_are_skipped_even_with_credits_left(
    client, session, sessionmaker_, clock
):
    await _subscribe(session, "lapsed")
    expired = _batch(clock, user_id="lapsed", remaining=50, age_days=400, expires_in_days=-1)
    live = _batch(clock, user_id="lapsed", remaining=2, age_days=10)
    session.add_all([expired, live])
    await session.commit()
    await _use_up_allowance(session, "lapsed")

    assert (await _chat(client, "lapsed")).status_code == 200
    assert (await _chat(client, "lapsed")).status_code == 200
    refused = await _chat(client, "lapsed")

    assert refused.status_code == 429
    assert refused.json()["credits_remaining"] == 0
    assert await _remaining(sessionmaker_, "lapsed") == {expired.id: 50, live.id: 0}


async def test_a_batch_expiring_now_is_not_spent(client, session, sessionmaker_, clock):
    """Read-time expiry: at `expires_at` the batch is gone, no job involved."""
    await _subscribe(session, "edge")
    batch = _batch(clock, user_id="edge", remaining=5, age_days=10)
    batch.expires_at = clock.now
    session.add(batch)
    await session.commit()
    await _use_up_allowance(session, "edge")

    response = await _chat(client, "edge")

    assert response.status_code == 429
    assert response.json()["credits_remaining"] == 0
    assert await _remaining(sessionmaker_, "edge") == {batch.id: 5}


async def test_an_over_cap_upload_is_refused_even_with_credits(session, sessionmaker_, clock):
    await _subscribe(session, "uploader")
    batch = _batch(clock, user_id="uploader", remaining=100, age_days=1)
    session.add(batch)
    await session.commit()
    await _use_up_allowance(session, "uploader", UPLOADS)
    ents = await resolve_entitlements(session, "uploader")

    with pytest.raises(QuotaExceeded) as refused:
        await quota.consume(session, user_id="uploader", key=UPLOADS, entitlements=ents)

    # Credits cannot pay for uploads, so the 429 does not offer them as a way out.
    assert refused.value.to_payload()["credits_remaining"] is None
    assert await _remaining(sessionmaker_, "uploader") == {batch.id: 100}
    assert await _spends(sessionmaker_, "uploader") == []


async def test_out_of_allowance_and_credits_is_the_existing_429_with_credits_remaining_zero(
    client, session, sessionmaker_, clock
):
    await _subscribe(session, "broke")
    session.add(_batch(clock, user_id="broke", remaining=1, age_days=3))
    await session.commit()
    await _use_up_allowance(session, "broke")
    exhausted = counted("credit_spends_total", quota=MESSAGES, outcome="exhausted")
    rejected = counted("quota_rejections_total", quota=MESSAGES, tier="plus")

    assert (await _chat(client, "broke")).status_code == 200
    response = await _chat(client, "broke")

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "quota_exceeded"
    assert body["credits_remaining"] == 0
    assert (body["quota"], body["limit"], body["remaining"]) == (MESSAGES, 300, 0)
    assert body["upgrade_tier"] == "pro"
    assert (exhausted(), rejected()) == (1, 1)
    assert len(await _spends(sessionmaker_, "broke")) == 1


async def test_with_no_credits_at_all_the_429_says_zero(client, session):
    await _subscribe(session, "never-bought")
    await _use_up_allowance(session, "never-bought")

    response = await _chat(client, "never-bought")

    assert response.status_code == 429
    assert response.json()["credits_remaining"] == 0


async def test_a_cost_above_one_spans_batches_or_spends_nothing(
    client, session, sessionmaker_, clock, monkeypatch
):
    """The cost table is the only knob: a pricier quota takes across batches,
    oldest first, and is refused whole if the balance falls short."""
    monkeypatch.setitem(plans.CREDIT_COST, MESSAGES, 3)
    await _subscribe(session, "pricey")
    older = _batch(clock, user_id="pricey", remaining=2, age_days=20)
    newer = _batch(clock, user_id="pricey", remaining=2, age_days=5)
    session.add_all([older, newer])
    await session.commit()
    await _use_up_allowance(session, "pricey")

    assert (await _chat(client, "pricey")).status_code == 200
    assert await _remaining(sessionmaker_, "pricey") == {older.id: 0, newer.id: 1}
    assert sorted(e.amount for e in await _spends(sessionmaker_, "pricey")) == [-2, -1]

    refused = await _chat(client, "pricey")
    assert refused.status_code == 429
    assert refused.json()["credits_remaining"] == 1
    assert await _remaining(sessionmaker_, "pricey") == {older.id: 0, newer.id: 1}


async def test_a_free_user_after_a_downgrade_spends_leftover_credits(
    client, session, sessionmaker_, clock
):
    """Credits outlive the plan: bought on Plus, still spendable on Free."""
    await _subscribe(session, "downgraded", tier="free", status="free")
    batch = _batch(clock, user_id="downgraded", remaining=2, age_days=60, source="purchase")
    session.add(batch)
    await session.commit()
    await _use_up_allowance(session, "downgraded")

    response = await _chat(client, "downgraded")

    assert response.status_code == 200, response.text
    assert await _remaining(sessionmaker_, "downgraded") == {batch.id: 1}
    assert len(await _spends(sessionmaker_, "downgraded")) == 1


# --------------------------------------------------------------------------
# failing closed, and failing open
# --------------------------------------------------------------------------


async def test_a_database_error_while_spending_refuses_the_request(
    client, session, sessionmaker_, engine, clock
):
    """Fail closed: a credit we cannot record is not given away. The batch is
    untouched -- the decrement and the ledger row commit together or not at all."""
    await _subscribe(session, "unlucky")
    batch = _batch(clock, user_id="unlucky", remaining=10, age_days=1)
    session.add(batch)
    await session.commit()
    await _use_up_allowance(session, "unlucky")
    async with engine.begin() as conn:
        # The ledger insert is the spend's last statement, so its failure must
        # also undo the decrement before it.
        await conn.execute(text("ALTER TABLE credit_ledger RENAME TO credit_ledger_gone"))
    failed = counted("credit_spends_total", quota=MESSAGES, outcome="failed")

    response = await _chat(client, "unlucky")

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "quota_exceeded"
    # Unknown, not zero: the user does have credits.
    assert body["credits_remaining"] is None
    assert failed() == 1
    assert await _remaining(sessionmaker_, "unlucky") == {batch.id: 10}


async def test_redis_down_fails_open_and_spends_no_credit(client, session, sessionmaker_, clock):
    await _subscribe(session, "outage")
    batch = _batch(clock, user_id="outage", remaining=10, age_days=1)
    session.add(batch)
    await session.commit()
    await _use_up_allowance(session, "outage")
    set_cache(DeadCache())
    quota_errors = counted("quota_errors_total", quota=MESSAGES)

    response = await _chat(client, "outage")

    assert response.status_code == 200, response.text
    assert quota_errors() == 1
    assert await _remaining(sessionmaker_, "outage") == {batch.id: 10}
    assert await _spends(sessionmaker_, "outage") == []


# --------------------------------------------------------------------------
# row locks (real Postgres)
# --------------------------------------------------------------------------


async def _seed(postgres, *batches: CreditBatch) -> None:
    async with postgres() as seed:
        seed.add_all(batches)
        await seed.commit()


async def _over_cap(session, user_id: str):
    """A Free user's entitlements with today's allowance already used."""
    ents = await resolve_entitlements(session, user_id)
    await _use_up_allowance(session, user_id)
    return ents


async def _pg_remaining(postgres, user_id: str) -> list[int]:
    async with postgres() as fresh:
        rows = await fresh.execute(
            select(CreditBatch.remaining)
            .where(CreditBatch.user_id == user_id)
            .order_by(CreditBatch.created_at)
        )
        return list(rows.scalars())


async def _hold(postgres, user_id: str):
    """A session holding the row lock on all of `user_id`'s batches."""
    holder = postgres()
    await holder.execute(
        select(CreditBatch).where(CreditBatch.user_id == user_id).with_for_update()
    )
    return holder


@pytest.mark.postgres
async def test_a_spend_that_cannot_get_the_lock_in_time_is_refused(postgres, clock, monkeypatch):
    monkeypatch.setattr(get_settings(), "credit_lock_timeout_seconds", 0.3)
    await _seed(postgres, _batch(clock, user_id="blocked", remaining=5, age_days=1))
    failed = counted("credit_spends_total", quota=MESSAGES, outcome="failed")

    async with postgres() as session:
        ents = await _over_cap(session, "blocked")
        holder = await _hold(postgres, "blocked")
        try:
            started = time.monotonic()
            with pytest.raises(QuotaExceeded) as refused:
                await asyncio.wait_for(
                    quota.consume(session, user_id="blocked", key=MESSAGES, entitlements=ents),
                    timeout=5,
                )
            waited = time.monotonic() - started
        finally:
            await holder.rollback()
            await holder.close()

        assert refused.value.to_payload()["credits_remaining"] is None
        assert 0.25 <= waited < 3, waited
        assert failed() == 1
        assert await _pg_remaining(postgres, "blocked") == [5]

        # Once the lock is free the same user's next request is paid for.
        await quota.consume(session, user_id="blocked", key=MESSAGES, entitlements=ents)
        assert await _pg_remaining(postgres, "blocked") == [4]


@pytest.mark.postgres
async def test_two_concurrent_over_cap_requests_on_one_batch_both_succeed(
    postgres, clock, monkeypatch
):
    """`FOR UPDATE`, not `SKIP LOCKED`: the second request waits for the first
    instead of skipping the only batch and being refused with credits left."""
    monkeypatch.setattr(get_settings(), "credit_lock_timeout_seconds", 5)
    await _seed(postgres, _batch(clock, user_id="twin", remaining=2, age_days=1))

    async with postgres() as setup:
        ents = await _over_cap(setup, "twin")

    async def request():
        async with postgres() as session:
            return await quota.consume(session, user_id="twin", key=MESSAGES, entitlements=ents)

    # Hold the batch so both requests are queued on its lock at once, then let go.
    holder = await _hold(postgres, "twin")
    both = asyncio.gather(request(), request())
    await asyncio.sleep(0.3)
    await holder.rollback()
    await holder.close()
    first, second = await asyncio.wait_for(both, timeout=10)

    assert sorted([first["credits_remaining"], second["credits_remaining"]]) == [0, 1]
    assert await _pg_remaining(postgres, "twin") == [0]
    async with postgres() as fresh:
        ledger = (
            await fresh.execute(
                select(CreditLedgerEntry.amount).where(CreditLedgerEntry.user_id == "twin")
            )
        ).scalars()
        assert sorted(ledger) == [-1, -1]


@pytest.mark.postgres
async def test_a_waiter_whose_batch_runs_dry_moves_on_to_the_next(postgres, clock, monkeypatch):
    """The oldest batch has one credit and two requests want it. The one that
    waited finds it empty once the lock is released, and must pay from the next
    batch rather than be refused while that batch is full."""
    monkeypatch.setattr(get_settings(), "credit_lock_timeout_seconds", 5)
    await _seed(
        postgres,
        _batch(clock, user_id="relay", remaining=1, age_days=20),
        _batch(clock, user_id="relay", remaining=5, age_days=2),
    )
    async with postgres() as setup:
        ents = await _over_cap(setup, "relay")

    async def request():
        async with postgres() as session:
            return await quota.consume(session, user_id="relay", key=MESSAGES, entitlements=ents)

    holder = await _hold(postgres, "relay")
    both = asyncio.gather(request(), request())
    await asyncio.sleep(0.3)
    await holder.rollback()
    await holder.close()
    await asyncio.wait_for(both, timeout=10)

    assert await _pg_remaining(postgres, "relay") == [0, 4]
