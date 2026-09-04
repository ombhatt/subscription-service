# Subscription service

[![CI](https://github.com/ombhatt/subscription-service/actions/workflows/ci.yml/badge.svg)](https://github.com/ombhatt/subscription-service/actions/workflows/ci.yml)

Flat-price **Free / Plus / Pro** subscriptions on Stripe Billing, with FastAPI,
Postgres, Supabase Auth and a Next.js frontend.

Changes reach `main` through pull requests that CI has passed — see
[CONTRIBUTING.md](CONTRIBUTING.md).

The service is two systems wearing one name:

| | write path | read path |
|---|---|---|
| runs | a few times a day per customer | on every product request |
| owns | Stripe sync, dunning, audit | "what may this user do right now?" |
| on failure | **fails closed** — no confirmation, no access | **fails open** — serves the last known entitlements |

That asymmetry is deliberate. A paying customer locked out by *our* outage is
worse than a free user getting an extra hour of Pro.

---

## Quick start

You need Python 3.11+, Node 20+, a [Stripe](https://dashboard.stripe.com) test
account, and a [Supabase](https://supabase.com/dashboard) project. Supabase is
not optional: it provides authentication, and its Postgres is what the service
runs on, so one signup covers both.

On macOS, name the interpreter explicitly. A bare `python3` is often an old
system build, and the failure is an unhelpful `unsupported operand type(s) for |`
from inside Pydantic rather than anything mentioning versions.

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Fill in `.env`:

- `SUPABASE_URL` — your project URL
- `DATABASE_URL` — the **session pooler** connection string with the driver
  changed to `postgresql+asyncpg://`. See [Connecting to Postgres](#connecting-to-postgres);
  the route you pick is not cosmetic.
- `STRIPE_SECRET_KEY` — an `sk_test_…` key

Then create the products and prices, and apply the schema:

```bash
.venv/bin/python -m scripts.seed_stripe   # prints the price ids for .env
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

Redis is optional for a single process — without `REDIS_URL` the cache falls
back to an in-process dict, which is wrong under more than one worker but fine
while developing.

For the frontend, copy [`web/.env.local.example`](web/.env.local.example) to
`web/.env.local` and fill in your project URL and **anon** key. Both are public
by design; the `service_role` key must never appear there.

```bash
cd web && npm install && npm run dev
```

Finally, forward webhooks in a second terminal and put the printed `whsec_…`
into `.env`:

```bash
stripe listen --forward-to localhost:8000/v1/webhooks/stripe
```

Then sign up at <http://localhost:3000/login>. Note that `stripe listen` mints a
fresh signing secret each session — if it differs from `.env`, every delivery
fails signature verification with a 400 until you update it.

Only `/v1/billing/plans` and `/healthz` are reachable without a session, so
`curl` is of limited use for exploring; the web app is the way in.

## How it works

### Access is granted on the webhook, never on the redirect

`POST /v1/billing/checkout` returns a hosted Stripe Checkout URL and changes
nothing. The customer may close the tab before the success page loads and they
have still paid, so the success URL only renders a page — it polls
`/v1/entitlements` until the grant appears, and says so plainly if it never does.

Every handled event type ends in the same call:

```python
await sync_subscription_from_stripe(session, stripe_customer_id=...)
```

It takes no event payload and applies no deltas — it asks Stripe what is true
for that customer and writes it down. Duplicate delivery is therefore a no-op
and out-of-order delivery converges. Event ids are recorded in
`processed_events` before handling; a second delivery of a *processed* event
returns `{"status": "duplicate"}`, while a previously *failed* one is allowed to
run again, which is what Stripe's retry is for.

Every outbound call is bounded. Stripe's SDK defaults to an **80-second**
timeout with two retries, which would be four minutes of a held row lock (see
below); it is set to 10 seconds here. Postgres statements, the row-lock wait and
Redis operations are all bounded too, in [`app/config.py`](app/config.py) — the
default for each of those is "wait indefinitely", and an unbounded wait
somewhere is how one slow dependency becomes an outage.

Events for one customer also arrive *simultaneously* — a checkout produces
`checkout.session.completed`, `customer.subscription.created` and `invoice.paid`
within the same second. So `sync` takes `SELECT ... FOR UPDATE` on the
subscription row **before** asking Stripe. The order is the point: locking
afterwards would let both workers fetch the same stale answer and merely
serialise the writes, which fixes nothing.

The holder of that lock is inside a network call, so the wait is bounded too:
`lock_timeout` makes a second worker give up after 5 seconds rather than hold a
connection until Stripe answers. It then returns 500 and Stripe retries on its
own backoff, which is the right behaviour under exactly those conditions.

### The entitlement interface

The product never asks "what plan is this user on". It asks:

```
GET /v1/entitlements
```

```json
{
  "tier": "pro",
  "status": "active",
  "source": "subscription",
  "features": { "models": ["small", "large", "reasoning"], "context_tokens": 1000000 },
  "quotas": [
    { "key": "messages_per_day", "limit": 1500, "used": 12, "remaining": 1488,
      "reset_at": "2026-09-05T00:00:00+00:00" }
  ],
  "current_period_end": "2026-10-04T09:14:00+00:00",
  "cancel_at_period_end": false,
  "grace_ends_at": null
}
```

Server-side, a metered endpoint does two things (see
[`app/routers/chat_demo.py`](app/routers/chat_demo.py)):

```python
if body.model not in feature(ents, "models"):
    raise FeatureNotEntitled(...)          # 403, names the tier that unlocks it
await quota.consume(session, user_id=..., key="messages_per_day", entitlements=ents)
                                            # 429 with remaining + reset_at + upgrade_tier
```

Tiers change; that call signature does not. **Nothing outside
[`app/plans.py`](app/plans.py) may hard-code a tier name or a numeric cap.**

### Authentication

Sessions are Supabase Auth JWTs, verified locally against the project's JWKS —
so no call to Supabase sits in the hot path of every request, and a rotated or
revoked key takes effect without a deploy. Signature, expiry, audience and
issuer are all checked, `sub` is required, and the accepted algorithms are
pinned so a forged token cannot select `none`.

There is deliberately **no development bypass** in production code. An "accept
this header and trust it" branch is exactly the kind of thing that survives into
production; the test suite overrides the dependency instead, which cannot.

One rule outlives any provider: **resolve the tier from this service, never from
a claim in the token.** Tokens are minted before a downgrade and stay valid
after it. The token says who you are; the database says what you may do.

### States

Six, and only three grant paid access:

```
free ──checkout──▶ active ──renewal fails──▶ past_due ──grace expires──▶ free
  │                  │  ▲                        │
  └──trial──▶ trialing  └────retry clears────────┘
                     │
                     └──user cancels──▶ (cancel_at_period_end) ──period ends──▶ free
```

`past_due` keeps access for `DUNNING_GRACE_DAYS`. Most failed renewals are
expired cards, and cutting off a customer who intends to pay turns a card update
into a cancellation. The grace check runs on the read path *and* in the nightly
job, so a lapsed subscriber never keeps access just because a job was late.

A cancelled subscription is treated as no subscription at all — Stripe keeps
returning the dead object, and without that the local state would differ
depending on how long the provider kept it around.

### Promotion codes

Codes are created in the Stripe dashboard and redeemed on Stripe's Checkout
page: `create_checkout_session` sets `allow_promotion_codes`, so the redemption
box appears with no work here. A code can also be pre-applied by passing
`promo_code` to `/v1/billing/checkout`.

**A discount changes what someone pays, never what they get.** `resolve_tier()`
keys off the Stripe price id, so a 25%-off Pro subscriber is still on the Pro
price and still resolves to Pro — entitlements, quotas, dunning and the grace
window need no knowledge of discounts at all. If you ever want a promotion to
change *limits* rather than price, that is an `entitlement_grant`, not a coupon.

The active discount is mirrored onto the subscription during sync and surfaced
on `/v1/billing/subscription` — deliberately *not* on `/v1/entitlements`, which
is the hot path and answers what a user may do, not what they were charged.

Reading it from Stripe needs care, and the shapes are asserted in
[`tests/test_discounts.py`](tests/test_discounts.py) using real payloads:
`subscription.discounts` is an array of *ids*, the legacy singular
`subscription.discount` is gone, and the amount is not on the discount at all —
it lives on the coupon, which only appears with
`expand=["data.discounts.source.coupon"]`.

Two things worth knowing before running a promotion. A **100%-off coupon is not
a trial**: the subscription is `active`, not `trialing`, and the invoice is 0,
which cannot fail — so no dunning, and you should check Checkout still collects
a card. And the **pricing page shows list price**; the frontend renders an
`Offer` rather than a raw number ([`web/lib/types.ts`](web/lib/types.ts)) so a
discount becomes a data change rather than a component change.

### Money mechanics we deliberately do not own

Change plan, cancel, reactivate, update card, download invoices — all of it is
Stripe's hosted Customer Portal (`POST /v1/billing/portal`). That is why there
is no billing UI and no proration code in this repo. Refunds are manual from the
Stripe dashboard; automating them before you understand your own fraud patterns
is premature.

## Connecting to Postgres

Supabase offers three routes and they are not interchangeable.
[`app/db.py`](app/db.py) encodes the difference rather than leaving it as
folklore:

| route | port | notes |
|---|---|---|
| direct `db.<ref>.supabase.co` | 5432 | **IPv6 only** on projects created since 2024 — fails from any network without IPv6, GitHub Actions runners included |
| **session pooler** `…pooler.supabase.com` | 5432 | IPv4, holds the connection for the session, prepared statements work. **Use this one.** |
| transaction pooler | 6543 | IPv4, returns the connection after every transaction, so prepared statements break. Handled automatically if you point at it, but avoid |

The connection string Supabase shows you starts `postgresql://` and will not
work as-is; it needs `postgresql+asyncpg://`.

## The web app

A Next.js + TypeScript frontend lives in [`web/`](web) and is what
`CHECKOUT_SUCCESS_URL` and `PORTAL_RETURN_URL` point at:

| route | what it shows |
|---|---|
| `/` | pricing — limits from `plans.py`, amounts from Stripe, both via `/v1/billing/plans` |
| `/login` | sign in or sign up |
| `/billing` | live entitlements, quota meters, dunning and cancellation banners, any discount, portal link |
| `/billing/success` | where Stripe returns; **polls** until the webhook grants, and says so if it never does |
| `/chat` | a metered endpoint rendering both paywalls: 403 locked model, 429 out of quota |

`next.config.mjs` rewrites `/api/*` to the FastAPI service, so the browser makes
same-origin requests and there is no CORS to configure.

`useRequireSession` redirects signed-out visitors away from `/billing` and
`/chat`. That is a rendering decision, not a security boundary — the API
verifies every token itself. It is deliberately client-side: Next middleware
would have called Supabase's `getUser()` on every navigation, paying a network
round trip per page load for protection that was never load-bearing.

## Layout

```
app/
  plans.py             tier -> limits. The only definition of a tier.
  policy.py            grace-window rules, shared by both paths
  auth.py              Supabase JWT verification; the admin key
  models.py            5 tables (below)
  db.py                engine settings, including the pooler routes
  stripe_client.py     the only module that imports `stripe`
  services/
    subscriptions.py   the write path; sync_subscription_from_stripe
    entitlements.py    the read path; resolve + cache + fail open
    quota.py           windows, atomic counters, enforcement
    view.py            assembles the API payload
    audit.py           append-only transitions
  routers/
    billing.py         checkout, portal, plans, subscription
    webhooks.py        Stripe -> sync, plus an admin-only ops view
    entitlements.py    GET /v1/entitlements
    admin.py           support tooling
    chat_demo.py       example metered endpoint — delete once yours does this
  jobs/
    reconcile.py       nightly drift check against Stripe
    expire_grace.py    nightly dunning cut-off
scripts/
  seed_stripe.py           products + prices, checked in rather than clicked
  cleanup_test_clocks.py   removes clocks an interrupted run left behind
```

### Schema

| table | why |
|---|---|
| `subscriptions` | one row per user for the life of the account, updated in place. `user_id` is unique — that is what makes a double-clicked checkout unable to leave someone with two subscriptions |
| `processed_events` | webhook de-duplication, keyed by Stripe's event id |
| `usage_counters` | durable mirror of the Redis counters, for support and analytics |
| `entitlement_grants` | comps and staff accounts; resolution takes the higher of grant and subscription |
| `subscription_audit` | append-only transitions with the causing event id |

## Tests

```bash
.venv/bin/pytest -q              # 91, service logic against a Stripe fake
cd web && npx playwright test    # 30, frontend against a mocked API
make testclock                   #  7, against real Stripe sandbox objects
```

The **service** suite runs on SQLite with a Stripe fake — no network, no
containers. What it asserts is the set of things that break subscription code in
production: duplicate webhook delivery, out-of-order delivery, a failed event
being retried, a grandfathered price, an unrecognised price, a cancelled
subscription Stripe still returns, and the grace window not restarting on every
retry.

The **frontend** suite ([`web/e2e`](web/e2e)) drives a real browser with
`/api/**` mocked, so it needs no backend. Two of its rules exist because of bugs
that got through: the API fake is an `auto` fixture with a catch-all route, so a
test cannot silently reach the real service by omission; and dates are asserted
against a pinned `timezoneId: "UTC"`, since the app formats with
`toLocaleDateString` and would otherwise pass or fail depending on who ran it.

The **test-clock** suite ([`integration/`](integration)) is the only one that
talks to Stripe for real. It creates a simulation per test, moves time forward,
and asserts what `sync_subscription_from_stripe` writes — catching the class of
bug the fake structurally cannot, where our *reading* of Stripe is wrong rather
than our logic. It found one immediately.

Two things it cannot do, by construction. It **never runs on pull requests** —
it needs a sandbox key and network, so it runs nightly instead
([`nightly.yml`](.github/workflows/nightly.yml)) and on demand via
`make testclock`. And **a test clock moves Stripe's time, not ours**:
`past_due_since` is stamped with real wall-clock time, so advancing a simulation
will never age our grace window.

## Operations

Two cron entries:

```bash
python -m app.jobs.reconcile      # nightly; alert on mismatched > 0
python -m app.jobs.expire_grace   # nightly; closes dunning grace windows
```

Reconciliation exists because you *will* miss webhooks — an outage, a bad
deploy, a 500 inside a retry window. The only question is whether you find out
before the customer does. When it happens to one customer,
`POST /v1/admin/users/{id}/resync` fixes that one without waiting for the job.

Support endpoints, all behind `X-Admin-Key`:

```
GET    /v1/admin/users/{user_id}              entitlements + Stripe ids + full audit
POST   /v1/admin/users/{user_id}/resync       force a re-read from Stripe
POST   /v1/admin/users/{user_id}/extend-grace push back a dunning cut-off
POST   /v1/admin/grants                       comp a user to a tier
DELETE /v1/admin/grants/{id}                  revoke
GET    /v1/webhooks/recent                    webhook health at a glance
```

Alert on three things and you catch nearly everything: webhook processing lag,
payment failure rate, reconciliation drift.

## Before this takes real money

- [x] **Real authentication** — Supabase Auth, verified against the project's JWKS
- [x] **Postgres** — the Supabase project's database, via the session pooler
- [x] **Test Clocks** — renewal, dunning, cancel-at-boundary, trial conversion
      and mid-cycle upgrade, against real Stripe objects
- [ ] **Re-enable email confirmation** in Supabase if you disabled it. Without
      it anyone can register an address they do not own, and that address ends
      up on the Stripe customer and every receipt.
- [ ] **Set `REDIS_URL`.** Without it the cache falls back to an in-process dict
      that is wrong under more than one worker.
- [ ] **Change `ADMIN_API_KEY`**, and put the admin router behind your internal
      network or SSO rather than a shared secret. The code refuses to
      authenticate with the default value when `ENVIRONMENT=production`.
- [ ] **Rate limiting.** There is none. General throttling belongs at the edge;
      the three worth doing in-app are admin auth attempts, checkout creation
      per user, and any public promo-code lookup added later.
- [ ] **Enable Stripe Tax** and set an origin address, or you are collecting no
      tax at all. You are the merchant of record on Stripe Billing: VAT and
      sales tax registration are yours.
- [ ] **Pin `STRIPE_API_VERSION`** to the version you tested against. Recent
      versions moved the billing period from the subscription onto its items;
      `stripe_client.subscription_period()` reads whichever is present, so an
      account upgrade cannot silently null out every renewal date.
- [ ] **Decide the mid-cycle downgrade rule** and put it on the pricing page: if
      someone used 1,400 Pro messages and drops to Plus, does the lower cap
      apply now or next period? Today it applies at the next period.
- [ ] **Account deletion must cancel the Stripe subscription**, and GDPR erasure
      has to be reconciled against multi-year financial record retention: delete
      the profile, keep the invoice.

## What is deliberately not here

Team seats, usage-based overage, enterprise or custom plans, multi-currency,
self-serve refunds, in-house invoice PDFs, coupon management screens, and rate
limiting.

Seats are the one deferred item that changes the schema — a subscription stops
belonging to a user — so if teams are on the roadmap, say so before this grows
call sites.
