"""Tests for the active/blocked -> pending release transition (agentops#2431).

VALID_TRANSITIONS grows a release edge from both open working statuses back
to pending, gated by a required ``reason`` and paired with releasing the
caller's own reservation and an ``item-released`` audit event.
"""
import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


class TestReleaseTransitionTable:
    def test_active_to_pending_with_reason_succeeds(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "pending", reason="rework")
        assert db.get_work_item(conn, iid)["status"] == "pending"

    def test_blocked_to_pending_with_reason_succeeds(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "blocked")
        db.set_work_item_status(conn, iid, "pending", reason="abandoned")
        assert db.get_work_item(conn, iid)["status"] == "pending"

    def test_pending_release_without_reason_rejected(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        with pytest.raises(db.InvalidTransition):
            db.set_work_item_status(conn, iid, "pending")
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_pending_release_with_unknown_reason_rejected(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        with pytest.raises(db.InvalidTransition):
            db.set_work_item_status(conn, iid, "pending", reason="because")
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_reason_rejected_on_non_pending_target(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(db.InvalidTransition):
            db.set_work_item_status(conn, iid, "active", reason="rework")

    def test_pending_to_done_still_rejected(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(db.InvalidTransition):
            db.set_work_item_status(conn, iid, "done")

    def test_done_stays_terminal(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.record_decision(conn, iid, "accept", actor="closer")
        assert db.get_work_item(conn, iid)["status"] == "done"
        with pytest.raises(db.InvalidTransition):
            db.set_work_item_status(conn, iid, "pending", reason="rework")


class TestReleaseReservation:
    def test_release_drops_callers_reservation(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        reservation = db.reserve(conn, iid, actor="worker", session_id="s-1")
        db.set_work_item_status(
            conn, iid, "pending", reason="rework", session_id="s-1"
        )
        [row] = db.list_reservations(conn, iid, active_only=False)
        assert row["id"] == reservation["id"]
        assert row["state"] == "released"

    def test_release_only_drops_the_callers_own_reservation(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        mine = db.reserve(conn, iid, actor="me", session_id="mine")
        theirs = db.reserve(conn, iid, actor="them", session_id="theirs")
        db.set_work_item_status(
            conn, iid, "pending", reason="rework", session_id="mine"
        )
        rows = {row["id"]: row for row in db.list_reservations(conn, iid, active_only=False)}
        assert rows[mine["id"]]["state"] == "released"
        assert rows[theirs["id"]]["state"] == "active"

    def test_release_with_no_reservation_still_succeeds(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(
            conn, iid, "pending", reason="rework", session_id="no-such-session"
        )
        assert db.get_work_item(conn, iid)["status"] == "pending"

    def test_release_with_no_session_id_still_succeeds(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.reserve(conn, iid, actor="worker", session_id="s-1")
        db.set_work_item_status(conn, iid, "pending", reason="rework")
        assert db.get_work_item(conn, iid)["status"] == "pending"
        [row] = db.list_reservations(conn, iid, active_only=True)
        assert row["state"] == "active"


class TestReleaseEvent:
    def test_item_released_event_carries_reason_and_previous_status(
        self, conn, active_sprint
    ):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "pending", reason="partial", actor="releaser")
        events = db.list_events(conn, active_sprint["id"])
        released = [e for e in events if e["event_type"] == "item-released"]
        assert len(released) == 1
        payload = json.loads(released[0]["payload"])
        assert payload["reason"] == "partial"
        assert payload["previous_status"] == "active"
        assert released[0]["actor"] == "releaser"


class TestReleaseReadyQueue:
    def test_released_item_lists_again_in_next_work(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "pending", reason="rework")
        ready = db.get_ready_items(conn, active_sprint["id"])
        assert any(item["id"] == iid for item in ready)


class TestReleaseCli:
    def test_cli_release_requires_reason(self, runner, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        basis = db.item_status_revision(db.get_work_item(conn, iid))
        result = runner.invoke(
            cli,
            [
                "item", "status", "--id", str(iid), "--status", "pending",
                "--expected-revision", basis,
            ],
        )
        assert result.exit_code == 1
        assert "reason" in result.output

    def test_cli_release_with_reason_succeeds(self, runner, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        basis = db.item_status_revision(db.get_work_item(conn, iid))
        result = runner.invoke(
            cli,
            [
                "item", "status", "--id", str(iid), "--status", "pending",
                "--reason", "rework", "--expected-revision", basis,
            ],
        )
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "pending"

    def test_cli_rejects_unknown_reason(self, runner, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active")
        basis = db.item_status_revision(db.get_work_item(conn, iid))
        result = runner.invoke(
            cli,
            [
                "item", "status", "--id", str(iid), "--status", "pending",
                "--reason", "because", "--expected-revision", basis,
            ],
        )
        assert result.exit_code != 0
