"""Delete subscription rows whose Stripe customer lives in an old sandbox.

After `setup_sandbox.sh` points the service at a new sandbox, rows created
while testing against the old one name customers the new one has never heard
of. They are harmless -- nothing will ever match them -- but they clutter a
development database, so the wizard offers to remove them.

The database behind `.env` may be the Supabase project that also serves paying
customers (issue #68), and a customer id does not say which account or mode it
came from. So a row is deleted only when the **old sandbox's test key** can
retrieve its customer:

* A test key cannot see live-mode customers, so a paying customer never
  matches. A live key is refused outright.
* A customer of the new sandbox, or of any other account, is "No such
  customer" to the old key and is left alone.
* Anything that is not a clear yes or no -- a revoked key after the sandbox was
  reset, a network error -- stops the run before anything is deleted.

    OLD_STRIPE_SECRET_KEY=sk_test_... python -m scripts.prune_sandbox_rows
    OLD_STRIPE_SECRET_KEY=sk_test_... python -m scripts.prune_sandbox_rows --delete

The key comes from the environment rather than an argument so it never shows up
in a process listing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa
import stripe
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Subscription

KEY_VAR = "OLD_STRIPE_SECRET_KEY"
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")

InOldSandbox = Callable[[str], bool]


@dataclass
class Result:
    old: list[str]
    kept: list[str]


def refuse_key(key: str) -> str | None:
    """Why `key` cannot be used to decide what to delete, or None if it can."""
    if key.startswith(TEST_KEY_PREFIXES):
        return None
    if "_live_" in key:
        return "that is a live key; it would match paying customers"
    return "that is not a Stripe secret test key (sk_test_ or rk_test_)"


def stripe_lookup(api_key: str) -> InOldSandbox:
    """Whether the account behind `api_key` has this customer.

    A deleted customer still answers, flagged `deleted`, and it is still that
    account's. Only "No such customer" is a no; any other error propagates.
    """
    client = stripe.StripeClient(api_key)

    def in_old_sandbox(customer_id: str) -> bool:
        try:
            client.v1.customers.retrieve(customer_id)
        except stripe.InvalidRequestError as exc:
            if exc.code == "resource_missing":
                return False
            raise
        return True

    return in_old_sandbox


async def prune(engine: AsyncEngine, in_old_sandbox: InOldSandbox, *, delete: bool) -> Result:
    """Sort every row with a Stripe customer into old-sandbox and kept, and
    delete the old ones if asked.

    Every customer is checked before anything is deleted, so a lookup that
    fails partway leaves the table as it was.
    """
    async with engine.begin() as conn:
        customers = (
            await conn.scalars(
                sa.select(Subscription.stripe_customer_id)
                .where(Subscription.stripe_customer_id.is_not(None))
                .order_by(Subscription.stripe_customer_id)
            )
        ).all()

        result = Result(old=[], kept=[])
        for customer_id in customers:
            found = await asyncio.to_thread(in_old_sandbox, customer_id)
            (result.old if found else result.kept).append(customer_id)

        if delete and result.old:
            await conn.execute(
                sa.delete(Subscription).where(Subscription.stripe_customer_id.in_(result.old))
            )
    return result


async def _run(url: str, api_key: str, *, delete_rows: bool) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import engine_kwargs

    engine = create_async_engine(url, poolclass=NullPool, **engine_kwargs(url))
    try:
        result = await prune(engine, stripe_lookup(api_key), delete=delete_rows)
    except stripe.StripeError as exc:
        print(f"  Stopped, nothing deleted: Stripe said {type(exc).__name__}: {exc}")
        print("  If the old sandbox was reset or deleted its key no longer works, and")
        print("  there is no way left to tell its rows apart. They can stay: they never match.")
        return 1
    finally:
        await engine.dispose()

    verb = "deleted" if delete_rows else "belong to the old sandbox"
    print(f"  {len(result.old)} row(s) {verb}; {len(result.kept)} with another customer left alone")
    if not delete_rows:
        print(f"old_sandbox_rows={len(result.old)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="delete; without it, only count")
    args = parser.parse_args()

    api_key = os.environ.get(KEY_VAR, "")
    if reason := refuse_key(api_key):
        print(f"  {KEY_VAR}: {reason}. Nothing was checked or deleted.", file=sys.stderr)
        return 2

    from app.config import get_settings

    url = get_settings().database_url
    return asyncio.run(_run(url, api_key, delete_rows=args.delete))


if __name__ == "__main__":
    raise SystemExit(main())
