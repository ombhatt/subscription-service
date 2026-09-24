"""Whether a Supabase account still exists.

The service never reads `auth.users`: `app_service` has no access to the auth
schema, by design (migration 0005). The one question it does need answered --
has this user been deleted? -- goes through `app_private.missing_accounts`
(migration 0006), which takes ids the caller already holds and returns the ones
with no account. Ids in, ids out; no account data crosses.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AccountDirectory(Protocol):
    async def missing(self, session: AsyncSession, user_ids: Sequence[str]) -> set[str] | None:
        """The ids among `user_ids` that have no account.

        None means "cannot tell" -- not Supabase -- and callers must treat it
        as such, never as "all missing".
        """
        ...


class SupabaseAccounts:
    async def missing(self, session: AsyncSession, user_ids: Sequence[str]) -> set[str] | None:
        if session.bind.dialect.name != "postgresql":
            # SQLite has no auth schema to ask.
            return None
        # Keyed by canonical form, so the answer maps back to the caller's
        # spelling of each id.
        by_uuid: dict[str, str] = {}
        missing: set[str] = set()
        for i in user_ids:
            canonical = _canonical_uuid(i)
            if canonical is None:
                # Supabase ids are UUIDs, so anything else cannot name an
                # account. Filtered here: one bad value would fail the cast for
                # the whole batch.
                missing.add(i)
            else:
                by_uuid[canonical] = i
        # Asked even with nothing to look up: a None answer has to win over the
        # non-UUID ids, or a database that cannot tell would still report them.
        result = await session.execute(
            text("select app_private.missing_accounts(cast(:ids as uuid[]))"),
            {"ids": list(by_uuid)},
        )
        found = result.scalar_one()
        if found is None:
            # Postgres without Supabase: the function exists, auth.users does not.
            return None
        return missing | {by_uuid[str(i)] for i in found}


def _canonical_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None
