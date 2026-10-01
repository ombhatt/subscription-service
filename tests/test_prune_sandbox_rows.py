"""The sandbox wizard's cleanup runs against whatever `.env` points at, which
may be the Supabase project that holds paying customers (issue #68). What it
is willing to delete is the part worth pinning down: only rows the old
sandbox's own test key can find, never a row it cannot vouch for."""

from __future__ import annotations

import pytest
import stripe
from sqlalchemy import select

from app.models import Subscription
from scripts.prune_sandbox_rows import prune, refuse_key

OLD = {"cus_old_1", "cus_old_2"}


def old_sandbox(customer_id: str) -> bool:
    return customer_id in OLD


async def seed(session, **customers: str | None) -> None:
    for user_id, customer_id in customers.items():
        session.add(Subscription(user_id=user_id, stripe_customer_id=customer_id))
    await session.commit()


async def remaining(session) -> set[str]:
    return set((await session.scalars(select(Subscription.user_id))).all())


async def test_only_rows_the_old_sandbox_owns_are_deleted(engine, session):
    await seed(
        session,
        tester1="cus_old_1",
        tester2="cus_old_2",
        paying="cus_live_or_new",
        free=None,
    )

    result = await prune(engine, old_sandbox, delete=True)

    assert sorted(result.old) == ["cus_old_1", "cus_old_2"]
    assert result.kept == ["cus_live_or_new"]
    assert await remaining(session) == {"paying", "free"}


async def test_a_report_deletes_nothing(engine, session):
    await seed(session, tester="cus_old_1", paying="cus_live")

    result = await prune(engine, old_sandbox, delete=False)

    assert result.old == ["cus_old_1"]
    assert await remaining(session) == {"tester", "paying"}


async def test_a_lookup_it_cannot_answer_deletes_nothing(engine, session):
    """A reset sandbox's key stops working, and a network error is not a
    "no". Either way nothing has been proven old, so nothing goes."""
    await seed(session, tester="cus_old_1", other="cus_old_2")

    def broken(customer_id: str) -> bool:
        if customer_id == "cus_old_2":
            raise stripe.AuthenticationError("Invalid API Key provided")
        return True

    with pytest.raises(stripe.AuthenticationError):
        await prune(engine, broken, delete=True)

    assert await remaining(session) == {"tester", "other"}


@pytest.mark.parametrize("key", ["sk_test_abc", "rk_test_abc"])
def test_a_test_key_is_accepted(key):
    assert refuse_key(key) is None


@pytest.mark.parametrize("key", ["", "sk_live_abc", "rk_live_abc", "pk_test_abc", "whatever"])
def test_anything_but_a_test_key_is_refused(key):
    """A live key would find live customers, which is the one thing this must
    never be able to delete."""
    assert refuse_key(key)


class FakeClient:
    """Stands in for stripe.StripeClient: `client.v1.customers.retrieve`."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.v1 = self
        self.customers = self

    def retrieve(self, customer_id):
        outcome = self.outcomes[customer_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def lookup(monkeypatch):
    from scripts import prune_sandbox_rows

    def build(outcomes):
        monkeypatch.setattr(
            prune_sandbox_rows.stripe, "StripeClient", lambda key: FakeClient(outcomes)
        )
        return prune_sandbox_rows.stripe_lookup("sk_test_old")

    return build


def test_a_customer_the_old_key_finds_is_old_even_once_deleted(lookup):
    in_old = lookup({"cus_a": {"id": "cus_a"}, "cus_b": {"id": "cus_b", "deleted": True}})
    assert in_old("cus_a") and in_old("cus_b")


def test_no_such_customer_is_a_no(lookup):
    missing = stripe.InvalidRequestError("No such customer: 'cus_x'", "id", code="resource_missing")
    assert lookup({"cus_x": missing})("cus_x") is False


@pytest.mark.parametrize(
    "error",
    [
        stripe.InvalidRequestError("Invalid request", "id", code="parameter_invalid_string"),
        stripe.AuthenticationError("Invalid API Key provided"),
        stripe.APIConnectionError("Network error"),
    ],
)
def test_anything_else_is_not_an_answer(lookup, error):
    with pytest.raises(type(error)):
        lookup({"cus_x": error})("cus_x")
