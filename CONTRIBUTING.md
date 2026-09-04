# Working on this repo

Every change reaches `main` through a pull request that CI has passed. Nothing
about that is ceremony for its own sake: this service takes money, and the
failure modes are the quiet kind — a webhook handled twice, a migration that
works on SQLite and not on Postgres, a tier limit hard-coded into a component.
CI is where those get caught.

## The loop

```bash
git switch -c fix/thing-that-is-broken
# work
.venv/bin/pytest -q                 # service
cd web && npx playwright test       # frontend
git push -u origin HEAD
```

Pushing a new branch prints a link to open the PR. Fill in the template — the
billing checklist is the part worth actually reading, since each line is
something that has gone wrong in a real subscription system.

## What CI runs

Three jobs, on every PR and every push to `main`:

| job | what it proves |
|---|---|
| `service (py3.11, py3.12)` | `ruff` is clean and all 44 tests pass, on the declared floor and the next version |
| `migrations on postgres` | the schema applies to a real Postgres 16, rolls back, and applies again |
| `web` | the production bundle builds, types check, and 24 Playwright specs pass against that bundle |

The migrations job earns its place: the test suite runs on SQLite for speed, so
this is the only place the schema meets the engine it will actually run on. It
also runs `downgrade base` — a rollback nobody tests is a rollback that does not
work on the night you need it.

The Playwright job runs against `next start`, not `next dev`, because dev mode
compiles on demand and is not what ships.

## Reviewing

Read the diff against three questions:

1. **Could this grant access that was not paid for, or revoke access that was?**
   Anything touching `services/subscriptions.py`, `services/entitlements.py`, or
   `policy.py` deserves slow reading.
2. **Does it survive being run twice?** Webhooks arrive more than once and out of
   order. Handlers must converge, not accumulate.
3. **Did a tier detail leak out of `plans.py`?** A limit in a component or a tier
   name in a conditional is how pricing changes turn into frontend releases.

Merge with **Squash and merge** so `main` reads as one commit per change, and
delete the branch after.

## Branch protection

CI reporting a failure means nothing if a PR can be merged anyway. Set this up in
**Settings → Branches → Add branch ruleset** (or *Branch protection rules*):

- Target: `main`
- **Require a pull request before merging** — with approvals set to 0 if you are
  working solo, so you can still merge your own PRs
- **Require status checks to pass**, selecting `service (py3.11)`,
  `service (py3.12)`, `migrations on postgres` and `web`
- **Require branches to be up to date before merging**
- **Block force pushes**

The status checks only appear in that list after they have run at least once, so
open the first PR before configuring this.

> On a **private** repo, branch protection and rulesets require GitHub Pro or
> Team. On a free plan the rules cannot be enforced — CI still runs and still
> reports on every PR, you just have nothing stopping a merge over a red check.
> Making the repo public also unlocks it, at the cost of publishing the pricing
> and dunning logic.

## Things CI cannot tell you

- **Stripe Test Clocks.** `make testclock` runs the lifecycle against real
  sandbox objects. Run it yourself before merging anything that touches
  `services/subscriptions.py`, `stripe_client.py` or a status mapping — it is
  the only thing that checks our reading of Stripe rather than our logic, and it
  takes about two minutes.

  It also runs nightly at 07:00 UTC via
  [`nightly.yml`](.github/workflows/nightly.yml), which is *not* one of the PR
  checks. That schedule exists because the drift it catches — Stripe moving a
  field, changing a status transition — arrives on its own rather than with your
  commits. A red nightly usually means the provider changed, not that you did.
  You can also trigger it by hand from the Actions tab.

  Two things about that workflow worth knowing. It runs against the same sandbox
  you develop in, so every clock it creates is named `ci-*` and the cleanup step
  only ever deletes those, and only once they are an hour old — a simulation you
  have open in the Dashboard is never touched. And scheduled workflows only run
  from the default branch, and GitHub disables them after 60 days without repo
  activity, so a silent nightly is worth checking on rather than trusting.
- **Price changes.** Editing an amount in `scripts/seed_stripe.py` and re-running
  it creates a *new* Stripe price. Existing subscribers stay on the old one by
  design — verify that grandfathering held rather than assuming it.
