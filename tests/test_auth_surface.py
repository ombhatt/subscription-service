"""What is reachable without credentials.

`/v1/webhooks/recent` shipped on the webhooks router, which is unauthenticated
by necessity -- Stripe cannot send an admin key, so `/stripe` is protected by
signature verification instead. The ops endpoint inherited that and served
`processed_events` rows, including the truncated tracebacks in `error`, to
anyone who asked.

The inventory test below exists so the next route added to a public router has
to be a deliberate choice rather than an oversight.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.auth import get_current_user, require_admin
from app.main import app

ADMIN = {"X-Admin-Key": "test-admin-key"}

# Every path that may be reached without credentials, and why.
DELIBERATELY_PUBLIC = {
    ("GET", "/healthz"),  # liveness probe
    ("GET", "/v1/billing/plans"),  # the pricing page, before anyone signs up
    ("GET", "/v1/billing/health"),  # config check for deploy pipelines
    ("POST", "/v1/webhooks/stripe"),  # Stripe cannot authenticate; signed instead
}

AUTH_DEPENDENCIES = {get_current_user, require_admin}


def _is_guarded(dependant) -> bool:
    """True if this route resolves an auth dependency anywhere in its tree."""
    if dependant.call in AUTH_DEPENDENCIES:
        return True
    return any(_is_guarded(child) for child in dependant.dependencies)


def _api_routes(routes):
    """Every APIRoute, descending into included routers.

    `app.routes` does not hold an included router's routes directly -- it holds
    an `_IncludedRouter` wrapper whose real endpoints live under
    `original_router.routes`. Iterating the top level alone finds only what was
    declared on `app` itself, which is why the first version of this test
    inspected exactly one route and cheerfully reported everything was fine.
    Hence `test_the_inventory_actually_sees_the_routes` below.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            inner = getattr(route, "original_router", None)
            nested = getattr(inner, "routes", None) if inner is not None else None
        if nested:
            yield from _api_routes(nested)


def test_the_inventory_actually_sees_the_routes():
    """Guard for the guard: if route discovery silently breaks again, the
    allowlist test would pass while checking nothing."""
    paths = {route.path for route in _api_routes(app.routes)}
    assert len(paths) >= 12, f"route discovery found only {len(paths)}: {sorted(paths)}"
    assert "/v1/entitlements" in paths
    assert "/v1/admin/grants" in paths


def test_no_route_is_accidentally_public():
    unguarded = {
        (method, route.path)
        for route in _api_routes(app.routes)
        for method in route.methods - {"HEAD", "OPTIONS"}
        if not _is_guarded(route.dependant)
    }

    assert unguarded == DELIBERATELY_PUBLIC, (
        "a route is reachable without credentials that is not on the allowlist. "
        "If that is intended, add it to DELIBERATELY_PUBLIC with a reason; if not, "
        "give it a dependency.\n"
        f"  unexpected: {sorted(unguarded - DELIBERATELY_PUBLIC)}\n"
        f"  missing:    {sorted(DELIBERATELY_PUBLIC - unguarded)}"
    )


async def test_the_ops_endpoint_needs_the_admin_key(client):
    """It leaked stack traces before this dependency existed."""
    assert (await client.get("/v1/webhooks/recent")).status_code == 403


async def test_the_ops_endpoint_works_with_the_admin_key(client):
    response = await client.get("/v1/webhooks/recent", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == []


async def test_a_wrong_admin_key_is_refused(client):
    for wrong in ("", "test-admin-ke", "test-admin-keyy", "TEST-ADMIN-KEY"):
        response = await client.get("/v1/webhooks/recent", headers={"X-Admin-Key": wrong})
        assert response.status_code == 403, f"{wrong!r} should not authenticate"


async def test_a_non_ascii_key_is_refused_not_crashed():
    """`secrets.compare_digest` raises TypeError on non-ASCII `str`, so both
    sides are encoded first.

    Called directly rather than over HTTP: httpx refuses to send a non-ASCII
    header at all, so the client can't reach this path -- but a caller that
    isn't httpx can.
    """
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        await require_admin(x_admin_key="café-ключ")
    assert raised.value.status_code == 403


def test_the_schema_is_not_served_in_production():
    """The docs publish every route and model, admin included, with no auth."""
    from app.config import Settings
    from app.main import docs_urls

    production = docs_urls(Settings(environment="production"))
    assert set(production.values()) == {None}, f"docs exposed in production: {production}"

    development = docs_urls(Settings(environment="development"))
    assert development["openapi_url"] == "/openapi.json"
    assert development["docs_url"] == "/docs"
