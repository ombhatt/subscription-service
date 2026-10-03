"""The row lock that serialises concurrent syncs for one customer.

The unit suite runs on SQLite, which silently omits `FOR UPDATE` -- that is why
the rest of the tests still work, and also why they cannot see the lock in what
SQLite runs. So these capture the statements the sync path hands to
`session.execute` and compile them for Postgres, which is where the lock exists.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.config import get_settings
from app.models import Subscription
from app.services import subscriptions
from app.services.entitlements import commit_and_invalidate


def _as_postgres(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def test_sync_locks_the_row_before_asking_stripe(session, stripe, monkeypatch):
    """Locking after the Stripe call would let two workers fetch the same stale
    answer and merely serialise the writes, which fixes nothing."""
    order: list[str] = []
    original_execute = session.execute

    async def recording_execute(statement, *args, **kwargs):
        order.append(_as_postgres(statement))
        return await original_execute(statement, *args, **kwargs)

    async def recording_fetch(customer_id):
        order.append("stripe.fetch")
        return await stripe.fetch_current_subscription(customer_id)

    session.add(Subscription(user_id="u1", stripe_customer_id="cus_lock"))
    await session.commit()
    stripe.set_subscription("cus_lock", status="active", price_id="price_pro_m")
    monkeypatch.setattr(session, "execute", recording_execute)
    monkeypatch.setattr(subscriptions.stripe_client, "fetch_current_subscription", recording_fetch)

    sub = await subscriptions.sync_subscription_from_stripe(session, stripe_customer_id="cus_lock")
    await commit_and_invalidate(session)

    lookup = order[0]
    assert "FROM subscriptions" in lookup and "stripe_customer_id" in lookup, order
    assert lookup.rstrip().endswith("FOR UPDATE"), lookup
    assert order.index("stripe.fetch") > 0
    assert (sub.tier, sub.status) == ("pro", "active")


async def test_the_unlocked_lookup_takes_no_row_lock(session, monkeypatch):
    executed: list[str] = []
    original_execute = session.execute

    async def recording_execute(statement, *args, **kwargs):
        executed.append(_as_postgres(statement))
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", recording_execute)

    assert await subscriptions._find_by_customer(session, "cus_none") is None
    assert len(executed) == 1
    assert "FOR UPDATE" not in executed[0]


class _PostgresSession:
    """Records what reaches `execute` while reporting a Postgres bind, which is
    the only way to reach the lock-timeout branch on a SQLite suite."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict | None]] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement, params=None):
        self.executed.append((_as_postgres(statement), params))
        return SimpleNamespace(scalar_one_or_none=lambda: None)


async def test_the_lock_wait_is_bounded_on_postgres(monkeypatch):
    """A waiter must give up rather than block for as long as Stripe takes.

    The holder of this lock is inside a network call, so an unbounded wait means
    every queued event for that customer holds a connection until Stripe answers.
    Postgres reads `lock_timeout` as milliseconds.
    """
    monkeypatch.setattr(get_settings(), "db_lock_timeout_seconds", 2.5)
    session = _PostgresSession()

    await subscriptions._find_by_customer(session, "cus_1", lock=True)

    (bound_sql, bound_params), (lookup_sql, _) = session.executed
    assert "set_config('lock_timeout', %(value)s, true)" in bound_sql
    assert bound_params == {"value": "2500"}
    assert lookup_sql.rstrip().endswith("FOR UPDATE")


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
