from __future__ import annotations

from dataclasses import replace

import pytest

from sprintctl.pg_testing import (
    DISPOSABLE_DATABASE_COMMENT,
    PostgresTestIdentity,
    UnsafePostgresTestTarget,
    new_test_repo_id,
    validate_disposable_identity,
)


@pytest.fixture
def disposable_identity() -> PostgresTestIdentity:
    return PostgresTestIdentity(
        database_name="sprintctl_test_ci",
        role_name="sprintctl_test_ci",
        database_owner="sprintctl_test_ci",
        database_comment=DISPOSABLE_DATABASE_COMMENT,
        role_superuser=False,
        role_create_db=False,
        role_create_role=False,
        role_replication=False,
        role_bypass_rls=False,
    )


def test_disposable_identity_accepts_dedicated_unprivileged_owner(disposable_identity):
    validate_disposable_identity(disposable_identity)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("database_name", "sprintctl", "database name"),
        ("role_name", "sprintctl", "role name"),
        ("database_owner", "postgres", "must own"),
        ("database_comment", None, "database comment"),
        ("role_superuser", True, "SUPERUSER"),
        ("role_create_db", True, "CREATEDB"),
        ("role_create_role", True, "CREATEROLE"),
        ("role_replication", True, "REPLICATION"),
        ("role_bypass_rls", True, "BYPASSRLS"),
    ],
)
def test_disposable_identity_rejects_unsafe_server_fact(
    disposable_identity, field, value, message
):
    with pytest.raises(UnsafePostgresTestTarget, match=message):
        validate_disposable_identity(replace(disposable_identity, **{field: value}))


def test_new_test_repo_id_is_guarded_and_normalized():
    repo_id = new_test_repo_id("Round Trip / Import")
    assert repo_id.startswith("itest-round-trip-import-")
    assert len(repo_id.rsplit("-", 1)[1]) == 12


def _records(path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines()]


def test_fixture_cleanup_appends_started_then_finished_v2_records(tmp_path, monkeypatch):
    from sprintctl import pg_testing

    report = {"schema_version": "sprintctl-pg-cleanup/v1", "cleanup_completed": True, "remaining_rows": {"t": 0}}
    monkeypatch.setattr(pg_testing, "cleanup_test_repositories", lambda conn, repo_ids: dict(report))
    path = tmp_path / "report.jsonl"

    evidence = pg_testing.FixtureCleanup("demo_fixture", "tests/x.py", path)
    assert [r["event"] for r in _records(path)] == ["started"]
    evidence.cleanup(object(), {"itest-a"})
    pg_testing.FixtureCleanup("other", "tests/y.py", path).cleanup(object(), set())

    records = _records(path)
    assert [(r["event"], r["fixture"]) for r in records] == [
        ("started", "demo_fixture"), ("finished", "demo_fixture"),
        ("started", "other"), ("finished", "other"),
    ]
    assert {r["schema_version"] for r in records} == {"sprintctl-pg-cleanup/v2"}
    assert records[1]["remaining_rows"] == {"t": 0}


def test_fixture_cleanup_records_failure_and_reraises(tmp_path, monkeypatch):
    from sprintctl import pg_testing

    def boom(conn, repo_ids):
        raise RuntimeError("residue")

    monkeypatch.setattr(pg_testing, "cleanup_test_repositories", boom)
    path = tmp_path / "report.jsonl"
    evidence = pg_testing.FixtureCleanup("demo_fixture", "tests/x.py", path)
    with pytest.raises(RuntimeError):
        evidence.cleanup(object(), {"itest-a"})
    finished = _records(path)[-1]
    assert finished["event"] == "finished"
    assert finished["cleanup_completed"] is False
    assert finished["error_type"] == "RuntimeError"


def test_fixture_cleanup_writes_nothing_without_a_report_path(tmp_path, monkeypatch):
    from sprintctl import pg_testing

    monkeypatch.delenv("SPRINTCTL_TEST_PG_CLEANUP_REPORT", raising=False)
    monkeypatch.setattr(pg_testing, "cleanup_test_repositories", lambda conn, repo_ids: {"remaining_rows": {}})
    monkeypatch.chdir(tmp_path)
    pg_testing.FixtureCleanup("demo_fixture", "tests/x.py").cleanup(object(), set())
    assert list(tmp_path.iterdir()) == []
