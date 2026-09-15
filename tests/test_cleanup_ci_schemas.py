"""The cleanup job runs against the project that holds the service's real data,
so what it is willing to drop is the part worth pinning down."""

from __future__ import annotations

from scripts.cleanup_ci_schemas import PREFIX, new_schema_name, stale

NOW = 1_800_000_000.0


def test_a_generated_name_is_recognised_once_it_is_old_enough():
    old = new_schema_name(now=NOW - 2 * 60 * 60)
    fresh = new_schema_name(now=NOW - 5 * 60)
    assert old.startswith(PREFIX)
    assert stale([old, fresh], now=NOW, older_than_minutes=60) == [old]


def test_a_running_job_is_left_alone():
    running = new_schema_name(now=NOW - 59 * 60)
    assert stale([running], now=NOW, older_than_minutes=60) == []


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
