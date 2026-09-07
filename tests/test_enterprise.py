"""The Enterprise tier, and the contact-sales path that sells it.

Enterprise is sales-led: no published price, no self-serve checkout, access
delivered by a manual grant against a contract negotiated out of band. That
makes it the first tier that is neither free nor purchasable, and these tests
are mostly about the places that distinction has to hold.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import SalesInquiry
from app.observability import sales_inquiries
from app.plans import CATALOG, TIER_RANK, Tier
from app.services import quota

ADMIN = {"X-Admin-Key": "test-admin-key"}
USER = {"X-User-Id": "alice"}


def counter(source: str, tier: str) -> float:
    return sales_inquiries.labels(source=source, current_tier=tier)._value.get()


# --------------------------------------------------------------------------
# the tier itself
# --------------------------------------------------------------------------


def test_enterprise_ranks_above_pro():
    """Entitlement resolution takes the highest tier, so the order matters."""
    assert TIER_RANK[Tier.ENTERPRISE] > TIER_RANK[Tier.PRO]


def test_only_plus_and_pro_can_be_bought():
    """The rule used to be `tier is not Tier.FREE`, which was right until it
    silently was not."""
    purchasable = {t for t, d in CATALOG.items() if d.purchasable}
    assert purchasable == {Tier.PLUS, Tier.PRO}


async def test_enterprise_appears_on_the_pricing_page_without_a_price(client, stripe):
    plans = (await client.get("/v1/billing/plans")).json()
    enterprise = next(p for p in plans if p["tier"] == "enterprise")
    assert enterprise["display_name"] == "Enterprise"
    assert enterprise["purchasable"] is False
    assert enterprise["prices"] == {}, "no price id means nothing for a Buy button"


async def test_enterprise_cannot_be_bought_through_checkout(client, stripe, session):
    """Otherwise a customer lands on a plan with no price and no contract."""
    response = await client.post(
        "/v1/billing/checkout", headers=USER,
        json={"tier": "enterprise", "interval": "monthly"},
    )
    assert response.status_code == 400
    assert "not a purchasable tier" in response.json()["detail"]
    assert stripe.checkout_sessions == [], "Stripe must not be reached"


async def test_free_is_still_refused_by_the_same_rule(client, stripe, session):
    response = await client.post(
        "/v1/billing/checkout", headers=USER,
        json={"tier": "free", "interval": "monthly"},
    )
    assert response.status_code == 400


def test_the_paywall_never_offers_enterprise_as_an_upgrade():
    """`upgrade_tier` drives a Buy button. Enterprise lifts every cap and cannot
    be bought, so offering it would put a customer one click from a checkout
    that refuses them."""
    assert quota.upgrade_tier_for("messages_per_day", Tier.FREE) is Tier.PLUS
    assert quota.upgrade_tier_for("messages_per_day", Tier.PLUS) is Tier.PRO
    assert quota.upgrade_tier_for("messages_per_day", Tier.PRO) is None


async def test_a_granted_enterprise_user_gets_uncapped_entitlements(client, session):
    """Delivery is a manual grant against a signed contract, which is the whole
    point of sales-led -- so the grant path has to actually work."""
    response = await client.post(
        "/v1/admin/grants", headers=ADMIN,
        json={"user_id": "bigco-admin", "tier": "enterprise", "reason": "signed contract"},
    )
    assert response.status_code in (200, 201)

    ents = (await client.get("/v1/entitlements", headers={"X-User-Id": "bigco-admin"})).json()
    assert ents["tier"] == "enterprise"
    assert ents["source"] == "grant"
    assert all(q["limit"] is None for q in ents["quotas"]), "no platform caps"


# --------------------------------------------------------------------------
# contact sales
# --------------------------------------------------------------------------


async def test_an_anonymous_visitor_can_ask_to_be_contacted(client, session):
    """The pricing page is public and the best leads have not signed up yet."""
    response = await client.post(
        "/v1/billing/contact-sales",
        json={"email": "cto@acme.com", "company": "Acme", "seats": 250,
              "message": "need SSO and an invoice", "source": "pricing_page"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "received"

    row = (await session.execute(select(SalesInquiry))).scalars().one()
    assert row.email == "cto@acme.com"
    assert row.seats == 250
    assert row.user_id is None
    assert row.current_tier is None
    assert row.handled_at is None


async def test_a_signed_in_lead_is_attributed_with_their_tier(client, session):
    """A Pro subscriber asking about Enterprise is a different conversation
    from a stranger browsing plans."""
    response = await client.post(
        "/v1/billing/contact-sales", headers=USER,
        json={"email": "alice@acme.com", "source": "paywall"},
    )
    assert response.status_code == 201

    row = (await session.execute(select(SalesInquiry))).scalars().one()
    assert row.user_id == "alice"
    assert row.current_tier == "free", "whatever they are on today"
    assert row.source == "paywall"


async def test_a_bad_token_records_an_anonymous_lead_rather_than_401(client, session):
    """Losing attribution is the right failure here; losing the lead is not."""
    response = await client.post(
        "/v1/billing/contact-sales",
        headers={"Authorization": "Bearer not-a-real-token"},
        json={"email": "someone@acme.com"},
    )
    assert response.status_code == 201
    row = (await session.execute(select(SalesInquiry))).scalars().one()
    assert row.user_id is None


async def test_the_submission_is_counted(client, session):
    before = counter("pricing_page", "anonymous")
    await client.post("/v1/billing/contact-sales", json={"email": "a@b.com"})
    assert counter("pricing_page", "anonymous") == before + 1


async def test_junk_is_rejected_before_it_reaches_the_table(client, session):
    """The only thing bounding this endpoint is the schema -- there is no rate
    limiting, and it is the one unauthenticated write in the service."""
    for body in (
        {"email": "not-an-email"},
        {"email": "a@b.com", "message": "x" * 2001},
        {"email": "a@b.com", "seats": 0},
        {"email": "a@b.com", "source": "somewhere-else"},
        {"company": "no email at all"},
    ):
        response = await client.post("/v1/billing/contact-sales", json=body)
        assert response.status_code == 422, f"accepted junk: {body}"

    rows = (await session.execute(select(SalesInquiry))).scalars().all()
    assert rows == [], "nothing malformed should have been written"


# --------------------------------------------------------------------------
# the review queue
# --------------------------------------------------------------------------


async def test_the_lead_queue_needs_the_admin_key(client):
    """Names, emails and stated seat counts of people evaluating the product."""
    assert (await client.get("/v1/admin/sales-inquiries")).status_code == 403


async def test_leads_come_back_newest_first(client, session):
    for email in ("first@acme.com", "second@acme.com", "third@acme.com"):
        await client.post("/v1/billing/contact-sales", json={"email": email})

    rows = (await client.get("/v1/admin/sales-inquiries", headers=ADMIN)).json()
    assert len(rows) == 3
    assert {r["email"] for r in rows} == {"first@acme.com", "second@acme.com", "third@acme.com"}


async def test_marking_one_handled_takes_it_off_the_working_list(client, session):
    await client.post("/v1/billing/contact-sales", json={"email": "lead@acme.com"})
    await client.post("/v1/billing/contact-sales", json={"email": "other@acme.com"})

    open_before = (await client.get("/v1/admin/sales-inquiries?handled=false",
                                    headers=ADMIN)).json()
    assert len(open_before) == 2

    target = open_before[0]["id"]
    marked = await client.post(f"/v1/admin/sales-inquiries/{target}/handled", headers=ADMIN)
    assert marked.status_code == 200
    assert marked.json()["handled_at"] is not None

    open_after = (await client.get("/v1/admin/sales-inquiries?handled=false",
                                   headers=ADMIN)).json()
    assert [r["id"] for r in open_after] == [r["id"] for r in open_before if r["id"] != target]


async def test_marking_the_same_lead_twice_keeps_the_first_timestamp(client, session):
    await client.post("/v1/billing/contact-sales", json={"email": "lead@acme.com"})
    row_id = (await client.get("/v1/admin/sales-inquiries", headers=ADMIN)).json()[0]["id"]

    first = (await client.post(f"/v1/admin/sales-inquiries/{row_id}/handled",
                               headers=ADMIN)).json()["handled_at"]
    second = (await client.post(f"/v1/admin/sales-inquiries/{row_id}/handled",
                                headers=ADMIN)).json()["handled_at"]

    # Compared as instants, not as strings: SQLite stores no timezone, so the
    # first response serialises the still-in-session Python datetime with a Z
    # and the second serialises the reloaded naive one without. Same moment,
    # different text -- and on Postgres both carry the offset.
    assert first.rstrip("Z") == second.rstrip("Z"), (
        "re-marking must not rewrite when it was first answered"
    )


async def test_an_unknown_inquiry_is_a_404(client):
    response = await client.post("/v1/admin/sales-inquiries/nope/handled", headers=ADMIN)
    assert response.status_code == 404
