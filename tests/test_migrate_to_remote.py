"""
Tests for migrate-to-remote: NDJSON export, import logic, and freeze behavior.

These tests do NOT require a real postgres connection. They test:
- export_ndjson output format and content
- The freeze/sentinel/marker creation paths
- CLI behavior for migrate-to-remote (dry-run, error paths)
"""

import io
import json
import os
from pathlib import Path

import pytest

from sprintctl import db
from sprintctl.pg import export_ndjson
from sprintctl.cli import cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_dir(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".sprintctl").mkdir()
    return repo


@pytest.fixture
def sqlite_db(repo_dir):
    db_path = repo_dir / ".sprintctl" / "sprintctl.db"
    conn = db.get_connection(db_path)
    db.init_db(conn)
    return conn, db_path


@pytest.fixture
def populated_sqlite(sqlite_db):
    conn, db_path = sqlite_db
    sid = db.create_sprint(conn, "Sprint 1", "Goal", "2026-01-01", "2026-03-31", "active")
    tid = db.get_or_create_track(conn, sid, "eng")
    iid = db.create_work_item(conn, sid, tid, "Task A")
    db.create_event(conn, sid, actor="agent", event_type="note", source_type="actor",
                    work_item_id=iid, payload={"summary": "working on it"})
    db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1")
    conn.commit()
    return conn, db_path, sid, iid


# ---------------------------------------------------------------------------
# export_ndjson tests
# ---------------------------------------------------------------------------

class TestExportNdjson:
    def test_empty_db_produces_zero_counts(self, sqlite_db):
        conn, _ = sqlite_db
        buf = io.StringIO()
        counts = export_ndjson(conn, "myrepo", buf)
        for table in ("sprint", "track", "work_item", "event", "claim", "ref", "dep"):
            assert counts[table] == 0

    def test_populated_db_exports_expected_counts(self, populated_sqlite):
        conn, _, sid, iid = populated_sqlite
        buf = io.StringIO()
        counts = export_ndjson(conn, "myrepo", buf)
        assert counts["sprint"] == 1
        assert counts["track"] == 1
        assert counts["work_item"] == 1
        assert counts["event"] == 1
        assert counts["ref"] == 1
        assert counts["claim"] == 0
        assert counts["dep"] == 0

    def test_ndjson_lines_have_required_keys(self, populated_sqlite):
        conn, _, sid, iid = populated_sqlite
        buf = io.StringIO()
        export_ndjson(conn, "myrepo", buf)
        lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        for record in lines:
            assert "table" in record
            assert "repo_id" in record
            assert "data" in record
            assert record["repo_id"] == "myrepo"

    def test_ndjson_preserves_sprint_id(self, populated_sqlite):
        conn, _, sid, iid = populated_sqlite
        buf = io.StringIO()
        export_ndjson(conn, "myrepo", buf)
        lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        sprint_records = [r for r in lines if r["table"] == "sprint"]
        assert len(sprint_records) == 1
        assert sprint_records[0]["data"]["id"] == sid

    def test_ndjson_event_payload_is_dict(self, populated_sqlite):
        conn, _, sid, iid = populated_sqlite
        buf = io.StringIO()
        export_ndjson(conn, "myrepo", buf)
        lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        event_records = [r for r in lines if r["table"] == "event"]
        assert len(event_records) == 1
        # Payload should be a deserialized dict, not a JSON string
        assert isinstance(event_records[0]["data"]["payload"], dict)

    def test_ndjson_tables_in_dependency_order(self, populated_sqlite):
        conn, _, sid, iid = populated_sqlite
        buf = io.StringIO()
        export_ndjson(conn, "myrepo", buf)
        lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        tables_seen = [r["table"] for r in lines]
        # sprint must come before track, track before work_item, work_item before event
        def idx(t):
            return next((i for i, x in enumerate(tables_seen) if x == t), -1)
        assert idx("sprint") < idx("track") < idx("work_item")
        assert idx("work_item") < idx("ref")

    def test_empty_db_ndjson_has_no_lines(self, sqlite_db):
        conn, _ = sqlite_db
        buf = io.StringIO()
        export_ndjson(conn, "myrepo", buf)
        assert buf.getvalue().strip() == ""


# ---------------------------------------------------------------------------
# Freeze / sentinel / marker tests (tested via CLI with mocked pg)
# ---------------------------------------------------------------------------

