# Subscription service

[![CI](https://github.com/ombhatt/subscription-service/actions/workflows/ci.yml/badge.svg)](https://github.com/ombhatt/subscription-service/actions/workflows/ci.yml)

Flat-price **Free / Plus / Pro** subscriptions on Stripe Billing, FastAPI and Postgres.

Changes reach `main` through pull requests that CI has passed — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the loop, what the three CI jobs prove,
and what CI still cannot tell you.

The service is two systems wearing one name:

| | write path | read path |
|---|---|---|
| runs | a few times a day per customer | on every product request |
| owns | Stripe sync, dunning, audit | "what may this user do right now?" |
| on failure | **fails closed** — no confirmation, no access | **fails open** — serves the last known entitlements |

That asymmetry is deliberate. A paying customer locked out by *our* outage is worse than a free user getting an extra hour of Pro.

---

## Quick start

Needs **Python 3.11+**. On macOS, `python3` is often Xcode's 3.9 — name the
interpreter explicitly when creating the venv, or the first import fails inside
Pydantic with a confusing `unsupported operand type(s) for |`.

Postgres and Redis, either way — `make up` for Docker, or `make services` if you
installed them with Homebrew (that one also creates the `postgres` role and the
database, which a brew install does not give you by default).

```bash
make up          # or: make services
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # add your Stripe test key
.venv/bin/python -m scripts.seed_stripe   # creates products/prices, prints price ids
# paste the printed price ids into .env
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

In a second terminal, point Stripe's CLI at the webhook endpoint and put the
printed `whsec_…` in `.env`:

```bash
stripe listen --forward-to localhost:8000/v1/webhooks/stripe
```

Then walk the flow:

```bash
curl -s localhost:8000/v1/entitlements -H 'X-User-Id: alice' | jq
```

## The web app

A Next.js + TypeScript frontend lives in [`web/`](web) and is what the
`CHECKOUT_SUCCESS_URL` and `PORTAL_RETURN_URL` in `.env` point at:

```bash
cd web && npm install && npm run dev     # http://localhost:3000
```

Four surfaces, all reading from the API rather than duplicating anything:

| route | what it shows |
|---|---|
| `/` | pricing — limits from `plans.py`, amounts from Stripe, both via `/v1/billing/plans` |
| `/billing` | live entitlements, quota meters, dunning and cancellation banners, portal link |
| `/billing/success` | where Stripe returns; **polls** until the webhook grants, and says so |
| `/chat` | a metered endpoint rendering both paywalls: 403 locked model, 429 out of quota |

`next.config.mjs` rewrites `/api/*` to the FastAPI service, so the browser makes
same-origin requests and there is no CORS to configure. If you ever host the
frontend on its own domain, drop the rewrite and add CORS middleware instead.

Identity is the same development stub the API uses: the nav's **Switch user**
control writes a name to `localStorage` and it is sent as `X-User-Id`. Switching
between two users is the quickest way to watch one backend resolve two different
entitlement sets. [`web/lib/api.ts`](web/lib/api.ts) is the single place that
changes when real auth lands.

The success page deserves a note: it grants nothing and cannot. It polls
`/v1/entitlements` until the tier changes, and if the webhook never arrives it
says exactly that rather than pretending. Closing the tab mid-checkout is
harmless, which is the whole point of granting on the webhook.

## Tests

```bash
.venv/bin/pytest -q              # the service
cd web && npx playwright test    # the frontend
```

The **service** suite runs against SQLite with a Stripe fake — no network, no
containers. What it actually asserts is the set of things that break
subscription code in production: duplicate webhook delivery, out-of-order
delivery, a failed event being retried, a grandfathered price, an unrecognised
price, and the grace window not restarting on every retry.

The **frontend** suite ([`web/e2e`](web/e2e)) drives a real browser with
`/api/**` mocked, so it needs no Postgres, no Redis and no Stripe. Mocking is
what lets it put the UI into states that are slow or impossible to produce for
real: a subscriber mid-dunning, a cancelled-but-not-yet-ended plan, a comped
account, a Stripe outage, and a webhook that never arrives.

Two of its rules are there because of bugs that got through:

- **`useUser` is covered explicitly.** A clean type check and a clean production
  build both missed that switching user re-rendered only the nav; only clicking
  found it. `user-switch.spec.ts` is that regression.
- **The API fake is an `auto` fixture, and there is a catch-all route.** As a
  lazy fixture, any test that did not destructure `api` installed no routes, and
  its requests went through the Next rewrite to the *real* service — mutating
  real quota counters and making the result depend on that service's state. Now
  the fake is always installed, and any unmocked `/api/**` call fails loudly
  rather than silently going to the network.

Dates are asserted against a pinned `timezoneId: "UTC"` and `locale: "en-US"`,
since the app formats with `toLocaleDateString` and would otherwise render
"Sep 10 UTC" as "Sep 9" for anyone west of Greenwich.

---

## How it works

### Access is granted on the webhook, never on the redirect

`POST /v1/billing/checkout` returns a hosted Stripe Checkout URL and changes
nothing. The customer may close the tab before the success page loads and they
have still paid, so the success URL only renders a page.

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
      "reset_at": "2026-09-03T00:00:00+00:00" }
  ],
  "current_period_end": "2026-10-02T09:14:00+00:00",
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

### Money mechanics we deliberately do not own

Change plan, cancel, reactivate, update card, download invoices — all of it is
Stripe's hosted Customer Portal (`POST /v1/billing/portal`). That is why there
is no billing UI and no proration code in this repo. Refunds are manual from the
Stripe dashboard for now; automating them before you understand your own fraud
patterns is premature.

---

## Layout

```
app/
  plans.py             tier -> limits. The only definition of a tier.
  policy.py            grace-window rules, shared by both paths
  models.py            5 tables (below)
  stripe_client.py     the only module that imports `stripe`
  services/
    subscriptions.py   the write path; sync_subscription_from_stripe
    entitlements.py    the read path; resolve + cache + fail open
    quota.py           windows, atomic counters, enforcement
    view.py            assembles the API payload
    audit.py           append-only transitions
  routers/
    billing.py         checkout, portal, plans, subscription
    webhooks.py        Stripe -> sync
    entitlements.py    GET /v1/entitlements
    admin.py           support tooling
    chat_demo.py       example metered endpoint — delete once yours does this
  jobs/
    reconcile.py       nightly drift check against Stripe
    expire_grace.py    nightly dunning cut-off
scripts/seed_stripe.py products + prices, checked in rather than clicked
```

### Schema

| table | why |
|---|---|
| `subscriptions` | one row per user for the life of the account, updated in place. `user_id` is unique — that is what makes a double-clicked checkout unable to leave someone with two subscriptions. |
| `processed_events` | webhook de-duplication, keyed by Stripe's event id |
| `usage_counters` | durable mirror of the Redis counters, for support and analytics |
| `entitlement_grants` | comps and staff accounts; resolution takes the higher of grant and subscription |
| `subscription_audit` | append-only transitions with the causing event id |

---

## Operations

Two cron entries:

```bash
python -m app.jobs.reconcile      # nightly; alert on mismatched > 0
python -m app.jobs.expire_grace   # nightly; closes dunning grace windows
```

Reconciliation exists because you *will* miss webhooks — an outage, a bad
deploy, a 500 inside a retry window. The only question is whether you find out
before the customer does.

Support endpoints (`X-Admin-Key`):

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

---

## Before this takes real money

- [ ] **Replace the auth stub.** [`app/auth.py`](app/auth.py) reads `X-User-Id`
      and refuses to run in production. Wire it to your real session/JWT check.
      Resolve the tier from this service, never from a claim in the token — a
      downgrade has to take effect before the token expires.
- [ ] **Set `REDIS_URL`.** Without it the cache falls back to an in-process dict
      that is wrong under more than one worker.
- [ ] **Change `ADMIN_API_KEY`**, and put the admin router behind your internal
      network or SSO rather than a shared secret.
- [ ] **Enable Stripe Tax** and set an origin address, or you are collecting no
      tax at all. You are the merchant of record on Stripe Billing: VAT/sales
      tax registration is yours. (Paddle is where you go to make that
      someone else's problem, at roughly 5%.)
- [ ] **Run the Test Clock suite** in your Stripe test account: renewal,
      upgrade, downgrade at the boundary, failed payment through full dunning,
      cancel and reactivate. The pytest suite covers our logic; test clocks
      cover the integration.
- [ ] **Pin `STRIPE_API_VERSION`** to the version you tested against. Note that
      recent versions moved the billing period from the subscription onto its
      items — `stripe_client.subscription_period()` reads whichever is present,
      so an account upgrade cannot silently null out every renewal date.
- [ ] **Decide the mid-cycle downgrade rule** and put it on the pricing page: if
      someone used 1,400 Pro messages and drops to Plus, does the lower cap
      apply now or next period? Today it applies at the next period, because the
      downgrade itself does.
- [ ] **Account deletion must cancel the Stripe subscription**, and GDPR erasure
      has to be reconciled against multi-year financial record retention: delete
      the profile, keep the invoice.

## What is deliberately not here

Team seats, usage-based overage, enterprise/custom plans, multi-currency,
self-serve refunds, in-house invoice PDFs, coupon management screens.

Seats are the one deferred item that changes the schema — a subscription stops
belonging to a user — so if teams are on the roadmap, say so before this grows
call sites.
