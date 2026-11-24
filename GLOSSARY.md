# Domain model

Terms this codebase uses precisely. Where two things are nearly the same, the
distinction is here because getting it wrong has already cost something.

## Mirrored tier

`subscriptions.tier` — **what Stripe says this customer pays for.**

A mirror of remote state, decided in `app/services/subscriptions.py` by
`project` (Stripe's answer in the shape of the row, a pure function) and
written by `_write`, its only writer. `reconcile` compares it against Stripe
through the same `project` and repairs divergence; that is the entire purpose
of the column and of the `reconciliation_drift` metric.

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

## Marking, and commit-and-invalidate

A write that changes what someone may do **marks** them
(`mark_entitlements_stale`) and commits through `commit_and_invalidate`. It
does not clear the cache itself.

The ordering is the reason. Clearing the cache before the transaction commits
opens a window in which a concurrent reader sees the pre-commit row and caches
it — so the stale answer *outlives* the write by a full TTL. Measured against
real Postgres and Redis, in a single process with one event loop:

    writer: sync() done — flushed and invalidated, NOT committed
    reader: resolved tier='free' and cached it for 60s
    writer: COMMIT
            database says pro, the API serves free

A customer paid, the webhook landed, and they sat on free for the next minute —
inside the window that "access is granted on the webhook, never on the
redirect" exists to protect. `WEB_CONCURRENCY=1` is no defence: the `await` on
the Stripe call inside `sync` is a yield point.

Marks live on `session.info`, so a write path several frames below the commit
can register without threading a return value up through every layer — which is
how the old code ended up reaching for the cache from inside the write.
`session.info` is not transactional, so an `after_rollback` listener discards
them; a rolled-back write changes nothing and invalidates nothing.

A plain `session.commit()` under a marking write drops the marks silently. An
`after_commit` listener logs that and counts it as
`entitlement_invalidations_total{outcome="undrained"}`. It only detects — curing
it there would need a scheduled task that can fail quietly, which is the failure
being removed.

## Orphaned subscription

A subscription **Stripe can still charge** whose **Supabase account no longer
exists** — typically someone deleted from the Supabase dashboard, since the
service has no account deletion of its own. Nothing tells Stripe, so it keeps
renewing, and the only user who could see it or cancel it can no longer sign in.

`reconcile` finds these through `app_private.missing_accounts` (migration 0006),
which answers with ids only: the service still cannot read `auth.users`. It
writes nothing for them: no re-sync, and no row invented for an unknown
customer (the webhook handler holds the same line). It lists them in
`orphaned`, marks each in Stripe (`metadata.orphaned_at`), and emails the
operator once per subscription (`metadata.orphan_notified_at`, set only after
the email is accepted). One that has already ended (`canceled`,
`incomplete_expired`) is only counted, in `orphaned_closed`: that is history,
not money still moving.

"Cannot tell" is not "deleted". On SQLite, or Postgres without Supabase's auth
schema, the check answers None, and reconcile behaves as it did before.

## Credit

A prepaid unit of usage **past a daily cap**: one credit pays for one message
once the day's included allowance is gone. Bought in packs (Plus/Pro) or granted
by an operator, always paid for -- or given -- *before* it is used. That is the
whole difference from overage, which this service still does not do
([ADR 0001](docs/adr/0001-prepaid-credits.md)).

A credit is not an entitlement. The tier decides what a user may do; credits
only extend how much of it they may do today, and they outlive the plan -- a
user who downgrades keeps spending what they already hold. The balance is read
from Postgres on every request and is not in the entitlement cache.

## Credit batch

`credit_batches` -- **one grant or purchase**, with what is left of it
(`remaining`) and when it lapses (`expires_at`, 12 months after the grant).

A user's **balance** is the sum of `remaining` over batches that have not
expired. Expiry is decided at read time (`expires_at > now`), never by a job, so
a lapsed batch simply stops counting at that instant; an `expire` ledger entry
may be written later for the books, but nothing depends on it. The **soonest
expiry** shown to the user skips spent-out batches: an empty batch expiring
tomorrow is not news.

`source` is `purchase` or `admin`. Admin batches come from
`POST /v1/admin/credits`; purchase batches have exactly one writer,
`grant_credits_from_checkout` (`app/services/credit_purchases.py`), and
`stripe_checkout_session_id` is unique so a replayed webhook cannot pay out
twice. `user_id` has no foreign key, like `subscriptions`: the account lives in
Supabase and these rows must outlive it.

## Credit pack

A fixed number of credits sold for a one-time payment: `CREDIT_PACKS` in
`app/plans.py` (small 500, medium 2,500, large 7,500), priced in Stripe by
`scripts/seed_stripe.py` and found through `STRIPE_PRICE_CREDITS_<PACK>`. Only
an active or trialing Plus or Pro subscriber may buy one.

A pack becomes a **purchase batch** only when Stripe says it is paid, and only
through `grant_credits_from_checkout` -- the single writer of purchase batches,
as `sync_subscription_from_stripe` is of subscription state. The Checkout
Session's `metadata.kind = credit_pack` is how the webhook tells a pack from a
subscription checkout; the batch keeps the PaymentIntent id and the amount
paid, which a refund or dispute is later matched against.

## Credit spend

What a request past its daily cap does instead of a 429, when its quota has a
**credit cost** (`CREDIT_COST` in `app/plans.py`; only `messages_per_day`, at
1, in v1). Only after Redis has counted the request over the cap -- under the
cap, Postgres is never asked. It takes the cost from the oldest unexpired
batches with `remaining > 0`, locked with a plain `FOR UPDATE` under
`CREDIT_LOCK_TIMEOUT_SECONDS`, and writes a `spend` ledger row per batch, all
in a transaction of its own. Not refunded if the request later fails, like the
counter itself.

It **fails closed**: a lock timeout or database error refuses the request
(429, `credits_remaining: null`), because a credit we cannot record must not be
given away. That is the opposite of the quota check, which fails *open* -- and
while Redis is down nothing says a request is over the cap, so no credit is
spent at all. Any tier spends, Free after a downgrade included; Enterprise never
reaches it, having no caps.

## Credit ledger

`credit_ledger` -- **append-only**, every movement of credits: the batch, a
signed `amount`, its `kind` (`purchase`, `spend`, `refund`, `dispute`, `admin`,
`expire`) and a `reference` to its cause (a Stripe event id, or
`admin:<actor>`).

Finance's record, not the balance: the balance is read from batches, and the
ledger says how each batch got to where it is. The running service may only
SELECT and INSERT here (migration 0008), so a bug or a leaked credential cannot
rewrite history. Rows survive account deletion.
