# 1. Prepaid credits are allowed; postpaid overage is still out

- **Status:** accepted
- **Date:** 2026-11-23
- **Context:** PRD ombhatt/subscription-service#134; first implemented in #135

## Context

Every tier has fixed caps, defined only in `app/plans.py`, and the service's
founding rule has been that **usage is gated, never billed as overage**: when a
Plus or Pro subscriber hits the daily message cap they get a 429 and wait for
the UTC-midnight reset, or upgrade. Price stays flat per tier.

That rule cost something real. A subscriber with one heavy day has no way to
pay a little more for that day. The two ways to sell "more" are:

- **Postpaid (metered) overage** -- let them keep going and bill the excess on
  the next invoice.
- **Prepaid credits** -- they buy a pack up front; each credit pays for one
  unit of usage past the cap; when the credits run out, the cap is back.

## Decision

**Prepaid credits are allowed. Postpaid or metered overage of any kind is
still excluded.**

Usage past the plan allowance needs credits that were paid for before they are
used. A credit is one message past the daily cap; the included allowance is
always used first. Credits come in batches that expire 12 months after they are
granted (`CREDIT_VALIDITY_MONTHS` in `app/plans.py`), are recorded in an
append-only ledger in Postgres, and are only ever *bought* (Plus/Pro) or
*granted by an operator* -- never accrued as a debt.

## Why this line and not the other

- **The gate stays the single enforcement point.** With prepaid credits the
  quota path still answers one question -- may this request run now? -- and
  the answer is still "no" (429) once allowance and credits are both gone.
  Nothing downstream has to reconstruct what a user owes. Metered overage would
  turn the counter into a billing input: the Redis counter, which deliberately
  fails *open* during an outage, would become a number we invoice from, and
  every fail-open request would be either a free message or a disputed charge.
- **No collection risk.** The money is in hand before the usage happens.
  Overage is billed after the fact, against a card that may decline, by a
  customer who may not have expected the bill -- which means dunning, disputes
  and support load on exactly the users we were trying to please. A failed
  overage invoice is also a past-due subscription, which drags the dunning
  grace window into what should have been a one-day spike.
- **The price a customer sees stays the price they pay.** Flat tiers plus an
  explicit, chosen purchase keep "what will this cost me" answerable without a
  usage report.
- **Pricing keeps upgrades honest.** Credits are priced well above a fully used
  subscription's per-message cost, so they suit occasional spikes and a
  regular heavy user is still better off on Pro.

## Consequences

- Two new tables, `credit_batches` and `credit_ledger` (migration 0008). The
  ledger is append-only for the running service (it may only SELECT and
  INSERT) and both tables survive account deletion, for the books.
- **Expiry is read-time**: the balance counts batches whose `expires_at` is in
  the future. No job enforces it, so none can fall behind -- the same reasoning
  as the dunning grace window in GLOSSARY.md.
- The balance is read from Postgres and is not part of the cached
  entitlements. If that ever changes, every credit write must mark and commit
  through `commit_and_invalidate`.
- Prepaid credits are deferred revenue until spent or expired. The ledger
  carries what that accounting needs; no reporting is built yet.
- Over-cap requests during a Redis outage are free, as included usage already
  is. Accepted.
- Still out of scope, and each needs its own decision: auto-recharge (charging
  a saved card without the user present is postpaid in all but name),
  custom amounts, credits for Free or Enterprise purchase, refunds of unused
  credits.
