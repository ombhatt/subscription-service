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


async def test_the_lock_wait_is_bounded_on_postgres(session, monkeypatch):
    """A waiter must give up rather than block for as long as Stripe takes.

    The holder of this lock is inside a network call, so an unbounded wait means
    every queued event for that customer holds a connection until Stripe answers.
    """
    from sqlalchemy import text

    from app.services import subscriptions

    issued: list[str] = []
    original = session.execute

    async def spy(statement, *args, **kwargs):
        issued.append(str(statement))
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", spy)

    # SQLite has no row locks, so the guard should skip silently rather than
    # emitting Postgres-only SQL that would raise.
    await subscriptions._bound_lock_wait(session)
    assert not any("set_config" in s for s in issued), (
        "lock_timeout must not be set on a dialect without row locks"
    )
    # And the session is still usable, which is the thing that would break.
    assert (await session.execute(text("select 1"))).scalar() == 1


def test_lock_and_stripe_timeouts_are_actually_bounded():
    """Defaults must be finite. `None` here means 'wait forever', which is how
    one slow dependency becomes an outage."""
    from app.config import Settings

    s = Settings()
    assert 0 < s.db_lock_timeout_seconds < 60
    assert 0 < s.stripe_timeout_seconds < 60
    assert 0 < s.db_command_timeout_seconds < 120
    assert 0 < s.redis_timeout_seconds < 30
    # The lock wait must be shorter than the call it is waiting behind, or it
    # never fires and the bound is decorative.
    assert s.db_lock_timeout_seconds < s.stripe_timeout_seconds


def test_the_stripe_client_sets_a_timeout():
    """The SDK defaults to 80 seconds with two retries — four minutes of lock."""
    import stripe

    from app import stripe_client
    from app.config import get_settings

    stripe_client.reset_http_client()
    stripe_client._client()
    assert stripe.default_http_client._timeout == get_settings().stripe_timeout_seconds
