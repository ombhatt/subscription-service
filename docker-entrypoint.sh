#!/bin/sh
# One image, several roles. The web process, the migration step and both cron
# jobs are the same build with a different argument, so a job can never run
# against a different version of the code than the API it shares a database
# with.
#
#   api           serve HTTP (default)
#   migrate       alembic upgrade head, then exit
#   reconcile     drift check against Stripe, then exit
#   expire-grace  close dunning grace windows, then exit
#   *             run it verbatim, so `docker run <image> python -c ...` works
set -eu

case "${1:-api}" in
  api)
    # --proxy-headers so the app sees the client's scheme and address rather
    # than the load balancer's; --forwarded-allow-ips because uvicorn ignores
    # those headers unless it is told the proxy is trusted.
    #
    # WEB_CONCURRENCY defaults to 1: /metrics is per-process and in memory, so
    # under N workers a scrape hits one of them at random. Raise it once
    # prometheus_client multiprocess mode is wired up, or scale by running
    # more single-worker containers -- which is what the platform wants anyway.
    #
    # Access logs stay on: they are JSON like everything else and carry the
    # request id. Add --no-access-log if your load balancer already logs them
    # and you would rather not pay twice.
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --workers "${WEB_CONCURRENCY:-1}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  reconcile)
    exec python -m app.jobs.reconcile
    ;;
  expire-grace)
    exec python -m app.jobs.expire_grace
    ;;
  *)
    exec "$@"
    ;;
esac
