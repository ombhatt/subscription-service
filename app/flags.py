"""Feature flags.

Flags are how a change ships dark and gets turned on deliberately, and how a
bad one gets turned off in seconds rather than in a deploy cycle. GrowthBook
holds the rules; this module is the only thing in the service that talks to it.

Three properties matter, and the wrapper exists for the first one:

**It cannot fail the request.** The SDK is fail-*closed*: when `initialize()`
cannot reach GrowthBook it returns False and every later evaluation raises
`RuntimeError: GrowthBook client not properly initialized`. Called directly from
a request handler that is a 500 on every path that reads a flag, so an outage in
the flag service would take down the app it exists to protect. Everything here
is wrapped, and every failure returns the compiled-in default.

**The default lives in code.** `DEFAULTS` below is the reviewed, checked-in
intent; GrowthBook is an override. That keeps the answer to "what does this do
by default" in the repository, under the same review as everything else, rather
than only in a SaaS dashboard.

**Evaluation is local.** The SDK holds an immutable snapshot refreshed in the
background, so `is_enabled()` is an in-process dict lookup -- measured at ~4
microseconds -- and never network I/O in the request path. That is the whole
reason this is safe to call from a hot handler.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.observability import event as log_event
from app.observability import flag_evaluations

try:
    from growthbook import GrowthBookClient, Options, UserContext
except ImportError:  # pragma: no cover - the package is pinned in the lock
    GrowthBookClient = Options = UserContext = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# the flags themselves
# --------------------------------------------------------------------------

# Every flag, with the value used when GrowthBook says nothing -- because it is
# unreachable, not configured, or does not know the flag.
#
# A flag missing from here is a bug: `is_enabled()` would fall back to False for
# a flag that is meant to default on, which is how a kill switch kills the wrong
# thing. Keep an owner and an expiry in the comment; a flag that has been at
# 100% for a month is a flag to delete along with its dead branch.
DEFAULTS: dict[str, Any] = {
    # Kill switch. Off means checkout returns 503 with a clear message instead
    # of failing somewhere inside Stripe. For an incident on their side, where
    # the alternative is every purchase erroring at a random point.
    "checkout-enabled": True,
}


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

_client: Any | None = None


async def init_flags() -> bool:
    """Connect at startup. Never raises, never blocks the app from starting.

    Returning False is a normal, survivable state: the service runs on the
    defaults above. It is logged loudly because "I flipped the flag and nothing
    happened" is otherwise a very confusing afternoon.
    """
    global _client
    settings = get_settings()

    if not settings.growthbook_client_key:
        # The ordinary local and test path. Not a failure.
        log_event(log, "flags.disabled", reason="no client key configured")
        return False

    if GrowthBookClient is None:
        log_event(log, "flags.unavailable", error="growthbook is not installed")
        return False

    try:
        client = GrowthBookClient(
            Options(
                client_key=settings.growthbook_client_key,
                api_host=settings.growthbook_api_host,
                # Bounded like every other outbound call in this service. The
                # refresh is off the request path, but an unbounded one still
                # ties up a connection indefinitely.
                http_connect_timeout=int(settings.growthbook_timeout_seconds),
                http_read_timeout=int(settings.growthbook_timeout_seconds),
            )
        )
        ok = await client.initialize()
    except Exception as exc:  # network, bad key, SDK change -- all survivable
        log_event(log, "flags.unavailable", error=f"{type(exc).__name__}: {exc}"[:200])
        _client = None
        return False

    if not ok:
        log_event(log, "flags.unavailable", error="initialize() returned False")
        # Deliberately dropped rather than kept: an uninitialised client raises
        # on every evaluation, so holding it would mean paying for an exception
        # per call to reach the same default.
        _client = None
        return False

    _client = client
    log_event(log, "flags.ready", flags=sorted(DEFAULTS))
    return True


async def close_flags() -> None:
    global _client
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            log.warning("closing the flag client failed", exc_info=True)
    _client = None


def set_client_for_tests(client: Any | None) -> None:
    """Test seam, matching `app.cache.set_cache`."""
    global _client
    _client = client


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------


async def value(name: str, *, user_id: str | None = None) -> Any:
    """The flag's value for this user, or its compiled-in default.

    `user_id` is the bucketing key. Omit it for a flag that is simply on or off;
    pass it for anything percentage-based, so a given user always lands in the
    same bucket.
    """
    default = DEFAULTS.get(name)
    if name not in DEFAULTS:
        # Loud, because the default is None and that is almost never what the
        # caller wanted.
        log.error("unknown feature flag %r -- add it to app.flags.DEFAULTS", name)
        flag_evaluations.labels(flag=name, source="unknown").inc()
        return None

    if _client is None:
        flag_evaluations.labels(flag=name, source="default").inc()
        return default

    try:
        result = await _client.get_feature_value(
            name, default, UserContext(attributes={"id": user_id or "anonymous"})
        )
        flag_evaluations.labels(flag=name, source="remote").inc()
        return result
    except Exception as exc:
        # The whole point of this module. A flag service problem must never
        # become an application problem.
        log.warning("flag %r evaluation failed, using default %r: %s", name, default, exc)
        flag_evaluations.labels(flag=name, source="error").inc()
        return default


async def is_enabled(name: str, *, user_id: str | None = None) -> bool:
    return bool(await value(name, user_id=user_id))
