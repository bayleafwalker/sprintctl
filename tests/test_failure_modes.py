"""
Failure-mode tests: stale-reservation sweeps, ref integrity, dep edge
cases, state transitions, and context/handoff recovery.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import db, maintain
import sprintctl.cli as cli_module
from sprintctl.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


def _status(conn, item_id, new_status):
    db.set_work_item_status(conn, item_id, new_status, actor="a")


# ---------------------------------------------------------------------------
# Group 1: Reservation — stale sweep edge cases
# ---------------------------------------------------------------------------

class TestStaleReservationSweep:
    def test_sweep_interrupts_stale_reservation_once(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        reservation = db.reserve(conn, iid, actor="agent-a", session_id="session-a")
        conn.execute(
            "UPDATE reservation SET last_activity_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (reservation["id"],),
        )
        conn.commit()
        now = datetime.now(timezone.utc)
        result1 = maintain.sweep(conn, active_sprint["id"], now)
        assert [entry["id"] for entry in result1["stale_reservations_interrupted"]] == [reservation["id"]]
        result2 = maintain.sweep(conn, active_sprint["id"], now)
        assert result2["stale_reservations_interrupted"] == []


# ---------------------------------------------------------------------------
# Group 4: Ref — failure modes
# ---------------------------------------------------------------------------

class TestRefFailureModes:
    def test_add_ref_invalid_type_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid ref_type"):
            db.add_ref(conn, iid, "bogus", "https://example.com")

    def test_add_ref_nonexistent_item_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.add_ref(conn, 9999, "pr", "https://example.com")

    def test_add_ref_invalid_target_url_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid ref URL"):
            db.add_ref(conn, iid, "doc", "bad-target")

    def test_remove_ref_wrong_item_raises(self, conn, active_sprint):
        iid1 = _item(conn, active_sprint["id"], "Item A")
        iid2 = _item(conn, active_sprint["id"], "Item B")
        ref_id = db.add_ref(conn, iid1, "pr", "https://github.com/org/repo/pull/1")
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, ref_id, iid2)

    def test_remove_ref_nonexistent_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, 9999, iid)

    def test_remove_ref_twice_raises_on_second(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        ref_id = db.add_ref(conn, iid, "doc", "https://docs.example.com")
        db.remove_ref(conn, ref_id, iid)
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, ref_id, iid)

    def test_list_refs_deleted_item_returns_empty(self, conn, active_sprint):
        """After item cascade-delete, its refs must be gone."""
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/99")
        conn.execute("DELETE FROM work_item WHERE id = ?", (iid,))
        conn.commit()
        refs = db.list_refs(conn, iid)
        assert refs == []

    def test_multiple_refs_same_item_all_returned(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1")
        db.add_ref(conn, iid, "issue", "https://github.com/org/repo/issues/42")
        db.add_ref(conn, iid, "doc", "https://docs.example.com/design")
        refs = db.list_refs(conn, iid)
        assert len(refs) == 3
        assert {r["ref_type"] for r in refs} == {"pr", "issue", "doc"}

    def test_ref_on_done_item_allowed(self, conn, active_sprint):
        """Refs can be attached to done items — no status restriction."""
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        ref_id = db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/5")
        assert ref_id is not None


# ---------------------------------------------------------------------------
# Group 5: Dep — failure modes and edge cases
# ---------------------------------------------------------------------------

class TestDepFailureModes:
    def test_add_dep_nonexistent_blocker_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            db.add_dep(conn, 9999, iid)

    def test_add_dep_nonexistent_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            db.add_dep(conn, iid, 9999)

    def test_remove_dep_nonexistent_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.remove_dep(conn, 9999, iid)

    def test_remove_dep_wrong_item_raises(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        iid_c = _item(conn, active_sprint["id"], "C")
        dep_id = db.add_dep(conn, iid_a, iid_b)
        with pytest.raises(ValueError, match="not found"):
            db.remove_dep(conn, dep_id, iid_c)

    def test_blocked_item_not_in_ready_list(self, conn, active_sprint):
        iid_blocker = _item(conn, active_sprint["id"], "Blocker")
        iid_blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, iid_blocker, iid_blocked)
        ready_ids = {it["id"] for it in db.get_ready_items(conn, active_sprint["id"])}
        assert iid_blocked not in ready_ids
        assert iid_blocker in ready_ids

    def test_item_becomes_ready_after_blocker_done(self, conn, active_sprint):
        iid_blocker = _item(conn, active_sprint["id"], "Blocker")
        iid_blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, iid_blocker, iid_blocked)
        _status(conn, iid_blocker, "active")
        _status(conn, iid_blocker, "done")
        ready_ids = {it["id"] for it in db.get_ready_items(conn, active_sprint["id"])}
        assert iid_blocked in ready_ids

    def test_dep_deleted_item_cascade(self, conn, active_sprint):
        """Deleting an item must cascade-delete its deps."""
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        conn.execute("DELETE FROM work_item WHERE id = ?", (iid_a,))
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM dep WHERE item_id = ? OR blocked_item_id = ?", (iid_a, iid_a)
        ).fetchall()
        assert rows == []

    def test_ready_items_no_deps_all_pending_included(self, conn, active_sprint):
        iid1 = _item(conn, active_sprint["id"], "Free A")
        iid2 = _item(conn, active_sprint["id"], "Free B")
        ready_ids = {it["id"] for it in db.get_ready_items(conn, active_sprint["id"])}
        assert iid1 in ready_ids
        assert iid2 in ready_ids


# ---------------------------------------------------------------------------
# Group 6: State transition — exhaustive invalid paths
# ---------------------------------------------------------------------------

class TestStateTransitionFailureModes:
    def test_pending_to_done_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "done")

    def test_pending_to_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "blocked")

    def test_done_to_active_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "active")

    def test_done_to_pending_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "pending")

    def test_done_to_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "blocked")

    def test_blocked_to_done_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "blocked")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "done")

    def test_blocked_to_pending_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "blocked")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "pending")

    def test_sprint_planned_to_closed_raises(self, conn):
        sid = db.create_sprint(conn, "P", "", "2026-01-01", "2026-01-31", "planned")
        with pytest.raises(db.InvalidTransition):
            db.set_sprint_status(conn, sid, "closed")

    def test_sprint_closed_to_active_raises(self, conn):
        sid = db.create_sprint(conn, "P", "", "2026-01-01", "2026-01-31", "planned")
        db.set_sprint_status(conn, sid, "active")
        db.set_sprint_status(conn, sid, "closed")
        with pytest.raises(db.InvalidTransition):
            db.set_sprint_status(conn, sid, "active")

    def test_set_item_status_unknown_item_raises(self, conn, active_sprint):
        with pytest.raises(ValueError):
            _status(conn, 9999, "active")

    def test_set_sprint_status_unknown_sprint_raises(self, conn):
        with pytest.raises((ValueError, AttributeError)):
            db.set_sprint_status(conn, 9999, "active")


# ---------------------------------------------------------------------------
# Group 7: Maintain sweep — edge cases not covered elsewhere
# ---------------------------------------------------------------------------

class TestMaintainSweepEdgeCases:
    def test_sweep_unknown_sprint_returns_empty(self, conn):
        """sweep on an unknown sprint_id silently returns empty results (no items to sweep)."""
        result = maintain.sweep(conn, 9999, _now())
        assert result["blocked_items"] == []
        assert result["stale_reservations_interrupted"] == []

    def test_check_unknown_sprint_raises(self, conn):
        with pytest.raises((ValueError, Exception), match="not found"):
            maintain.check(conn, 9999, _now())

    def test_sweep_interrupts_stale_reservations_across_sprints(self, conn):
        """The seven-day reservation maintenance sweep is repository-wide."""
        sid_a = db.create_sprint(conn, "A", "", "2026-01-01", "2026-01-31", "active")
        sid_b = db.create_sprint(conn, "B", "", "2026-02-01", "2026-02-28", "active")
        iid_a = _item(conn, sid_a, "Task A")
        iid_b = _item(conn, sid_b, "Task B")
        reservation_a = db.reserve(conn, iid_a, actor="agent-a", session_id="session-a")
        reservation_b = db.reserve(conn, iid_b, actor="agent-b", session_id="session-b")
        conn.execute("UPDATE reservation SET last_activity_at = '2000-01-01T00:00:00Z'")
        conn.commit()

        result = maintain.sweep(conn, sid_b, datetime.now(timezone.utc))
        assert {entry["id"] for entry in result["stale_reservations_interrupted"]} == {reservation_a["id"], reservation_b["id"]}

    def test_sweep_stale_threshold_env(self, conn, active_sprint, monkeypatch):
        """SPRINTCTL_STALE_THRESHOLD=0 makes all active items immediately stale."""
        monkeypatch.setenv("SPRINTCTL_STALE_THRESHOLD", "0")
        iid = _item(conn, active_sprint["id"], "Active task")
        _status(conn, iid, "active")
        result = maintain.sweep(conn, active_sprint["id"], _now(), threshold=timedelta(seconds=0))
        assert len(result["blocked_items"]) >= 1


# ---------------------------------------------------------------------------
# Group 8: Context and handoff recovery surfaces
# ---------------------------------------------------------------------------

class TestContextAndHandoffRecoveryModes:
    def test_handoff_without_any_sprint_fails_cleanly(self, runner, db_path):
        result = runner.invoke(cli, ["handoff", "--output", "-"])
        assert result.exit_code == 1
        assert "No sprint found" in result.output

    def test_handoff_explicit_sprint_id_succeeds_without_active_sprint(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "Planned", "resume target", "2026-04-01", "2026-04-14", "planned")
        result = runner.invoke(cli, ["handoff", "--sprint-id", str(sid), "--output", "-"])
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)
        assert bundle["sprint"]["id"] == sid
        assert bundle["sprint"]["status"] == "planned"

    def test_handoff_without_git_context_still_emits_typed_bundle(self, runner, conn, active_sprint, db_path, monkeypatch):
        iid = _item(conn, active_sprint["id"], "Task")
        db.create_event(
            conn,
            active_sprint["id"],
            actor="agent",
            event_type="decision",
            source_type="actor",
            work_item_id=iid,
            payload={"summary": "Recovery-safe bundle without git context"},
        )
        monkeypatch.setattr(cli_module, "_detect_git_context", lambda: None)

        result = runner.invoke(cli, ["handoff", "--sprint-id", str(active_sprint["id"]), "--output", "-"])
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)
        assert bundle["bundle_type"] == "handoff"
        assert bundle["bundle_version"] == "1"
        assert bundle["git_context"] is None
        assert bundle["evidence"]["dirty_files"] == []
        assert bundle["freshness"]["dirty_file_count"] == 0
        assert bundle["recent_decisions"][0]["summary"] == "Recovery-safe bundle without git context"
