"""The row lock that serialises concurrent syncs for one customer.

The unit suite runs on SQLite, which silently omits `FOR UPDATE` -- that is why
the rest of the tests still work, and also why they cannot prove the lock is
there. So this checks the statement we build, and `integration/` proves the
behaviour against real Postgres.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.models import Subscription


def _statement(lock: bool):
    stmt = select(Subscription).where(Subscription.stripe_customer_id == "cus_1")
    return stmt.with_for_update() if lock else stmt


def test_the_locked_lookup_asks_postgres_for_a_row_lock():
    sql = str(_statement(lock=True).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_the_unlocked_lookup_does_not():
    sql = str(_statement(lock=False).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in sql


def test_sqlite_drops_the_clause_rather_than_failing():
    """If SQLite raised on FOR UPDATE, every test using the sync path would
    break the moment the lock was added."""
    sql = str(_statement(lock=True).compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in sql
    assert "SELECT" in sql


async def test_sync_takes_the_lock(session, stripe, monkeypatch):
    """The lock has to be requested before Stripe is asked anything.

    Locking afterwards would let two workers fetch the same stale answer and
    merely serialise the writes, which fixes nothing.
    """
    from app.services import subscriptions

    order: list[str] = []
    original_find = subscriptions._find_by_customer

    async def watched_find(session_, customer_id, *, lock=False):
        order.append(f"select(lock={lock})")
        return await original_find(session_, customer_id, lock=lock)

    async def watched_fetch(customer_id):
        order.append("stripe.fetch")
        return stripe.subscriptions.get(customer_id)

    monkeypatch.setattr(subscriptions, "_find_by_customer", watched_find)
    monkeypatch.setattr(subscriptions.stripe_client, "fetch_current_subscription", watched_fetch)

    session.add(Subscription(user_id="u1", stripe_customer_id="cus_lock"))
    await session.commit()
    stripe.set_subscription("cus_lock", status="active", price_id="price_pro_m")

    await subscriptions.sync_subscription_from_stripe(session, stripe_customer_id="cus_lock")
    await session.commit()

    assert order[0] == "select(lock=True)", f"lock must come first, got {order}"
    assert "stripe.fetch" in order
    assert order.index("select(lock=True)") < order.index("stripe.fetch")
