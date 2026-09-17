"""Fixed-window rate limiting, for endpoints anyone on the internet can call.

One endpoint needs it today. `POST /v1/billing/contact-sales` is the only
unauthenticated write in the service, and every accepted request inserts a row.
Everything else behind `get_current_user` already costs an attacker a valid
Supabase token, and the metered product endpoints are bounded by quotas.

Two windows, not one. The short window stops a burst; the daily window stops
the patient version of the same script, dripping requests just under the short
limit all day. Both use the atomic INCR-with-TTL that the quota counters use,
so a counter expires with its window and there is no reset job to fall behind.

Fixed windows, not sliding. A caller can send `limit` at the end of one window
and `limit` again at the start of the next. For an abuse control on a lead form
that is an acceptable edge; a sliding window means a sorted set per caller and
considerably more Redis for the same outcome.

**Fails open.** If the cache cannot answer, the request is allowed, logged and
counted. The other choice turns a Redis blip into lost sales leads, which is
worse than a brief window in which a flood gets through -- and a flood still
shows up in `sales_inquiries_total`, which is what that metric is for.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import Request

from app.cache import get_cache
from app.config import get_settings
from app.errors import RateLimited
from app.observability import rate_limit_errors, rate_limit_rejections

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Window:
    """`limit` requests per `seconds`, counted per caller."""

    name: str
    limit: int
    seconds: int


def _now() -> float:
    """Indirection so tests can move the clock instead of sleeping."""
    return time.time()


def client_ip(request: Request) -> str:
    """Who to count against.

    `X-Forwarded-For` is read only when the deployment says a proxy sets it.
    The header is attacker-controlled otherwise, and trusting it would let one
    script present itself as a new caller on every request -- a rate limiter
    that counts a value the caller chooses is not a rate limiter.

    The opposite mistake is just as real: behind a proxy with this off, every
    request carries the proxy's address and the whole internet shares one
    budget. See TRUST_PROXY_HEADERS in DEPLOY.md.
    """
    if get_settings().trust_proxy_headers:
        # Leftmost entry: the client as recorded by the first proxy that saw it.
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if first:
            return first[:45]  # an IPv6 address with a zone index, at most
    return request.client.host if request.client else "unknown"


async def enforce(scope: str, identity: str, windows: tuple[Window, ...]) -> None:
    """Count this request against every window, or raise RateLimited."""
    now = _now()
    cache = get_cache()

    for window in windows:
        if window.limit <= 0:
            continue  # 0 disables the window
        bucket = int(now // window.seconds)
        key = f"rl:{scope}:{window.name}:{identity}:{bucket}"
        try:
            used = await cache.incr(key, window.seconds)
        except Exception:
            rate_limit_errors.labels(scope=scope).inc()
            log.exception("rate limit check failed for %s; allowing the request", scope)
            return

        if used > window.limit:
            rate_limit_rejections.labels(scope=scope, window=window.name).inc()
            retry_after = max(1, int((bucket + 1) * window.seconds - now))
            raise RateLimited(
                scope=scope,
                limit=window.limit,
                window_seconds=window.seconds,
                retry_after=retry_after,
            )


def contact_sales_windows() -> tuple[Window, ...]:
    settings = get_settings()
    return (
        Window("hour", settings.contact_sales_per_hour, 3600),
        Window("day", settings.contact_sales_per_day, 86_400),
    )


async def contact_sales_rate_limit(request: Request) -> None:
    """Dependency, so it runs *before* the body is validated.

    Inside the handler it would only ever see well-formed submissions, and a
    script posting junk would be unlimited -- each attempt still costs a
    request, a JSON parse and a round trip.
    """
    await enforce("contact_sales", client_ip(request), contact_sales_windows())
