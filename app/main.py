from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import stripe
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.auth import require_admin
from app.cache import close_cache
from app.config import get_settings
from app.db import dispose_engine
from app.errors import FeatureNotEntitled, QuotaExceeded
from app.health import readiness
from app.observability import (
    REGISTRY,
    configure_logging,
    new_request_id,
    request_id_var,
)
from app.routers import admin, billing, chat_demo, entitlements, webhooks

configure_logging(
    json_logs=get_settings().log_json,
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_cache()
    await dispose_engine()


def docs_urls(settings) -> dict[str, str | None]:
    """Where the schema and its viewers are served, if at all.

    All three are unauthenticated by construction and publish every route,
    parameter and model -- the admin surface included. Useful while building,
    not something to serve to the internet.
    """
    if settings.is_production:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


app = FastAPI(
    title="Subscription service",
    description="Flat-price Free/Plus/Pro subscriptions on Stripe Billing.",
    version="0.1.0",
    lifespan=lifespan,
    **docs_urls(get_settings()),
)


@app.exception_handler(QuotaExceeded)
async def quota_exceeded_handler(request: Request, exc: QuotaExceeded) -> JSONResponse:
    # 429 with everything the client needs to render the paywall in one shot.
    return JSONResponse(status_code=429, content=exc.to_payload())


@app.exception_handler(FeatureNotEntitled)
async def feature_handler(request: Request, exc: FeatureNotEntitled) -> JSONResponse:
    return JSONResponse(status_code=403, content=exc.to_payload())


@app.exception_handler(stripe.StripeError)
async def stripe_error_handler(request: Request, exc: stripe.StripeError) -> JSONResponse:
    """Never let a Stripe rejection surface as a bare 500.

    Stripe's own message is the actionable part -- "you must have a valid head
    office address to enable automatic tax" tells you exactly what to fix, while
    "Internal Server Error" sends you to the logs. These are configuration and
    request errors, not secrets, so the message is passed through; the request id
    is what Stripe support asks for.
    """
    request_id = getattr(exc, "request_id", None)
    log.error("stripe error on %s: %s (request_id=%s)", request.url.path, exc, request_id)

    if isinstance(exc, stripe.AuthenticationError):
        # Our API key is wrong. The caller did nothing wrong, so this is a 500.
        code = 500
    elif isinstance(exc, (stripe.APIConnectionError, stripe.RateLimitError)):
        code = 503
    else:
        code = 400

    return JSONResponse(
        status_code=code,
        content={
            "error": "stripe_error",
            "type": type(exc).__name__,
            "message": getattr(exc, "user_message", None) or str(exc),
            "request_id": request_id,
        },
    )


@app.middleware("http")
async def correlate_requests(request: Request, call_next):
    """Give every request an id, and put it on every log line it produces.

    Accepts an inbound `X-Request-ID` so a trace started at a load balancer or
    another service carries through, and always echoes it back so a customer
    reporting a problem can quote something you can search for.
    """
    incoming = request.headers.get("x-request-id")
    request_id = incoming if incoming and len(incoming) <= 128 else new_request_id()
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/metrics", tags=["ops"], dependencies=[Depends(require_admin)])
async def metrics() -> Response:
    """Prometheus exposition.

    Behind the admin key rather than open: it is a precise description of your
    traffic and failure rates. Most scrapers can send a header.

    Per-process and in memory, so under several workers a scrape sees one of
    them. `prometheus_client`'s multiprocess mode is the answer when that
    matters; until then, one worker keeps these honest.
    """
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


app.include_router(billing.router)
app.include_router(entitlements.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
app.include_router(chat_demo.router)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    """Liveness. Deliberately checks nothing.

    "Is this process able to serve a request" -- and nothing else. A liveness
    probe that touches the database restarts every instance when the database
    blinks, which is how a recoverable outage becomes a crash loop. Use
    /readyz to decide whether to send traffic here.
    """
    return {"status": "ok", "environment": get_settings().environment}


@app.get("/readyz", tags=["ops"])
async def readyz(response: Response) -> dict:
    """Readiness. Checks the dependencies, and says which one is unhappy.

    503 when Postgres is unreachable; 200 with status "degraded" when only
    Redis is, because the read path survives that and a shared cache failure
    must not empty the load balancer. See app/health.py.
    """
    status, payload = await readiness()
    response.status_code = status
    return payload
