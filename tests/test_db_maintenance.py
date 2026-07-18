"""
Tests for database maintenance (db vacuum, db integrity) on the local backend.
"""

import json

from sprintctl import db
from sprintctl.cli import cli


def _seed(conn):
    sid = db.create_sprint(conn, "S1", "goal", "2026-03-01", "2026-03-31", "active")
    tid = db.get_or_create_track(conn, sid, "eng")
    iid = db.create_work_item(conn, sid, tid, "task")
    return sid, tid, iid


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

class TestVacuumDB:
    def test_vacuum_reports_pages(self, conn):
        _seed(conn)
        report = db.vacuum_database(conn)
        assert report["backend"] == "local"
        assert report["pages_after"] > 0
        assert report["pages_after"] <= report["pages_before"]
        assert report["bytes_reclaimed"] >= 0

    def test_vacuum_reclaims_deleted_rows(self, conn):
        sid, tid, _ = _seed(conn)
        for i in range(500):
            db.create_work_item(conn, sid, tid, f"bulk {i}", description="x" * 512)
        conn.execute("DELETE FROM work_item WHERE title LIKE 'bulk %'")
        conn.commit()
        report = db.vacuum_database(conn)
        assert report["pages_after"] < report["pages_before"]
        assert report["bytes_reclaimed"] > 0


class TestIntegrityDB:
    def test_clean_db_is_ok(self, conn):
        _seed(conn)
        report = db.check_integrity(conn)
        assert report["ok"] is True
        assert report["integrity_check"] == ["ok"]
        assert report["foreign_key_violations"] == []
        assert report["table_counts"]["work_item"] == 1
        assert report["table_counts"]["sprint"] == 1

    def test_orphaned_fk_detected(self, conn):
        _seed(conn)
        # Insert an orphan with FK enforcement off, as corruption would leave it.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO work_item (track_id, sprint_id, title, aggregate_uuid)"
            " VALUES (99999, 99999, 'orphan', 'a0000000-0000-0000-0000-000000000001')"
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        report = db.check_integrity(conn)
        assert report["ok"] is False
        assert report["foreign_key_violations"]


# ---------------------------------------------------------------------------
# CLI surfaces
# ---------------------------------------------------------------------------

class TestMaintenanceCLI:
    def test_db_vacuum_text(self, runner, conn, db_path):
        _seed(conn)
        result = runner.invoke(cli, ["db", "vacuum"])
        assert result.exit_code == 0, result.output
        assert "VACUUM complete" in result.output

    def test_db_vacuum_json(self, runner, conn, db_path):
        _seed(conn)
        result = runner.invoke(cli, ["db", "vacuum", "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["backend"] == "local"
        assert "pages_after" in report

    def test_db_integrity_text_ok(self, runner, conn, db_path):
        _seed(conn)
        result = runner.invoke(cli, ["db", "integrity"])
        assert result.exit_code == 0, result.output
        assert "Integrity: ok" in result.output
        assert "work_item=1" in result.output

    def test_db_integrity_json_ok(self, runner, conn, db_path):
        _seed(conn)
        result = runner.invoke(cli, ["db", "integrity", "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["ok"] is True

    def test_db_integrity_failure_exits_nonzero(self, runner, conn, db_path):
        _seed(conn)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO work_item (track_id, sprint_id, title, aggregate_uuid)"
            " VALUES (99999, 99999, 'orphan', 'a0000000-0000-0000-0000-000000000002')"
        )
        conn.commit()
        result = runner.invoke(cli, ["db", "integrity"])
        assert result.exit_code == 1
        assert "FAILED" in result.output
