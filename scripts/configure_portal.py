"""Configure Stripe's customer portal from this repo's plan catalogue.

The portal decides for itself which plans a subscriber may switch between, and
it only knows what its *configuration* lists. Ours listed nothing, so "Update
your subscription" offered the product the customer was already on and no way
to reach the other tier -- the upgrade button led to a dead end.

Configuration lives here rather than in the Dashboard for the same reason
prices do: `plans.py` is the source of truth, this is reviewable, and a fresh
Stripe account can be brought up to the same state by running a script.

    python -m scripts.configure_portal --dry-run
    python -m scripts.configure_portal          # create or update, print the id

Put the printed id in STRIPE_PORTAL_CONFIGURATION_ID. Without it the service
uses whatever the account's default configuration happens to say.
"""

from __future__ import annotations

import argparse
import sys

import stripe

from app.config import get_settings
from app.plans import CATALOG, BillingInterval, price_id_for
from app.stripe_client import _as_dict

# Stamped on the configuration we own, so re-running updates that one instead of
# adding another. The account's Dashboard-made default is left alone.
MARKER = {"managed_by": "subscription-service"}


def switchable_products() -> list[dict]:
    """Every purchasable tier, with both of its intervals.

    Read from the catalogue rather than listed here: a tier added to plans.py
    and seeded into Stripe becomes switchable by re-running this script.
    """
    products = []
    for tier, definition in CATALOG.items():
        if not definition.purchasable:
            continue
        prices = [
            price_id for interval in BillingInterval if (price_id := price_id_for(tier, interval))
        ]
        if not prices:
            print(f"  ! {tier.value}: no price ids configured, skipping")
            continue
        product = _as_dict(stripe.Price.retrieve(prices[0], expand=["product"]))["product"]["id"]
        products.append({"product": product, "prices": prices})
        print(f"  {tier.value}: product {product} with {len(prices)} price(s)")
    return products


def features(products: list[dict]) -> dict:
    return {
        "customer_update": {
            "enabled": True,
            "allowed_updates": ["name", "email", "address", "phone"],
        },
        "invoice_history": {"enabled": True},
        "payment_method_update": {"enabled": True},
        # The app cancels through its own endpoint, but the portal can too, and
        # both must mean the same thing: end of the paid period, never
        # immediately. A portal set to cancel at once would contradict what the
        # app's confirmation screen promises.
        "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
        "subscription_update": {
            "enabled": True,
            "default_allowed_updates": ["price"],
            # Charge the difference now. The alternative (`none`) hands the
            # customer the higher tier and bills for it next month.
            "proration_behavior": "always_invoice",
            "products": products,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.stripe_secret_key:
        print("STRIPE_SECRET_KEY is not set", file=sys.stderr)
        return 1
    stripe.api_key = settings.stripe_secret_key

    mode = "live" if settings.stripe_secret_key.startswith("sk_live_") else "test"
    print(f"account mode: {mode}")
    products = switchable_products()
    if not products:
        print("no purchasable tiers with prices; run scripts/seed_stripe.py first", file=sys.stderr)
        return 1

    existing = next(
        (
            c
            for c in _as_dict(stripe.billing_portal.Configuration.list(limit=100))["data"]
            if c.get("metadata", {}).get("managed_by") == MARKER["managed_by"]
        ),
        None,
    )

    if args.dry_run:
        verb = "update" if existing else "create"
        print(f"would {verb} a portal configuration with {len(products)} switchable product(s)")
        return 0

    if existing:
        config = _as_dict(
            stripe.billing_portal.Configuration.modify(existing["id"], features=features(products))
        )
        print(f"updated {config['id']}")
    else:
        config = _as_dict(
            stripe.billing_portal.Configuration.create(
                features=features(products),
                business_profile={"headline": "Manage your subscription"},
                metadata=MARKER,
            )
        )
        print(f"created {config['id']}")

    print("\nSet this in .env (and in your deployment's environment):")
    print(f"  STRIPE_PORTAL_CONFIGURATION_ID={config['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
