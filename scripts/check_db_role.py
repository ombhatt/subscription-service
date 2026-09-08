"""What can the credential in DATABASE_URL actually do?

Run this after changing DATABASE_URL, and again whenever you want to be sure
production is connecting as the role you think it is:

    .venv/bin/python -m scripts.check_db_role

It answers three questions the connection string itself does not:

  * who am I, and what role attributes do I carry
  * can I reach anything outside this service -- auth, storage, vault
  * can I still do the service's own work

The last one matters as much as the first two. A credential locked down so far
that the application breaks is not a win, and the failure would otherwise show
up as a 500 on somebody's checkout.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.db import dispose_engine, get_sessionmaker

# Everything the running service reads or writes.
APP_TABLES = (
    "subscriptions",
    "processed_events",
    "usage_counters",
    "entitlement_grants",
    "subscription_audit",
    "sales_inquiries",
)

# Schemas this service has no business touching. Reaching them means a leaked
# DATABASE_URL exposes every account in the project, not just billing rows.
FOREIGN_SCHEMAS = ("auth", "storage", "vault")


async def main() -> int:
    problems: list[str] = []
    async with get_sessionmaker()() as session:
        user = (await session.execute(text("select current_user"))).scalar()
        attrs = (
            await session.execute(
                text(
                    """select rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
                       from pg_roles where rolname = current_user"""
                )
            )
        ).one()
        print(f"connected as: {user}")

        for name, value in zip(
            ("superuser", "createrole", "createdb", "bypassrls"), attrs, strict=True
        ):
            flag = "  <-- more than this service needs" if value else ""
            print(f"  {name:<12} {value}{flag}")
            if value:
                problems.append(f"role has {name}")

        print("\nreach outside this service:")
        for schema in FOREIGN_SCHEMAS:
            can = (
                await session.execute(
                    text("select has_schema_privilege(current_user, :s, 'USAGE')"),
                    {"s": schema},
                )
            ).scalar()
            print(f"  {schema:<10} {'REACHABLE  <-- should not be' if can else 'unreachable'}")
            if can:
                problems.append(f"can reach the {schema} schema")

        print("\ncan it do the service's work:")
        for table in APP_TABLES:
            try:
                await session.execute(text(f'select 1 from public."{table}" limit 1'))
                print(f"  {table:<22} readable")
            except Exception as exc:
                print(f"  {table:<22} DENIED -- {type(exc).__name__}")
                problems.append(f"cannot read {table}")

    # A write, then undone. Reading proves less than half of it: RLS and grants
    # can allow SELECT and refuse INSERT, and the service does both.
    try:
        async with get_sessionmaker()() as session:
            await session.execute(
                text(
                    "insert into public.sales_inquiries (id, email, source) "
                    "values ('rolecheck-probe', 'probe@example.invalid', 'pricing_page')"
                )
            )
            await session.rollback()
        print("  write probe             ok (rolled back)")
    except Exception as exc:
        print(f"  write probe             DENIED -- {type(exc).__name__}")
        problems.append("cannot write")

    await dispose_engine()

    print()
    if problems:
        print("NOT the scoped setup:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("scoped correctly: this credential can do its job and nothing else.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