class TestMigrateToRemoteCliDryRun:
    def test_dry_run_exits_zero(self, repo_dir, runner, monkeypatch):
        db_path = repo_dir / ".sprintctl" / "sprintctl.db"
        conn = db.get_connection(db_path)
        db.init_db(conn)
        conn.close()

        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        monkeypatch.chdir(repo_dir)

        # Mock pg.get_connection and pg.init_db
        import sprintctl.pg as _pg
        from unittest.mock import MagicMock, patch

        mock_store = MagicMock()
        mock_store.conn.cursor.return_value.__enter__ = lambda s: mock_store.conn.cursor()
        mock_store.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.conn.cursor().fetchone.return_value = {"cnt": 0}
        mock_store.conn.commit = MagicMock()
        mock_store.repo_id = "myrepo"

        with patch.object(_pg, "get_connection", return_value=mock_store), \
             patch.object(_pg, "init_db"):
            result = runner.invoke(cli, [
                "migrate-to-remote",
                "--url", "postgresql://localhost/test",
                "--dry-run",
            ])
        assert result.exit_code == 0, result.output
        assert "dry" in result.output.lower() or "myrepo" in result.output

    def test_missing_sqlite_errors(self, repo_dir, runner, monkeypatch):
        monkeypatch.setenv("SPRINTCTL_DB", str(repo_dir / ".sprintctl" / "nothere.db"))
        monkeypatch.chdir(repo_dir)

        result = runner.invoke(cli, [
            "migrate-to-remote",
            "--url", "postgresql://localhost/test",
        ])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "source" in result.output.lower()

    def test_missing_url_errors(self, repo_dir, runner, monkeypatch):
        db_path = repo_dir / ".sprintctl" / "sprintctl.db"
        conn = db.get_connection(db_path)
        db.init_db(conn)
        conn.close()
        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        monkeypatch.chdir(repo_dir)
        monkeypatch.delenv("SPRINTCTL_URL", raising=False)

        result = runner.invoke(cli, ["migrate-to-remote"])
        assert result.exit_code != 0
        assert "url" in result.output.lower() or "URL" in result.output

    def test_repo_id_assert_mismatch_errors(self, repo_dir, runner, monkeypatch):
        db_path = repo_dir / ".sprintctl" / "sprintctl.db"
        conn = db.get_connection(db_path)
        db.init_db(conn)
        conn.close()
        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        monkeypatch.chdir(repo_dir)

        result = runner.invoke(cli, [
            "migrate-to-remote",
            "--url", "postgresql://localhost/test",
            "--repo-id", "wrong-repo-id",
        ])
        assert result.exit_code != 0
        assert "wrong-repo-id" in result.output or "mismatch" in result.output.lower()


class TestFreezeArtifacts:
    def test_dry_run_does_not_freeze(self, repo_dir, runner, monkeypatch):
        """--dry-run must not rename sqlite or create backend.json."""
        db_path = repo_dir / ".sprintctl" / "sprintctl.db"
        conn = db.get_connection(db_path)
        db.init_db(conn)
        conn.close()
        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        monkeypatch.chdir(repo_dir)

        import sprintctl.pg as _pg
        from unittest.mock import MagicMock, patch

        mock_store = MagicMock()
        mock_store.conn.cursor.return_value.__enter__ = lambda s: mock_store.conn.cursor()
        mock_store.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_store.conn.cursor().fetchone.return_value = {"cnt": 0}
        mock_store.conn.commit = MagicMock()
        mock_store.repo_id = "myrepo"

        with patch.object(_pg, "get_connection", return_value=mock_store), \
             patch.object(_pg, "init_db"):
            result = runner.invoke(cli, [
                "migrate-to-remote",
                "--url", "postgresql://localhost/test",
                "--dry-run", "--yes",
            ])

        assert result.exit_code == 0, result.output
        # sqlite must still exist
        assert db_path.is_file(), "sqlite should not have been renamed on dry-run"
        # backend.json must not exist
        assert not (repo_dir / ".sprintctl" / "backend.json").exists()

    def test_real_migration_uses_explicit_trusted_state_transfer(
        self,
        repo_dir,
        runner,
        monkeypatch,
    ):
        db_path = repo_dir / ".sprintctl" / "sprintctl.db"
        conn = db.get_connection(db_path)
        db.init_db(conn)
        db.create_sprint(conn, "Migration", status="active")
        conn.close()
        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        monkeypatch.chdir(repo_dir)

        import sprintctl.pg as _pg
        from unittest.mock import MagicMock, patch

        mock_store = MagicMock()
        mock_cursor = mock_store.conn.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = {"cnt": 0}
        mock_store.repo_id = "myrepo"

        with patch.object(_pg, "get_connection", return_value=mock_store), \
             patch.object(_pg, "init_db"), \
             patch.object(_pg, "import_ndjson", return_value={}) as import_mock:
            result = runner.invoke(
                cli,
                ["migrate-to-remote", "--url", "postgresql://localhost/test", "--yes"],
            )

        assert result.exit_code == 0, result.output
        assert import_mock.call_args.kwargs["trusted_state_transfer"] is True
