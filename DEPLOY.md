# Deploying this

Everything here runs from one image. The API, the migration step and both cron
jobs are the same build with a different argument, so a job can never run
against a different version of the code than the API it shares a database with.

```bash
docker build -t subscription-api .

docker run subscription-api                 # serve HTTP  (default: `api`)
docker run subscription-api migrate         # alembic upgrade head, then exit
docker run subscription-api reconcile       # drift check against Stripe
docker run subscription-api expire-grace    # close dunning grace windows
```

The frontend is a second image (`web/Dockerfile`), or Vercel, which is easier.

## Try the whole thing locally first

```bash
docker compose --profile full up --build
```

Postgres and Redis come up, `migrate` runs to completion, then the API and the
frontend start. This is worth doing before any real deploy: it exercises the
actual images, the migration ordering and the startup dependencies, none of
which `make run` touches.

## The process model

| process | command | how many | notes |
|---|---|---|---|
| API | `api` | scale horizontally | stateless; one uvicorn worker per container |
| migrations | `migrate` | exactly one, before the API rolls | must finish before new code serves |
| reconcile | `reconcile` | one, nightly | alert if it reports drift |
| expire-grace | `expire-grace` | one, nightly | closes dunning windows |

**One worker per container, not `WEB_CONCURRENCY=4`.** `/metrics` is per-process
and held in memory, so under four workers a scrape hits one of them at random
and every number is quartered-ish and wrong. Running four single-worker
containers gives the same capacity and four scrapeable targets, which is what
your platform wants anyway. `WEB_CONCURRENCY` exists for the case where you
have wired up `prometheus_client`'s multiprocess mode; until then leave it at 1.

Memory is roughly 120–180MB per container at rest. The database connection pool
is per-process, so total connections are `containers × pool size` — worth
checking against your Postgres connection limit before scaling out. Supabase's
session pooler exists exactly for this.

## Deploy sequence

```
1. build image, tagged with the commit sha
2. run `migrate` as a one-off task, wait for exit 0
3. roll the API containers
4. (jobs pick up the new image on their next scheduled run)
```

Migrations run as their own task, never on API boot. Two containers starting
together would both try to migrate; one wins and the other fails, restarts, and
your deploy looks flaky for a reason that has nothing to do with your code.

This ordering means **migrations must be backwards compatible with the running
version**, because step 2 finishes while step 3 is still rolling and old code is
briefly talking to the new schema. Adding a nullable column is fine. Dropping or
renaming one is two deploys: stop writing it, ship, then drop it.

Rollback is redeploying the previous image tag. It does *not* undo a migration —
`alembic downgrade` exists and CI proves the round trip works, but running it
against production data is a decision, not a step.

## Health checks

Two probes, and wiring them to the wrong things causes outages.

| probe | checks | use for |
|---|---|---|
| `GET /healthz` | nothing | liveness / container restart |
| `GET /readyz` | Postgres, Redis | load balancer rotation |

`/healthz` deliberately touches no dependency. A liveness probe that checks the
database restarts every instance when the database blinks — a recoverable
outage becomes a crash loop.

`/readyz` returns 503 when Postgres is unreachable, and **200 with
`"status": "degraded"` when only Redis is**. That asymmetry is on purpose: the
read path survives a Redis outage by falling back to the database, and Redis is
shared, so failing readiness on it would pull every instance out of the load
balancer at once and turn a cache problem into a total one. The payload names
whichever dependency is unhappy, and each check runs under its own deadline so
the probe cannot hang.

Neither probe needs credentials — a load balancer cannot send an admin key.
`/metrics` does, and stays behind `X-Admin-Key`.

**Give the readiness probe real headroom.** Measured against a Supabase session
pooler, the database check takes about **2.3s on the first call** (TLS plus the
pooler handshake) and **~600ms warm**. The first call is the one a probe makes
right after a container starts, which is exactly when readiness matters, so a
platform default of 1–2s marks a perfectly healthy instance as unready and the
deploy stalls. Set the probe timeout to 5s and the initial delay / start period
to 15s, and let the app's own deadlines (`DB_COMMAND_TIMEOUT_SECONDS`,
`REDIS_TIMEOUT_SECONDS`) be the thing that decides a dependency is gone.

