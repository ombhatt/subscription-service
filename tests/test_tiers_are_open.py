"""Adding a tier should take an entry in plans.py and its price variables.

It used to take four named settings in config.py, a hard-coded map in
price_catalog(), a `(Tier.PLUS, Tier.PRO)` loop and a `len(missing) == 4` in
the billing router, and a `tier === "enterprise"` on the pricing page. These
pin the replacements: prices found by naming convention, lists derived from the
catalog, and nothing outside the catalog naming a paid tier.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app import plans
from app.config import Settings
from app.plans import CATALOG, PURCHASABLE_TIERS, BillingInterval, Tier, price_env_name

ROOT = pathlib.Path(__file__).parents[1]


def configured(monkeypatch, ids: dict[str, str]) -> None:
    monkeypatch.setattr(plans, "get_settings", lambda: type("S", (), {"stripe_price_ids": ids})())


def test_price_variables_are_found_by_name_in_the_environment_and_dotenv(tmp_path, monkeypatch):
    """Production sets real environment variables; development uses .env.
    pydantic-settings drops undeclared environment variables on its own, so
    this is the case that would silently lose every price in production."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "STRIPE_PRICE_TEAM_MONTHLY=price_team_from_file\n"
        "STRIPE_PRICE_PLUS_MONTHLY=price_plus_from_file\n"
    )
    monkeypatch.setenv("STRIPE_PRICE_TEAM_ANNUAL", "price_team_from_env")
    monkeypatch.setenv("STRIPE_PRICE_PLUS_MONTHLY", "price_plus_from_env")

    found = Settings(_env_file=dotenv).stripe_price_ids

    assert found["team_monthly"] == "price_team_from_file"
    assert found["team_annual"] == "price_team_from_env"
    assert found["plus_monthly"] == "price_plus_from_env", "the environment wins, as for any field"


def test_an_empty_variable_is_not_a_price(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", "")
    assert "pro_annual" not in Settings(_env_file=None).stripe_price_ids


def test_every_purchasable_tier_and_interval_is_priced_from_its_variable(monkeypatch):
    ids = {
        f"{t.value}_{i.value}": f"price_{t.value}_{i.value}"
        for t in PURCHASABLE_TIERS
        for i in BillingInterval
    }
    configured(monkeypatch, ids)

    catalog = plans.price_catalog()

    assert set(catalog.values()) == {(t, i) for t in PURCHASABLE_TIERS for i in BillingInterval}


def test_a_price_set_for_a_tier_that_is_not_for_sale_does_not_put_it_on_sale(monkeypatch):
    """Enterprise is sales-led. A stray STRIPE_PRICE_ENTERPRISE_MONTHLY must
    not make it buyable through checkout."""
    configured(monkeypatch, {"enterprise_monthly": "price_ent", "free_monthly": "price_free"})

    assert plans.price_catalog() == {}
    assert plans.price_id_for(Tier.ENTERPRISE, BillingInterval.MONTHLY) is None


def test_variable_names_follow_the_convention():
    assert price_env_name(Tier.PLUS, BillingInterval.MONTHLY) == "STRIPE_PRICE_PLUS_MONTHLY"
    assert price_env_name(Tier.PRO, BillingInterval.ANNUAL) == "STRIPE_PRICE_PRO_ANNUAL"


def test_neither_the_free_floor_nor_the_sales_led_tier_is_for_sale():
    assert Tier.FREE not in PURCHASABLE_TIERS and Tier.ENTERPRISE not in PURCHASABLE_TIERS


async def test_the_pricing_page_is_told_which_tiers_are_sales_led(client, stripe):
    plans_payload = (await client.get("/v1/billing/plans")).json()
    assert {p["tier"]: p["sales_led"] for p in plans_payload} == {
        t.value: d.sales_led for t, d in CATALOG.items()
    }
    assert [p["tier"] for p in plans_payload if p["sales_led"]] == ["enterprise"]


@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in [*(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").glob("*")]
        if p.is_file()
        and p.suffix in {".py", ".sh"}
        and p.relative_to(ROOT).as_posix() not in {"app/plans.py", "scripts/seed_stripe.py"}
    ),
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_nothing_outside_the_catalog_names_a_paid_tier(path):
    """Free is the floor every account starts on, so naming it is fine. Naming
    any other tier is a call site a new tier would have to edit.

    plans.py defines the tiers. seed_stripe.py is the one other exception: its
    AMOUNTS are what each price costs, which is data about specific tiers by
    definition."""
    named = re.findall(r"Tier\.(?!FREE\b)[A-Z]+", path.read_text())
    assert not named, f"{path.relative_to(ROOT)} names {sorted(set(named))}"


def test_the_pricing_page_does_not_branch_on_a_paid_tier_by_name():
    page = (ROOT / "web" / "app" / "page.tsx").read_text()
    named = re.findall(r"tier\s*===?\s*\"(?!free\")(\w+)\"", page)
    assert not named, f"page.tsx branches on {named}; use the plan's flags"
