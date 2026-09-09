# Domain model

Terms this codebase uses precisely. Where two things are nearly the same, the
distinction is here because getting it wrong has already cost something.

## Mirrored tier

`subscriptions.tier` — **what Stripe says this customer pays for.**

A mirror of remote state, and `_apply_remote` in `app/services/subscriptions.py`
is its only writer. `reconcile` compares it against Stripe and repairs
divergence; that is the entire purpose of the column and of the
`reconciliation_drift` metric.

Nothing else may write it. A second writer holding a value Stripe never said
is, by definition, drift — and `reconcile` will faithfully undo it.

## Effective tier

What the customer may actually use right now. Derived on every read by
`resolve_entitlements`, never stored.

It is the mirrored tier *plus* the things the mirror cannot express: whether a
dunning grace window has closed (`policy.grace_expired`), and whether a manual
grant outranks the subscription (`_active_grant_tier`). It is what
`GET /v1/entitlements` returns and what every paywall decision reads.

**Why the two are separate.** A grace window closes through the passage of
time, not through a write. No webhook fires; no row changes. So the mirror
cannot represent it, and any attempt to store the answer goes stale with no
event to invalidate it. Derivation on read is not an optimisation to remove
later — it is the only correct place for it.

The cache in front of it caps its own TTL at the grace boundary for the same
reason (`entitlements._ttl_for`): nothing can invalidate an entry when the
thing that changed was the clock.

**This distinction was not written down, and three modules disagreed about it.**
`expire_grace` wrote the mirror to record an effective change; `reconcile`
compared the mirror to Stripe and wrote it back; `resolve_entitlements` ignored
the mirror and re-derived. The two jobs undid each other nightly and held the
drift alert permanently non-zero. No customer was affected — the read path was
right all along — but the alarm for missed webhooks could never fall silent.

## Dunning grace window

The period after a failed renewal during which a subscriber keeps paid access
while Stripe retries. Opens when `status` becomes `past_due`
(`past_due_since` is stamped once and cleared on recovery), and closes
`DUNNING_GRACE_DAYS` later.

`expire_grace` reports that the window has closed; it does not enforce it. The
read path enforces it, on every request, so a lapsed subscriber is not still
paying customers' rates because a nightly job has not fired yet. The job exists
for the audit trail — support needs to answer *"why did I lose access last
night"* — and its audit row records the **effective** transition, not a change
to the mirror.
