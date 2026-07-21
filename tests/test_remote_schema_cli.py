"""CLI boundaries for runtime schema checks and deployment migrations."""

from __future__ import annotations

import json

import pytest

from sprintctl import pg, pg_migrations
from sprintctl.cli import cli


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _remote_repo(tmp_path, monkeypatch):
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
    monkeypatch.setenv("SPRINTCTL_URL", "postgresql://example.invalid/work")


def test_normal_remote_command_uses_read_only_startup_handshake(
    tmp_path,
    monkeypatch,
    runner,
):
    _remote_repo(tmp_path, monkeypatch)
    conn = _Connection()
    store = pg.PgStore(conn=conn, repo_id=tmp_path.name)
    observed = {}

    monkeypatch.setattr(pg, "get_connection", lambda _url: store)
    monkeypatch.setattr(
        pg,
        "init_db",
        lambda _store: pytest.fail("normal remote command entered migration"),
    )

    def handshake(value, environ):
        observed["store"] = value
        observed["mode"] = environ.get(pg_migrations.STARTUP_MODE_ENV)
        return {"compatible": True}

    monkeypatch.setattr(pg_migrations, "startup_schema_handshake", handshake)
    monkeypatch.setattr(pg, "list_repos", lambda _conn: ["sprintctl"])

    result = runner.invoke(cli, ["repo", "list"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "sprintctl"
    assert observed == {"store": store, "mode": None}
    assert conn.closed is True


def test_remote_schema_migrate_command_returns_bounded_result(
    tmp_path,
    monkeypatch,
    runner,
):
    _remote_repo(tmp_path, monkeypatch)
    conn = _Connection()
    store = pg.PgStore(conn=conn, repo_id=tmp_path.name)
    monkeypatch.setattr(pg, "get_connection", lambda _url: store)
    monkeypatch.setattr(
        pg_migrations,
        "migrate_schema",
        lambda _store: {
            "schema_version": "sprintctl-remote-migration-result/v1",
            "from_version": 1,
            "to_version": 2,
            "applied_versions": [2],
            "compatibility": {"compatible": True},
        },
    )

    result = runner.invoke(cli, ["remote-schema", "migrate", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["from_version"] == 1
    assert payload["to_version"] == 2
    assert payload["applied_versions"] == [2]
    assert conn.closed is True


def test_remote_schema_connection_error_redacts_credentials(
    tmp_path,
    monkeypatch,
    runner,
):
    _remote_repo(tmp_path, monkeypatch)
    url = "postgresql://runtime:do-not-print@example.invalid/work"
    monkeypatch.setenv("SPRINTCTL_URL", url)

    def fail(value):
        raise RuntimeError(f"could not connect to {value}")

    monkeypatch.setattr(pg, "get_connection", fail)

    result = runner.invoke(cli, ["remote-schema", "check"])

    assert result.exit_code != 0
    assert "do-not-print" not in result.output
    assert "<redacted SPRINTCTL_URL>" in result.output
