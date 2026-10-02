-- The database role the integration suite uses in CI. Run once, as the admin
-- credential (MIGRATION_DATABASE_URL). Idempotent.
--
-- Not an Alembic migration on purpose: this is test infrastructure for one
-- Supabase project, and a migration would create a CI login in every database
-- the service is ever deployed to -- production included.
--
-- What it can do: create schemas named `ci_<epoch>_<8 hex>`, through
-- ci.create_schema() below, and do anything inside the schemas it owns. Every
-- run of integration/ builds its tables in a fresh one and drops it afterwards.
--
-- What it cannot do: create a schema under any other name, read or write any
-- table in `public` (where the service's data lives), create tables in
-- `public`, or read `auth.users`. `postgres` would be all of those, which is
-- why CI never gets that credential.
--
-- The name rule is the point. A role that may create *any* schema can create
-- one named after another role -- `postgres`, `app_service` -- and every role
-- whose search_path starts with `"$user"` (the default) looks there first, so
-- it would find ci_runner's tables in place of its own. #66 and #70 closed
-- that for the service and for migrations; this closes it for everything.
--
-- The role is created NOLOGIN with no password. Enabling it is a separate,
-- manual step so the password never lands in the repository:
--
--   alter role ci_runner with login password '<generate one>';

do $$ begin
    create role ci_runner nologin;
exception when duplicate_object then null;
end $$;

-- Bounded, so a runaway or leaked CI credential cannot exhaust the free-tier
-- pooler or hold a query open indefinitely.
alter role ci_runner connection limit 5;
alter role ci_runner set statement_timeout = '60s';

-- Database-wide CREATE is what let it name a schema anything. Granted by
-- earlier versions of this file; revoked so re-running it confines the role.
do $$ begin
    execute format('revoke create on database %I from ci_runner', current_database());
end $$;

-- Supabase's `postgres` is not a superuser. Having created ci_runner it holds
-- ADMIN on it but not SET, and `create schema ... authorization ci_runner`
-- needs SET. INHERIT stays off: `postgres` gains nothing of ci_runner's.
do $$ begin
    execute format('grant ci_runner to %I with inherit false, set true', current_user);
end $$;

create schema if not exists ci;
revoke all on schema ci from public;
grant usage on schema ci to ci_runner;

-- The only way ci_runner gets a schema. Runs as this file's caller, so the
-- name check is the whole of the guard. The pattern must match
-- scripts/cleanup_ci_schemas.py, which is what recognises these for deletion.
-- The schema belongs to ci_runner, so ci_runner can fill it and drop it.
create or replace function ci.create_schema(name text) returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
    if name !~ '^ci_[0-9]{10}_[0-9a-f]{8}$' then
        raise exception 'not a generated CI schema name: %', name
            using errcode = 'insufficient_privilege';
    end if;
    execute format('create schema %I authorization ci_runner', name);
end
$$;
revoke all on function ci.create_schema(text) from public;
grant execute on function ci.create_schema(text) to ci_runner;