## Scheduling the two jobs

Same image, different argument. Give both a timeout; a job that hangs holding a
database connection is worse than one that fails.

**crontab**

```cron
17 3 * * *  docker run --rm --env-file /etc/subscription.env subscription-api reconcile
42 3 * * *  docker run --rm --env-file /etc/subscription.env subscription-api expire-grace
```

**Kubernetes** — a `CronJob` per job, `concurrencyPolicy: Forbid` so a slow run
never overlaps itself, `backoffLimit: 2`.

**Fly.io** — `fly machine run <image> reconcile --schedule daily`, or an
external scheduler hitting `fly machine start`.

**ECS** — a scheduled task per job with the same task definition and a
`command` override.

**Render / Railway** — a Cron Job service pointed at the same image with the
command set.

Stagger them. They both walk the subscription table and there is no reason for
them to do it at once.

Reconciliation sets `reconciliation_drift`, but in *its* process, which nothing
scrapes. Alert on its `reconcile.finished` log line and the `mismatched` field
instead. That is a real limitation, not an oversight — see the README.

## Configuration

Required everywhere:

```
ENVIRONMENT=production
DATABASE_URL=postgresql+asyncpg://...      session pooler, port 5432, not 6543
REDIS_URL=redis://...                      without it the cache is a per-process
                                           dict, which is wrong under >1 container
ADMIN_API_KEY=...                          the app refuses the default in production
SUPABASE_URL=https://<project>.supabase.co
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...            from the endpoint you create, not the CLI
STRIPE_PRICE_{PLUS,PRO}_{MONTHLY,ANNUAL}=price_...
CHECKOUT_SUCCESS_URL / CHECKOUT_CANCEL_URL / PORTAL_RETURN_URL
```

`ENVIRONMENT=production` is load-bearing, not a label: it disables `/docs`,
`/redoc` and `/openapi.json` — which publish the entire admin surface without
authentication — and makes the app refuse to start authenticating with the
default admin key.

The frontend's `NEXT_PUBLIC_*` values are **build arguments, not runtime
environment**. `next build` inlines them into the browser bundle, so an image
built against one Supabase project cannot be pointed at another by changing its
environment; it has to be rebuilt. Both values are public by design, so nothing
leaks by baking them in — the constraint is operational. The web image fails the
build if they are missing rather than shipping a bundle with `undefined` in it,
where the server looks healthy and every page throws in the browser.

Secrets go in your platform's secret store. Never in the image: `.dockerignore`
excludes `.env`, because a secret copied into a layer stays in that layer even
if a later one deletes it.

## After the first deploy

1. Create the webhook endpoint in the Stripe dashboard pointing at
   `https://your-host/v1/webhooks/stripe`, and put *its* signing secret in
   `STRIPE_WEBHOOK_SECRET` — the `whsec_` the CLI prints is a different secret
   and will reject every live event.
2. `curl https://your-host/v1/billing/health` — it reports which price ids and
   keys are actually configured, without needing credentials.
3. Confirm `/docs` returns 404. If it does not, `ENVIRONMENT` is not
   `production`.
4. Make one real purchase and watch `webhook_events_total` move.

## Dependencies are pinned

`requirements.txt` says which libraries this depends on and the floor for each.
`requirements.lock` says what actually gets installed, transitive dependencies
included, and is what the image and CI both read — so two builds of the same
commit install the same code.

```bash
make lock        # after editing requirements.txt
make test        # a lock bump is a code change; the tests say whether it is safe
```

The image installs the lock with `--no-deps` and then runs `pip check`: if the
lock is incomplete, the build fails there rather than the container failing on
an import in production.

The base images are pinned to a minor version (`python:3.12-slim-bookworm`,
`node:22-slim`), not a digest. Pinning digests is stricter and worth doing once
you have something that updates them for you; hand-maintained digests go stale
and stop receiving security patches, which is the worse failure.
