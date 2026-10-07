"""The cleanup job runs against the project that holds the service's real data,
so what it is willing to drop is the part worth pinning down."""

from __future__ import annotations

import pathlib
import re

from scripts.cleanup_ci_schemas import PREFIX, new_schema_name, stale

NOW = 1_800_000_000.0


def test_a_generated_name_is_recognised_once_it_is_old_enough():
    old = new_schema_name(now=NOW - 2 * 60 * 60)
    fresh = new_schema_name(now=NOW - 5 * 60)
    assert old.startswith(PREFIX)
    assert stale([old, fresh], now=NOW, older_than_minutes=60) == [old]


def test_a_schema_is_stale_only_once_strictly_older_than_the_cutoff():
    exactly_an_hour = new_schema_name(now=NOW - 60 * 60)
    a_second_more = new_schema_name(now=NOW - 60 * 60 - 1)
    assert stale([exactly_an_hour, a_second_more], now=NOW, older_than_minutes=60) == [
        a_second_more
    ]


def test_nothing_that_is_not_a_generated_name_is_ever_a_candidate():
    """Including schemas that merely look close -- a hand-made `ci_` schema is
    somebody's work, not an orphan."""
    names = [
        "public",
        "auth",
        "storage",
        "ci_verify_probe",
        "ci_1700000000",
        "ci_1700000000_nothex!!",
        "ci_1700000000_abcdef012",
        "xci_1700000000_abcdef01",
        "CI_1700000000_abcdef01",
    ]
    assert stale(names, now=NOW, older_than_minutes=0) == []


def test_generated_names_are_unique_within_the_same_second():
    names = {new_schema_name(now=NOW) for _ in range(200)}
    assert len(names) == 200


def test_the_database_creates_exactly_the_names_this_module_recognises():
    """ci_runner gets schemas only through ci.create_schema(), which checks the
    name against its own copy of the pattern. If the two drift, CI either cannot
    create its schemas or leaves ones behind that cleanup never matches."""
    sql = (pathlib.Path(__file__).parents[1] / "scripts" / "ci_db_role.sql").read_text()
    [pattern] = re.findall(r"name !~ '([^']+)'", sql)
    allowed = re.compile(pattern)

    for when in (NOW, NOW - 1, 1_000_000_000):
        assert allowed.search(new_schema_name(now=when))
    for name in ["public", "postgres", "app_service", "ci_probe", "ci_1700000000_ABCDEF01"]:
        assert not allowed.search(name)
        assert stale([name], now=NOW, older_than_minutes=0) == []
