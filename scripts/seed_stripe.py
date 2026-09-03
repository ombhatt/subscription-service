"""Create the products and prices this service expects, in whichever Stripe
account STRIPE_SECRET_KEY points at.

Checked into the repo on purpose: clicking prices together in the dashboard
means test and live drift, and the difference only surfaces when a real customer
checks out. Re-running is safe -- products are created with fixed ids and prices
are looked up by `lookup_key`.

Every price is stamped with `metadata.tier`, which is what lets a subscriber on
a price you have since retired keep resolving to the right tier.

    python -m scripts.seed_stripe            # create/verify, print env lines
    python -m scripts.seed_stripe --dry-run  # show what would be created
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import stripe

from app.config import get_settings
from app.plans import BillingInterval, Tier

# Amounts live here rather than in plans.py because Stripe owns pricing once
# these exist; edit, re-run, and the new price becomes the one new customers
# get, while existing subscribers stay on the old one.
AMOUNTS: dict[tuple[Tier, BillingInterval], int] = {
    (Tier.PLUS, BillingInterval.MONTHLY): 2000,  # $20.00
    (Tier.PLUS, BillingInterval.ANNUAL): 20000,  # $200.00
    (Tier.PRO, BillingInterval.MONTHLY): 10000,  # $100.00
    (Tier.PRO, BillingInterval.ANNUAL): 100000,  # $1000.00
}

CURRENCY = "usd"
PRODUCT_IDS = {Tier.PLUS: "tier_plus", Tier.PRO: "tier_pro"}
PRODUCT_NAMES = {Tier.PLUS: "Plus", Tier.PRO: "Pro"}
INTERVALS = {BillingInterval.MONTHLY: "month", BillingInterval.ANNUAL: "year"}
ENV_NAMES = {
    (Tier.PLUS, BillingInterval.MONTHLY): "STRIPE_PRICE_PLUS_MONTHLY",
    (Tier.PLUS, BillingInterval.ANNUAL): "STRIPE_PRICE_PLUS_ANNUAL",
    (Tier.PRO, BillingInterval.MONTHLY): "STRIPE_PRICE_PRO_MONTHLY",
    (Tier.PRO, BillingInterval.ANNUAL): "STRIPE_PRICE_PRO_ANNUAL",
}


def ensure_product(tier: Tier, dry_run: bool) -> str:
    product_id = PRODUCT_IDS[tier]
    try:
        product = stripe.Product.retrieve(product_id)
        print(f"  product {product_id}: exists")
        return product["id"]
    except stripe.InvalidRequestError:
        pass

    if dry_run:
        print(f"  product {product_id}: would create")
        return product_id

    product = stripe.Product.create(
        id=product_id,
        name=PRODUCT_NAMES[tier],
        metadata={"tier": tier.value},
    )
    print(f"  product {product_id}: created")
    return product["id"]


def ensure_price(tier: Tier, interval: BillingInterval, product_id: str, dry_run: bool) -> str:
    lookup_key = f"{tier.value}_{interval.value}"
    existing = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
    if existing["data"]:
        price = existing["data"][0]
        print(f"  price {lookup_key}: exists ({price['id']})")
        return price["id"]

    if dry_run:
        print(f"  price {lookup_key}: would create")
        return "price_would_be_created"

    price = stripe.Price.create(
        product=product_id,
        currency=CURRENCY,
        unit_amount=AMOUNTS[(tier, interval)],
        recurring={"interval": INTERVALS[interval]},
        lookup_key=lookup_key,
        # The fallback that keeps grandfathered subscribers resolving correctly
        # after this price is retired.
        metadata={"tier": tier.value, "interval": interval.value},
    )
    print(f"  price {lookup_key}: created ({price['id']})")
    return price["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    # The two ways this is wrong in practice: no .env at all, or the template's
    # placeholder still in place. Neither deserves a bare "not set".
    if not settings.stripe_secret_key or settings.stripe_secret_key.endswith("_xxx"):
        env_exists = Path(".env").exists()
        print(
            "STRIPE_SECRET_KEY is not set.\n"
            + (
                "  No .env file found -- copy the template:  cp .env.example .env\n"
                if not env_exists
                else "  .env still has the placeholder value.\n"
            )
            + "  Then put a test key from https://dashboard.stripe.com/test/apikeys\n"
            "  into .env as STRIPE_SECRET_KEY=sk_test_...\n"
            "  (run from the project root -- .env is read relative to the working directory)",
            file=sys.stderr,
        )
        return 1
    if settings.stripe_secret_key.startswith("sk_live"):
        confirm = input("This is a LIVE key. Type 'live' to continue: ")
        if confirm != "live":
            return 1

    stripe.api_key = settings.stripe_secret_key
    if settings.stripe_api_version:
        stripe.api_version = settings.stripe_api_version

    env_lines: list[str] = []
    for tier in (Tier.PLUS, Tier.PRO):
        print(f"{tier.value}:")
        product_id = ensure_product(tier, args.dry_run)
        for interval in BillingInterval:
            price_id = ensure_price(tier, interval, product_id, args.dry_run)
            env_lines.append(f"{ENV_NAMES[(tier, interval)]}={price_id}")

    print("\nAdd these to .env:\n")
    print("\n".join(env_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
