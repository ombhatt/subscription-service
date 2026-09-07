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

Five jobs, on every PR and every push to `main`:

| job | what it proves |
|---|---|
| `service (py3.11, py3.12)` | `ruff` is clean and all 170 tests pass, on the declared floor and the next version |
| `migrations on postgres` | the schema applies to a real Postgres 17, rolls back, and applies again |
| `web` | 35 unit tests run first, then the production bundle builds, types check, and 55 Playwright specs pass against that bundle — including an axe sweep for WCAG A/AA violations on every page |
| `coverage` | **fails the PR if backend coverage drops below 80%**, and if `web/lib` drops below its vitest thresholds |
| `images` | both images build, the API image migrates a real Postgres and answers both probes, and every log line it writes is JSON |

The migrations job earns its place: the test suite runs on SQLite for speed, so
this is the only place the schema meets the engine it will actually run on. It
also runs `downgrade base` — a rollback nobody tests is a rollback that does not
work on the night you need it.

The Playwright job runs against `next start`, not `next dev`, because dev mode
compiles on demand and is not what ships.

The `images` job builds *and runs* the containers rather than only building
them. "It builds" is a much weaker claim than "it starts, migrates a database
and answers its own probes" — and since these images cannot be built on a Mac
without Docker installed, CI is the only place the Dockerfile is ever exercised.

### Coverage

`coverage` is its own job, and therefore its own check, so it can be marked
**required** in the branch ruleset. Folded into `service` it could only be
required together with the tests, and a coverage drop would show up in the
PR's check list looking like a test failure.

It fails the pull request when the backend drops below **80%** (`fail_under`
in `pyproject.toml`) or `web/lib` drops below its vitest thresholds. Both
numbers also land in the pull request's job summary, so a change that moves
them is visible without anyone going looking.

```bash
make coverage                 # backend: report + the 80% floor
make coverage-html            # same, browsable at htmlcov/index.html
cd web && npm run coverage    # web/lib
```

The floors are floors, not targets — set a little below where the suites
actually sit, so they catch a real regression without failing every PR that
adds a branch. Raise them when a number moves up and stays up.

Backend coverage **requires** `concurrency = ["thread", "greenlet"]`, which
`pyproject.toml` already sets. Without it, coverage cannot see inside
SQLAlchemy's async bridge and reports most of the request path as unreached
while its tests pass — a 70% reading against a true 83%.

### Security

Two workflows, answering two different questions.

`Security` (`.github/workflows/security.yml`) asks **is anything we already
depend on known-vulnerable**. It runs on every pull request *and daily at
06:20 UTC*, and the daily run is the one that earns its keep: an advisory
published today applies to versions that have been sitting in `main` for
weeks, and nobody opens a pull request to tell you that. Three jobs:

| job | what it checks |
|---|---|
| `dependency audit` | `pip-audit` against `requirements.lock`, `npm audit` against `web/` |
| `image scan` | Trivy against the built API image — the OS packages we inherit from the base image, which nothing here pins |
| `code scanning` | CodeQL over Python and TypeScript; results land in the Security tab |

Dependabot (`.github/dependabot.yml`) is the other half — it keeps things
current, weekly, grouped so patch bumps arrive as one reviewable pull request
rather than five rubber-stamped ones. It covers pip, npm, GitHub Actions and
both Dockerfiles.

The lock is deliberately named `requirements.lock` and **not** `.txt`, so
Dependabot does not parse it. Dependabot's pip ecosystem reads a flat `.txt`
as a list of *direct* requirements with no notion of the constraints between
them; pointed at a generated lock it proposes single-package bumps that cannot
resolve — it offered `pydantic-core 2.48.0` while `pydantic` pins
`pydantic-core==2.46.5` exactly. So the lock stays a generated artefact:
Dependabot manages `requirements.txt` and `requirements-dev.txt`, `make lock`
regenerates the whole tree coherently, and `pip-audit` covers the lock for
advisories daily.

That is also why `requirements-dev.txt` no longer does `-r requirements.lock`.
Install both explicitly:

```bash
pip install -r requirements.lock -r requirements-dev.txt
```

Locally:

```bash
make audit                    # both ecosystems
```

Auditing the **lock** rather than `requirements.txt` is deliberate — the latter
holds floors, which is not what runs anywhere.

### Changing a dependency

`requirements.txt` holds the direct dependencies and their floors.
`requirements.lock` holds the full transitive tree at exact versions and is what
the images and CI actually install. It is generated — never hand-edit it, and
never let a bot edit it either.

```bash
# edit requirements.txt, then
make lock
make test
```

Commit both. A lock bump is a code change — the suite is what says whether it is
a safe one, and reviewing the lock diff is how you notice that a patch release
of something you have never heard of just entered production.

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

`main` is protected, and this is enforced rather than advisory: a direct push is
rejected with `GH013`, and a merge needs a pull request with all four checks
green. The ruleset requires a pull request (0 approvals, so you can merge your
own), those four status checks, branches up to date before merging, and blocks
force pushes and deletion.

Two things that were not obvious when setting it up, in case it ever needs
rebuilding. A ruleset can be **Active** and still enforce nothing if its *target
branches* list is empty — check `gh api repos/:owner/:repo/rules/branches/main`
returns rules rather than `[]`, because the settings page looks identical either
way. And status checks only appear in the selector once they have run at least
once, so open a PR before configuring it.

Enforcement on a **private** repo needs a GitHub Team or Enterprise
*organization* — a personal Pro upgrade does not unlock it. This repo is public,
which is the other way to get it.

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
