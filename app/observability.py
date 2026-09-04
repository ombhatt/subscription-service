"""Structured logs, request correlation, and the metrics the README promises.

The README has always said to alert on three things — webhook processing lag,
payment failure rate, reconciliation drift. None of them were emitted, which
made that advice unactionable. They are defined here.

Two deliberate limits, so nobody mistakes this for more than it is:

* Metrics are per-process and in memory. Under several workers a scrape hits one
  of them; `prometheus_client`'s multiprocess mode is the fix when that day comes.
* The cron jobs run in their own processes, so a Gauge set there is invisible to
  a scrape of the web process. Rather than ship a gauge that reads zero forever,
  the jobs emit a structured log line and the alert is built on that.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# The id travelling with the current request, so every log line emitted while
# handling it can be tied together without threading a parameter through
# everything.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

REGISTRY = CollectorRegistry()


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

webhook_events = Counter(
    "webhook_events_total",
    "Stripe webhooks received, by type and outcome.",
    ["event_type", "outcome"],  # processed | duplicate | ignored | failed
    registry=REGISTRY,
)

webhook_duration = Histogram(
    "webhook_processing_seconds",
    "Time spent handling a webhook, including the Stripe re-read.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

# Not how long *we* took, but how far behind Stripe we are: the gap between the
# event being created and us finishing with it. This is the number that tells
# you a backlog is forming.
webhook_lag = Histogram(
    "webhook_lag_seconds",
    "Age of a Stripe event when we finished processing it.",
    buckets=(1, 5, 15, 30, 60, 300, 900, 3600),
    registry=REGISTRY,
)

payment_failures = Counter(
    "payment_failures_total",
    "Renewals Stripe told us failed.",
    registry=REGISTRY,
)

entitlement_cache = Counter(
    "entitlement_cache_total",
    "Entitlement resolutions by where the answer came from.",
    ["result"],  # hit | miss | stale
    registry=REGISTRY,
)

quota_rejections = Counter(
    "quota_rejections_total",
    "Requests refused because a quota was exhausted.",
    ["quota", "tier"],
    registry=REGISTRY,
)

subscription_transitions = Counter(
    "subscription_transitions_total",
    "Tier changes, by where they ended up.",
    ["from_tier", "to_tier"],
    registry=REGISTRY,
)

reconciliation_drift = Gauge(
    "reconciliation_drift",
    "Subscriptions that disagreed with Stripe on the last reconciliation run. "
    "Only meaningful if the job shares a process with the scrape target; "
    "otherwise alert on the job's structured log line instead.",
    registry=REGISTRY,
)


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so fields are queryable rather than grepped.

    Written by hand rather than pulling in a dependency: the whole contract is
    a dict and a timestamp, and a log formatter is a poor place for surprises.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Anything passed as `extra={...}` rides along, which is how the
        # structured events below carry their fields.
        for key, value in getattr(record, "context", {}).items():
            payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(*, json_logs: bool = True, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter("%(levelname)-8s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Stripe's SDK logs every request and response at INFO, which drowns
    # everything else. Its warnings still get through.
    logging.getLogger("stripe").setLevel(logging.WARNING)

    # Uvicorn installs its own handlers with propagate=False, so its access and
    # error lines bypass the root handler entirely and come out in a different
    # format. Half-JSON output is worse than none: an aggregator parses some
    # lines and silently drops the rest.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    """Emit a structured event.

    `event(log, "webhook.processed", event_type=..., duration_ms=...)` produces
    a line an aggregator can filter on `event` rather than one you have to
    regex out of prose.
    """
    logger.info(name, extra={"context": {"event": name, **fields}})


def new_request_id() -> str:
    return uuid.uuid4().hex


class Timer:
    """Wall-clock duration of a block, in seconds."""

    def __enter__(self) -> Timer:
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.monotonic() - self._start

    @property
    def elapsed(self) -> float:
        return getattr(self, "seconds", time.monotonic() - self._start)
