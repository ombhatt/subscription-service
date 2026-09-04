"""Delete test clocks left behind by an interrupted run.

The suite deletes its own clock in a fixture teardown, but a cancelled or
timed-out CI job kills the process before that runs, and each orphan holds
customers and subscriptions until Stripe expires it 30 days later.

Scoped by name prefix on purpose. The nightly workflow shares a sandbox with
whatever simulations you have open in the Dashboard, so a blind "delete all
clocks" would quietly destroy work in progress.

    python -m scripts.cleanup_test_clocks --prefix ci- --older-than 60
    python -m scripts.cleanup_test_clocks --prefix ci- --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time

import stripe

from app.config import get_settings
from app.stripe_client import _as_dict


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prefix",
        default="ci-",
        help="only delete clocks whose name starts with this (empty matches all)",
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=60,
        help="minutes; skip clocks newer than this so a running job is left alone",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.stripe_secret_key.startswith("sk_test_"):
        print("refusing to run without a Stripe sandbox key", file=sys.stderr)
        return 1
    stripe.api_key = settings.stripe_secret_key

    cutoff = time.time() - args.older_than * 60
    clocks = _as_dict(stripe.test_helpers.TestClock.list(limit=100)).get("data", [])

    deleted = kept = 0
    for clock in clocks:
        name = clock.get("name") or ""
        created = clock.get("created") or 0

        if args.prefix and not name.startswith(args.prefix):
            kept += 1
            continue
        if created > cutoff:
            print(f"  skip   {clock['id']} {name!r} (too recent)")
            kept += 1
            continue

        if args.dry_run:
            print(f"  would delete {clock['id']} {name!r}")
        else:
            try:
                stripe.test_helpers.TestClock.delete(clock["id"])
                print(f"  deleted {clock['id']} {name!r}")
            except Exception as exc:  # already gone, or mid-advance
                print(f"  failed  {clock['id']} {name!r}: {exc}")
                continue
        deleted += 1

    print(f"\n{deleted} deleted, {kept} left alone, {len(clocks)} seen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
