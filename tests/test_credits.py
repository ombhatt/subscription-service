"""Prepaid credits: the admin grant, the ledger, and the balance read.

These are about getting credits in, recording them, and showing them --
including which ones no longer count. Spending them is tests/test_credit_spend.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import CreditBatch, CreditLedgerEntry, Subscription
from app.services import credits
from app.timeutil import as_utc

ADMIN = {"X-Admin-Key": "test-admin-key", "X-Admin-Actor": "support-sam"}
USER = {"X-User-Id": "alice"}


def _batch(*, credits_granted: int, remaining: int, expires_at: datetime, **kw) -> CreditBatch:
    return CreditBatch(
        user_id=kw.pop("user_id", "alice"),
        source=kw.pop("source", "admin"),
        credits_granted=credits_granted,
        remaining=remaining,
        expires_at=expires_at,
        reason=kw.pop("reason", "test"),
        **kw,
    )


async def _ledger(session, user_id: str = "alice") -> list[CreditLedgerEntry]:
    result = await session.execute(
        select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == user_id)
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------
# the admin grant
# --------------------------------------------------------------------------


async def test_an_admin_grant_writes_a_batch_and_its_ledger_entry(client, session, clock):
    response = await client.post(
        "/v1/admin/credits",
        headers=ADMIN,
        json={"user_id": "alice", "credits": 250, "reason": "outage make-good"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["credits"] == 250
    assert body["created_by"] == "support-sam"
    assert body["balance"] == {"balance": 250, "next_expiry_at": body["expires_at"]}

    (batch,) = (await session.execute(select(CreditBatch))).scalars().all()
    assert batch.id == body["batch_id"]
    assert (batch.source, batch.credits_granted, batch.remaining) == ("admin", 250, 250)
    assert batch.reason == "outage make-good"
    # The standard expiry: twelve months from the grant, to the second.
    assert as_utc(batch.expires_at) == datetime(2028, 1, 15, 8, tzinfo=UTC)
    assert batch.stripe_checkout_session_id is None and batch.amount_paid is None

    (entry,) = await _ledger(session)
    assert entry.batch_id == batch.id
    assert (entry.kind, entry.amount) == ("admin", 250)
    assert entry.reference == "admin:support-sam"


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "alice", "credits": 0, "reason": "zero"},
        {"user_id": "alice", "credits": -50, "reason": "a revoke in disguise"},
        {"user_id": "alice", "credits": 100},
        {"user_id": "alice", "credits": 100, "reason": ""},
        {"user_id": "alice", "credits": 100, "reason": "   "},
        {"user_id": "alice", "credits": 50_000_000, "reason": "slipped a few zeros"},
        {"user_id": "", "credits": 100, "reason": "nobody"},
    ],
)
async def test_a_bad_grant_is_refused_and_writes_nothing(client, session, payload):
    response = await client.post("/v1/admin/credits", headers=ADMIN, json=payload)
    assert response.status_code == 422, response.text
    assert (await session.execute(select(CreditBatch))).scalars().all() == []
    assert await _ledger(session) == []


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Key": "wrong"}, USER])
async def test_granting_credits_needs_the_admin_key(client, session, headers):
    response = await client.post(
        "/v1/admin/credits",
        headers=headers,
        json={"user_id": "alice", "credits": 100, "reason": "please"},
    )
    assert response.status_code == 403
    assert (await session.execute(select(CreditBatch))).scalars().all() == []


async def test_a_grant_shows_on_the_next_read_despite_cached_entitlements(client):
    """The balance is read from Postgres, not from the entitlement cache, so a
    grant needs no invalidation to be seen."""
    before = (await client.get("/v1/entitlements", headers=USER)).json()
    assert before["credits"] == {"balance": 0, "next_expiry_at": None}

    await client.post(
        "/v1/admin/credits",
        headers=ADMIN,
        json={"user_id": "alice", "credits": 40, "reason": "goodwill"},
    )
    after = (await client.get("/v1/entitlements", headers=USER)).json()
    assert after["credits"]["balance"] == 40


async def test_support_sees_every_batch_including_used_up_and_expired(client, session, clock):
    session.add(Subscription(user_id="alice"))
    session.add(_batch(credits_granted=10, remaining=0, expires_at=clock.now + timedelta(days=9)))
    session.add(_batch(credits_granted=20, remaining=20, expires_at=clock.now - timedelta(days=1)))
    await session.commit()

    view = (await client.get("/v1/admin/users/alice", headers=ADMIN)).json()
    assert sorted(b["credits_granted"] for b in view["credit_batches"]) == [10, 20]
    assert view["entitlements"]["credits"] == {"balance": 0, "next_expiry_at": None}


# --------------------------------------------------------------------------
# the balance
# --------------------------------------------------------------------------


async def test_no_batches_is_a_zero_balance_with_no_expiry(session):
    assert await credits.balance(session, "alice") == credits.Balance(0, None)


async def test_expired_and_spent_out_batches_do_not_count(session, clock):
    soon = clock.now + timedelta(days=3)
    later = clock.now + timedelta(days=200)
    session.add_all(
        [
            _batch(credits_granted=500, remaining=120, expires_at=later),
            _batch(credits_granted=300, remaining=300, expires_at=soon),
            # Expired an hour ago, still holding credits: gone.
            _batch(credits_granted=900, remaining=900, expires_at=clock.now - timedelta(hours=1)),
            # Expires first, but empty: neither its credits nor its date count.
            _batch(credits_granted=50, remaining=0, expires_at=clock.now + timedelta(days=1)),
            # Someone else's.
            _batch(user_id="bob", credits_granted=7, remaining=7, expires_at=soon),
        ]
    )
    await session.commit()

    held = await credits.balance(session, "alice")
    assert held.credits == 420
    assert held.next_expiry_at == soon


async def test_a_batch_stops_counting_the_moment_it_expires(session, clock):
    expires = clock.now + timedelta(days=30)
    session.add(_batch(credits_granted=100, remaining=100, expires_at=expires))
    await session.commit()

    clock.now = expires - timedelta(seconds=1)
    assert (await credits.balance(session, "alice")).credits == 100
    # Read-time expiry, no job: at `expires_at` it is simply no longer counted.
    clock.now = expires
    assert await credits.balance(session, "alice") == credits.Balance(0, None)


async def test_the_soonest_expiry_moves_on_when_the_first_batch_lapses(client, session, clock):
    first = clock.now + timedelta(days=10)
    second = clock.now + timedelta(days=40)
    session.add_all(
        [
            _batch(credits_granted=10, remaining=10, expires_at=second),
            _batch(credits_granted=5, remaining=5, expires_at=first),
        ]
    )
    await session.commit()

    ents = (await client.get("/v1/entitlements", headers=USER)).json()
    assert ents["credits"]["balance"] == 15
    assert datetime.fromisoformat(ents["credits"]["next_expiry_at"]) == first

    clock.now = first + timedelta(minutes=1)
    ents = (await client.get("/v1/entitlements", headers=USER)).json()
    assert ents["credits"]["balance"] == 10
    assert datetime.fromisoformat(ents["credits"]["next_expiry_at"]) == second


# --------------------------------------------------------------------------
# expiry arithmetic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("granted", "expires"),
    [
        (datetime(2027, 1, 15, 8, tzinfo=UTC), datetime(2028, 1, 15, 8, tzinfo=UTC)),
        # Twelve months is never longer than a year.
        (datetime(2028, 2, 29, 12, tzinfo=UTC), datetime(2029, 2, 28, 12, tzinfo=UTC)),
        (datetime(2027, 12, 31, 23, 59, tzinfo=UTC), datetime(2028, 12, 31, 23, 59, tzinfo=UTC)),
    ],
)
def test_a_batch_expires_twelve_months_after_the_grant(granted, expires):
    assert credits.expiry_for(granted) == expires


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (datetime(2027, 1, 31, tzinfo=UTC), 1, datetime(2027, 2, 28, tzinfo=UTC)),
        (datetime(2027, 11, 30, tzinfo=UTC), 3, datetime(2028, 2, 29, tzinfo=UTC)),
        (datetime(2027, 6, 15, tzinfo=UTC), 18, datetime(2028, 12, 15, tzinfo=UTC)),
    ],
)
def test_adding_months_clamps_to_the_end_of_a_shorter_month(start, months, expected):
    assert credits.add_months(start, months) == expected


async def test_the_service_refuses_a_non_positive_grant(session):
    """The schema is the first line; the writer is the one every caller shares."""
    with pytest.raises(ValueError):
        await credits.grant_admin_credits(
            session, user_id="alice", credits=0, reason="x", actor="a"
        )
