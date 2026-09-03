from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime | None) -> datetime | None:
    """Normalise a database timestamp to an aware UTC datetime.

    Postgres `timestamptz` comes back aware; SQLite (the test suite) hands back
    naive values for the same column. Comparing the two raises, so everything
    that does date arithmetic on a stored timestamp goes through here.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
