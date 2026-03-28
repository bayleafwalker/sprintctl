"""
Tests for Phase 2: maintain commands (check, sweep, carryover) and sprint status CLI.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import db, maintain as maint
from sprintctl.cli import cli


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _add_item(conn, sprint_id, track_name, title, assignee=None):
    tid = db.get_or_create_track(conn, sprint_id, track_name)
    return db.create_work_item(conn, sprint_id, tid, title, assignee=assignee)


def _age_item(conn, item_id, hours: float):
    """Back-date updated_at to simulate an idle item."""
    conn.execute(
        "UPDATE work_item SET updated_at = datetime(updated_at, ?) WHERE id = ?",
        (f"-{hours} hours", item_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Group 1: sprint status command
# ---------------------------------------------------------------------------

class TestSprintStatusCmd:
    def test_activate_planned_sprint(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "P1", "", "2026-04-01", "2026-04-30", "planned")
        result = runner.invoke(cli, ["sprint", "status", "--id", str(sid), "--status", "active"])
        assert result.exit_code == 0, result.output
        assert "planned -> active" in result.output
        assert db.get_sprint(conn, sid)["status"] == "active"

    def test_close_active_sprint(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "A1", "", "2026-04-01", "2026-04-30", "active")
        result = runner.invoke(cli, ["sprint", "status", "--id", str(sid), "--status", "closed"])
        assert result.exit_code == 0, result.output
        assert "active -> closed" in result.output
        assert db.get_sprint(conn, sid)["status"] == "closed"

    def test_invalid_planned_to_closed(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "P2", "", "2026-04-01", "2026-04-30", "planned")
        result = runner.invoke(cli, ["sprint", "status", "--id", str(sid), "--status", "closed"])
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_invalid_closed_is_terminal(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "C1", "", "2026-04-01", "2026-04-30", "closed")
        result = runner.invoke(cli, ["sprint", "status", "--id", str(sid), "--status", "active"])
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_sprint_status_unknown_id(self, runner, db_path):
        result = runner.invoke(cli, ["sprint", "status", "--id", "9999", "--status", "closed"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 2: maintain.check (library layer)
# ---------------------------------------------------------------------------

class TestMaintainCheck:
    def test_check_no_stale_items(self, conn, active_sprint):
        now = datetime.now(timezone.utc)
        report = maint.check(conn, active_sprint["id"], now)
        assert report["stale_items"] == []
        assert report["sprint"]["id"] == active_sprint["id"]

    def test_check_detects_stale_active_item(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Stale task")
        db.set_work_item_status(conn, iid, "active")
        _age_item(conn, iid, hours=5)
        now = datetime.now(timezone.utc)
        report = maint.check(conn, active_sprint["id"], now, threshold=timedelta(hours=4))
        stale_ids = [it["id"] for it in report["stale_items"]]
        assert iid in stale_ids

    def test_check_pending_item_not_stale_by_default(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Old pending backlog")
        _age_item(conn, iid, hours=72)
        now = datetime.now(timezone.utc)
        # pending items are never stale unless SPRINTCTL_PENDING_STALE_THRESHOLD is set
        report = maint.check(conn, active_sprint["id"], now, threshold=timedelta(hours=4), pending_threshold=None)
        assert report["stale_items"] == []

    def test_check_pending_item_stale_when_pending_threshold_set(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Old pending backlog")
        _age_item(conn, iid, hours=50)
        now = datetime.now(timezone.utc)
        report = maint.check(conn, active_sprint["id"], now, threshold=timedelta(hours=4), pending_threshold=timedelta(hours=24))
        stale_ids = [it["id"] for it in report["stale_items"]]
        assert iid in stale_ids

    def test_check_includes_track_health(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Item A")
        now = datetime.now(timezone.utc)
        report = maint.check(conn, active_sprint["id"], now)
        assert "backend" in report["track_health"]

    def test_check_invalid_sprint(self, conn):
        with pytest.raises(ValueError, match="not found"):
            maint.check(conn, 9999, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Group 3: maintain.sweep (library layer)
# ---------------------------------------------------------------------------

class TestMaintainSweep:
    def test_sweep_blocks_stale_active_items(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Old task")
        db.set_work_item_status(conn, iid, "active")
        _age_item(conn, iid, hours=6)
        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, active_sprint["id"], now, threshold=timedelta(hours=4))
        assert any(it["id"] == iid for it in result["blocked_items"])
        assert db.get_work_item(conn, iid)["status"] == "blocked"

    def test_sweep_emits_system_event(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Old task")
        db.set_work_item_status(conn, iid, "active")
        _age_item(conn, iid, hours=6)
        now = datetime.now(timezone.utc)
        maint.sweep(conn, active_sprint["id"], now, threshold=timedelta(hours=4))
        events = db.list_events(conn, active_sprint["id"])
        auto_blocked = [e for e in events if e["event_type"] == "auto-blocked-stale"]
        assert len(auto_blocked) == 1
        assert auto_blocked[0]["source_type"] == "system"
        assert auto_blocked[0]["actor"] == "maintain-sweep"

    def test_sweep_skips_non_stale_items(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Fresh task")
        db.set_work_item_status(conn, iid, "active")
        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, active_sprint["id"], now, threshold=timedelta(hours=4))
        assert result["blocked_items"] == []
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_sweep_idempotent_already_blocked(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Task")
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "blocked")
        _age_item(conn, iid, hours=10)
        now = datetime.now(timezone.utc)
        # Already blocked — sweep should not touch it (only sweeps active items)
        result = maint.sweep(conn, active_sprint["id"], now, threshold=timedelta(hours=4))
        assert result["blocked_items"] == []

    def test_sweep_auto_close_overdue_no_active(self, conn):
        sid = db.create_sprint(conn, "Past", "", "2025-01-01", "2025-01-31", "active")
        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, sid, now, auto_close=True)
        assert result["auto_closed"] is True
        assert db.get_sprint(conn, sid)["status"] == "closed"

    def test_sweep_auto_close_skipped_if_active_items(self, conn):
        sid = db.create_sprint(conn, "Past2", "", "2025-01-01", "2025-01-31", "active")
        iid = _add_item(conn, sid, "t", "Unfinished")
        db.set_work_item_status(conn, iid, "active")
        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, sid, now, threshold=timedelta(hours=0), auto_close=True)
        # The fresh item won't be stale (threshold=0 makes everything stale, but let's use large threshold)
        # Re-run with large threshold so item stays active
        result2 = maint.sweep(conn, sid, now, threshold=timedelta(hours=999), auto_close=True)
        assert result2["auto_closed"] is False


# ---------------------------------------------------------------------------
# Group 4: maintain.carryover (library layer)
# ---------------------------------------------------------------------------

class TestMaintainCarryover:
    def _setup_two_sprints(self, conn):
        s1 = db.create_sprint(conn, "Sprint 1", "", "2026-03-01", "2026-03-31", "active")
        s2 = db.create_sprint(conn, "Sprint 2", "", "2026-04-01", "2026-04-30", "planned")
        return s1, s2

    def test_carryover_moves_incomplete_items(self, conn):
        s1, s2 = self._setup_two_sprints(conn)
        iid1 = _add_item(conn, s1, "backend", "Pending task")
        iid2 = _add_item(conn, s1, "backend", "Active task")
        db.set_work_item_status(conn, iid2, "active")
        iid3 = _add_item(conn, s1, "frontend", "Done task")
        db.set_work_item_status(conn, iid3, "active")
        db.set_work_item_status(conn, iid3, "done")

        created = maint.carryover(conn, s1, s2)
        assert len(created) == 2
        titles = {it["title"] for it in created}
        assert titles == {"Pending task", "Active task"}

    def test_carryover_does_not_move_done_items(self, conn):
        s1, s2 = self._setup_two_sprints(conn)
        iid = _add_item(conn, s1, "backend", "Finished")
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "done")
        created = maint.carryover(conn, s1, s2)
        assert created == []

    def test_carryover_marks_originals_done(self, conn):
        s1, s2 = self._setup_two_sprints(conn)
        iid = _add_item(conn, s1, "backend", "Carry me")
        maint.carryover(conn, s1, s2)
        assert db.get_work_item(conn, iid)["status"] == "done"

    def test_carryover_preserves_track_name(self, conn):
        s1, s2 = self._setup_two_sprints(conn)
        _add_item(conn, s1, "special-track", "Work")
        created = maint.carryover(conn, s1, s2)
        assert len(created) == 1
        new_item = created[0]
        # Verify the new item is in the right track in s2
        tracks = db.list_tracks(conn, s2)
        assert any(t["name"] == "special-track" for t in tracks)

    def test_carryover_emits_events_on_both_sprints(self, conn):
        s1, s2 = self._setup_two_sprints(conn)
        _add_item(conn, s1, "backend", "Task")
        maint.carryover(conn, s1, s2)
        events_s1 = db.list_events(conn, s1)
        events_s2 = db.list_events(conn, s2)
        assert any(e["event_type"] == "carried-out" for e in events_s1)
        assert any(e["event_type"] == "carryover" for e in events_s2)

    def test_carryover_same_sprint_raises(self, conn):
        s1, _ = self._setup_two_sprints(conn)
        with pytest.raises(ValueError, match="differ"):
            maint.carryover(conn, s1, s1)

    def test_carryover_invalid_source_raises(self, conn):
        _, s2 = self._setup_two_sprints(conn)
        with pytest.raises(ValueError, match="not found"):
            maint.carryover(conn, 9999, s2)

    def test_carryover_no_items_returns_empty(self, conn):
        s1, s2 = self._setup_two_sprints(conn)
        result = maint.carryover(conn, s1, s2)
        assert result == []


# ---------------------------------------------------------------------------
# Group 5: maintain CLI commands
# ---------------------------------------------------------------------------

class TestMaintainCLI:
    def test_check_cmd_output(self, runner, conn, active_sprint):
        result = runner.invoke(cli, ["maintain", "check", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Sprint #" in result.output
        assert "Stale items" in result.output
        assert "Track health" in result.output

    def test_check_cmd_no_active_sprint(self, runner, db_path):
        result = runner.invoke(cli, ["maintain", "check"])
        assert result.exit_code == 1

    def test_sweep_cmd_no_stale(self, runner, active_sprint):
        result = runner.invoke(cli, ["maintain", "sweep", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "No stale items" in result.output

    def test_sweep_cmd_blocks_stale_item(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "backend", "Old work")
        db.set_work_item_status(conn, iid, "active")
        _age_item(conn, iid, hours=6)
        result = runner.invoke(
            cli,
            ["maintain", "sweep", "--sprint-id", str(active_sprint["id"]), "--threshold", "4h"],
        )
        assert result.exit_code == 0, result.output
        assert "Blocked 1 stale item" in result.output
        assert db.get_work_item(conn, iid)["status"] == "blocked"

    def test_carryover_cmd(self, runner, conn, db_path):
        s1 = db.create_sprint(conn, "Sprint 1", "", "2026-03-01", "2026-03-31", "active")
        s2 = db.create_sprint(conn, "Sprint 2", "", "2026-04-01", "2026-04-30", "planned")
        _add_item(conn, s1, "backend", "Unfinished")
        result = runner.invoke(
            cli,
            ["maintain", "carryover", "--from-sprint", str(s1), "--to-sprint", str(s2)],
        )
        assert result.exit_code == 0, result.output
        assert "Carried 1 item" in result.output

    def test_carryover_cmd_nothing_to_carry(self, runner, conn, db_path):
        s1 = db.create_sprint(conn, "Sprint 1", "", "2026-03-01", "2026-03-31", "active")
        s2 = db.create_sprint(conn, "Sprint 2", "", "2026-04-01", "2026-04-30", "planned")
        result = runner.invoke(
            cli,
            ["maintain", "carryover", "--from-sprint", str(s1), "--to-sprint", str(s2)],
        )
        assert result.exit_code == 0, result.output
        assert "No incomplete items" in result.output

    def test_carryover_cmd_invalid_source(self, runner, conn, db_path):
        s2 = db.create_sprint(conn, "Sprint 2", "", "2026-04-01", "2026-04-30", "planned")
        result = runner.invoke(
            cli,
            ["maintain", "carryover", "--from-sprint", "9999", "--to-sprint", str(s2)],
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 6: migration 2 (claim table)
# ---------------------------------------------------------------------------

class TestMigration2:
    def test_claim_table_exists_after_init(self, conn):
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "claim" in tables

    def test_schema_version_is_5(self, conn):
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 5


# ---------------------------------------------------------------------------
# Group 7: claim expiry purge in sweep
# ---------------------------------------------------------------------------

class TestSweepPurgesExpiredClaims:
    def test_sweep_purges_expired_claim(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "eng", "Claimed task")
        # Insert a claim with an already-expired TTL (1 second, then back-date it)
        cid = db.create_claim(conn, iid, agent="agent-a", ttl_seconds=1)
        conn.execute(
            "UPDATE claim SET expires_at = datetime('now', '-10 seconds') WHERE id = ?", (cid,)
        )
        conn.commit()
        # Confirm claim exists before sweep
        row = conn.execute("SELECT id FROM claim WHERE id = ?", (cid,)).fetchone()
        assert row is not None

        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, active_sprint["id"], now, threshold=timedelta(hours=99))
        assert result["expired_claims_purged"] == 1

        row = conn.execute("SELECT id FROM claim WHERE id = ?", (cid,)).fetchone()
        assert row is None

    def test_sweep_does_not_purge_active_claim(self, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "eng", "Active task")
        cid = db.create_claim(conn, iid, agent="agent-a", ttl_seconds=3600)
        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, active_sprint["id"], now, threshold=timedelta(hours=99))
        assert result["expired_claims_purged"] == 0
        row = conn.execute("SELECT id FROM claim WHERE id = ?", (cid,)).fetchone()
        assert row is not None

    def test_sweep_only_purges_claims_in_sprint(self, conn):
        s1 = db.create_sprint(conn, "S1", "", "2026-04-01", "2026-04-30", "active")
        s2 = db.create_sprint(conn, "S2", "", "2026-04-01", "2026-04-30", "active")
        tid1 = db.get_or_create_track(conn, s1, "eng")
        tid2 = db.get_or_create_track(conn, s2, "eng")
        iid1 = db.create_work_item(conn, s1, tid1, "S1 task")
        iid2 = db.create_work_item(conn, s2, tid2, "S2 task")
        cid1 = db.create_claim(conn, iid1, agent="a", ttl_seconds=1)
        cid2 = db.create_claim(conn, iid2, agent="a", ttl_seconds=1)
        conn.execute("UPDATE claim SET expires_at = datetime('now', '-5 seconds')")
        conn.commit()
        now = datetime.now(timezone.utc)
        result = maint.sweep(conn, s1, now, threshold=timedelta(hours=99))
        # Only s1's claim purged
        assert result["expired_claims_purged"] == 1
        assert conn.execute("SELECT id FROM claim WHERE id = ?", (cid1,)).fetchone() is None
        assert conn.execute("SELECT id FROM claim WHERE id = ?", (cid2,)).fetchone() is not None

    def test_maintain_sweep_cli_reports_purged_claims(self, runner, conn, active_sprint, db_path):
        iid = _add_item(conn, active_sprint["id"], "eng", "Claimed task")
        cid = db.create_claim(conn, iid, agent="agent-a", ttl_seconds=1)
        conn.execute(
            "UPDATE claim SET expires_at = datetime('now', '-10 seconds') WHERE id = ?", (cid,)
        )
        conn.commit()
        result = runner.invoke(cli, ["maintain", "sweep", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Purged 1 expired claim" in result.output
