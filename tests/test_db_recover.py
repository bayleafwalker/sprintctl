"""
Unit tests for the local half of `sprintctl db recover-from-remote`:
writing a Postgres-shaped snapshot into a fresh SQLite database with
original IDs preserved. See tests/pg/test_remote_recovery.py for the
Postgres-backed round-trip test.
"""

import json

import pytest

from sprintctl import backend, db
from sprintctl.cli import cli


def _snapshot():
    return {
        "sprint": [
            {
                "id": 407,
                "name": "Recovered Sprint",
                "goal": "goal",
                "start_date": "2026-03-01",
                "end_date": "2026-03-31",
                "status": "planned",
                "created_at": "2026-03-01T00:00:00Z",
                "kind": "backlog",
                "aggregate_uuid": "a0000000-0000-0000-0000-000000000010",
            }
        ],
        "track": [
            {
                "id": 55,
                "sprint_id": 407,
                "name": "eng",
                "description": "",
                "created_at": "2026-03-01T00:00:00Z",
            }
        ],
        "work_item": [
            {
                "id": 1219,
                "track_id": 55,
                "sprint_id": 407,
                "title": "Recovery rehearsal",
                "description": "",
                "status": "pending",
                "assignee": None,
                "priority": None,
                "created_at": "2026-03-01T00:00:00Z",
                "updated_at": "2026-03-01T00:00:00Z",
                "aggregate_uuid": "a0000000-0000-0000-0000-000000000011",
            }
        ],
        "event": [
            {
                "id": 9001,
                "sprint_id": 407,
                "work_item_id": 1219,
                "source_type": "actor",
                "actor": "tester",
                "event_type": "note",
                "payload": {"summary": "restored from remote"},
                "created_at": "2026-03-01T00:00:00Z",
            }
        ],
        "claim": [
            {
                "id": 501,
                "work_item_id": 1219,
                "agent": "tester",
                "claim_type": "execute",
                "exclusive": True,
                "created_at": "2026-03-01T00:00:00Z",
                "expires_at": "2026-03-01T01:00:00Z",
                "heartbeat": "2026-03-01T00:00:00Z",
                "branch": None,
                "worktree_path": None,
                "commit_sha": None,
                "pr_ref": None,
                "claim_token": "tok",
                "runtime_session_id": None,
                "instance_id": None,
                "hostname": None,
                "pid": None,
                "status": "active",
                "lease_epoch": 1,
            }
        ],
        "ref": [
            {
                "id": 77,
                "work_item_id": 1219,
                "ref_type": "doc",
                "url": "docs/plans/x.md",
                "label": "",
                "created_at": "2026-03-01T00:00:00Z",
            }
        ],
        "dep": [],
    }


class TestWriteRecoverySnapshot:
    def test_preserves_original_ids(self, conn):
        counts = db.write_recovery_snapshot(conn, _snapshot())
        assert counts == {
            "sprint": 1,
            "track": 1,
            "work_item": 1,
            "event": 1,
            "claim": 1,
            "ref": 1,
            "dep": 0,
        }
        assert db.get_sprint(conn, 407) is not None
        assert db.get_work_item(conn, 1219)["title"] == "Recovery rehearsal"

    def test_integrity_clean_after_write(self, conn):
        db.write_recovery_snapshot(conn, _snapshot())
        report = db.check_integrity(conn)
        assert report["ok"] is True
        assert report["table_counts"]["claim"] == 1

    def test_boolean_and_json_coercion(self, conn):
        db.write_recovery_snapshot(conn, _snapshot())
        claim = conn.execute("SELECT exclusive FROM claim WHERE id = 501").fetchone()
        assert claim["exclusive"] == 1
        event = conn.execute("SELECT payload FROM event WHERE id = 9001").fetchone()
        assert json.loads(event["payload"])["summary"] == "restored from remote"

    def test_subsequent_event_id_does_not_collide_with_restored_ids(self, conn):
        db.write_recovery_snapshot(conn, _snapshot())
        new_id = db.create_event(conn, 407, "sprintctl", "recovery.completed", source_type="system")
        assert new_id != 9001
        assert new_id > 9001

    def test_repo_id_column_is_tolerated_and_not_written(self, conn):
        snapshot = _snapshot()
        for rows in snapshot.values():
            for row in rows:
                row["repo_id"] = "sprintctl"
        counts = db.write_recovery_snapshot(conn, snapshot)
        assert counts["sprint"] == 1


