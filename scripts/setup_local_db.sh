#!/usr/bin/env bash
# Create the role and database that DATABASE_URL expects.
#
# Homebrew's Postgres initialises with a superuser named after the current OS
# user and no "postgres" role at all, while docker-compose gives you
# postgres/postgres. This papers over that difference so the same DATABASE_URL
# works either way and nobody has to keep two .env files.
#
# Idempotent: safe to re-run.

set -euo pipefail

PG_PREFIX="$(brew --prefix postgresql@16)"
export PATH="$PG_PREFIX/bin:$PATH"

if ! pg_isready -q; then
    echo "Postgres is not accepting connections. Start it with:" >&2
    echo "    brew services start postgresql@16" >&2
    exit 1
fi

if createuser -s postgres 2>/dev/null; then
    echo "created role 'postgres'"
else
    echo "role 'postgres' already exists"
fi

psql -q -d postgres -c "ALTER ROLE postgres WITH PASSWORD 'postgres';"

if createdb -O postgres subscriptions 2>/dev/null; then
    echo "created database 'subscriptions'"
else
    echo "database 'subscriptions' already exists"
fi

echo
echo "ready: postgresql://postgres:postgres@localhost:5432/subscriptions"
echo "matches DATABASE_URL in .env and the docker-compose service."
