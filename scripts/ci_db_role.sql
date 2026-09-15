-- The database role the integration suite uses in CI. Run once, as the admin
-- credential (MIGRATION_DATABASE_URL). Idempotent.
--
-- Not an Alembic migration on purpose: this is test infrastructure for one
-- Supabase project, and a migration would create a CI login in every database
-- the service is ever deployed to -- production included.
--
-- What it can do: create schemas, and do anything inside the schemas it
-- creates. Every run of integration/ builds its tables in a fresh
-- `ci_<epoch>_<random>` schema and drops it afterwards.
--
-- What it cannot do, verified when this was written: read or write any table
-- in `public` (where the service's data lives), create tables in `public`, or
-- read `auth.users`. `postgres` would be all of those, which is why CI never
-- gets that credential.
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

do $$ begin
    execute format('grant create on database %I to ci_runner', current_database());
end $$;