class TestOwnershipInvalidation:
    def test_active_claim_is_closed_and_token_stripped(self, conn):
        db.write_recovery_snapshot(conn, _snapshot())
        claim = conn.execute(
            "SELECT status, claim_token FROM claim WHERE id = 501"
        ).fetchone()
        assert claim["status"] == "expired"
        assert claim["claim_token"] is None

    def test_expired_claim_keeps_status_but_loses_token(self, conn):
        snapshot = _snapshot()
        snapshot["claim"][0]["status"] = "expired"
        db.write_recovery_snapshot(conn, snapshot)
        claim = conn.execute(
            "SELECT status, claim_token FROM claim WHERE id = 501"
        ).fetchone()
        assert claim["status"] == "expired"
        assert claim["claim_token"] is None


class TestSchemaMismatch:
    def test_missing_column_fails_closed(self, conn):
        snapshot = _snapshot()
        del snapshot["claim"][0]["lease_epoch"]
        with pytest.raises(db.RecoverySchemaMismatch) as exc:
            db.write_recovery_snapshot(conn, snapshot)
        assert exc.value.table == "claim"
        assert exc.value.missing == ["lease_epoch"]
        assert exc.value.unexpected == []

    def test_unexpected_column_fails_closed(self, conn):
        snapshot = _snapshot()
        snapshot["work_item"][0]["future_col"] = "x"
        with pytest.raises(db.RecoverySchemaMismatch) as exc:
            db.write_recovery_snapshot(conn, snapshot)
        assert exc.value.table == "work_item"
        assert exc.value.unexpected == ["future_col"]

    def test_mismatch_rolls_back_everything(self, conn):
        snapshot = _snapshot()
        snapshot["ref"][0]["future_col"] = "x"
        with pytest.raises(db.RecoverySchemaMismatch):
            db.write_recovery_snapshot(
                conn, snapshot, provenance={"source_repo_id": "sprintctl"}
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM sprint").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"] == 0


class TestProvenance:
    def test_recovery_event_written_per_sprint_in_same_transaction(self, conn):
        counts = db.write_recovery_snapshot(
            conn,
            _snapshot(),
            provenance={"recovered_at": "2026-07-24T00:00:00Z", "source_repo_id": "sprintctl"},
        )
        # counts cover source rows only, not the synthetic provenance events
        assert counts["event"] == 1
        rows = conn.execute(
            "SELECT sprint_id, payload FROM event WHERE event_type = 'recovery.completed'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["sprint_id"] == 407
        payload = json.loads(rows[0]["payload"])
        assert payload["source_repo_id"] == "sprintctl"
        assert payload["source_row_counts"]["claim"] == 1
        assert payload["claims_closed"] == 1

    def test_no_provenance_means_no_synthetic_events(self, conn):
        db.write_recovery_snapshot(conn, _snapshot())
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM event WHERE event_type = 'recovery.completed'"
        ).fetchone()["n"]
        assert n == 0


class TestRecoverFromRemoteCLIGuards:
    def test_explicit_recovery_helper_accepts_legacy_remote_configuration(
        self, runner, monkeypatch, tmp_path
    ):
        marker_dir = tmp_path / ".sprintctl"
        marker_dir.mkdir()
        marker_dir.joinpath("backend.json").write_text(
            json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
        monkeypatch.setenv("SPRINTCTL_URL", "postgresql://recovery@example.invalid/work")

        config = backend.require_remote_backend()

        assert config.mode == "remote"
        assert config.repo_id == tmp_path.name
        assert config.url == "postgresql://recovery@example.invalid/work"

    def test_local_backend_is_rejected(self, tmp_path, runner, db_path):
        result = runner.invoke(
            cli, ["db", "recover-from-remote", "--output", str(tmp_path / "out.db")]
        )
        assert result.exit_code != 0
        assert "requires SPRINTCTL_BACKEND=remote" in result.output

    def test_missing_url_is_rejected(self, runner, monkeypatch, tmp_path):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
        monkeypatch.delenv("SPRINTCTL_URL", raising=False)
        result = runner.invoke(
            cli, ["db", "recover-from-remote", "--output", str(tmp_path / "out.db")]
        )
        assert result.exit_code != 0
        assert "requires SPRINTCTL_URL" in result.output
