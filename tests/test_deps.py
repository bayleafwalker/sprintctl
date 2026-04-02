"""
Tests for the dependencies model (schema v8): item deps and next-work suggestion.
"""

import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task", status="pending"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    iid = db.create_work_item(conn, sprint_id, tid, title)
    if status != "pending":
        conn.execute(
            "UPDATE work_item SET status = ? WHERE id = ?", (status, iid)
        )
        conn.commit()
    return iid


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

class TestDepDB:
    def test_add_dep_basic(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        dep_id = db.add_dep(conn, iid_a, iid_b)
        assert dep_id > 0

    def test_dep_self_reference_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="cannot depend on itself"):
            db.add_dep(conn, iid, iid)

    def test_dep_missing_blocker_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.add_dep(conn, 9999, iid)

    def test_dep_missing_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.add_dep(conn, iid, 9999)

    def test_add_dep_duplicate_is_idempotent(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        dep_id1 = db.add_dep(conn, iid_a, iid_b)
        dep_id2 = db.add_dep(conn, iid_a, iid_b)
        assert dep_id1 == dep_id2

    def test_list_deps_blocking(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        blockers = db.list_deps_blocking(conn, iid_b)
        assert len(blockers) == 1
        assert blockers[0]["item_id"] == iid_a
        assert blockers[0]["blocker_title"] == "A"
        assert blockers[0]["blocker_status"] == "pending"

    def test_list_deps_blocked_by(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        waiting = db.list_deps_blocked_by(conn, iid_a)
        assert len(waiting) == 1
        assert waiting[0]["blocked_item_id"] == iid_b
        assert waiting[0]["waiting_title"] == "B"

    def test_list_deps_blocking_empty(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        assert db.list_deps_blocking(conn, iid) == []

    def test_list_deps_blocked_by_empty(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        assert db.list_deps_blocked_by(conn, iid) == []

    def test_remove_dep(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        dep_id = db.add_dep(conn, iid_a, iid_b)
        db.remove_dep(conn, dep_id, iid_a)
        assert db.list_deps_blocking(conn, iid_b) == []

    def test_remove_dep_via_blocked_side(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        dep_id = db.add_dep(conn, iid_a, iid_b)
        db.remove_dep(conn, dep_id, iid_b)
        assert db.list_deps_blocking(conn, iid_b) == []

    def test_remove_dep_not_found_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.remove_dep(conn, 9999, iid)

    def test_dep_cascade_on_item_delete(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        conn.execute("DELETE FROM work_item WHERE id = ?", (iid_a,))
        conn.commit()
        assert db.list_deps_blocking(conn, iid_b) == []

    def test_multiple_blockers(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        iid_c = _item(conn, active_sprint["id"], "C")
        db.add_dep(conn, iid_a, iid_c)
        db.add_dep(conn, iid_b, iid_c)
        blockers = db.list_deps_blocking(conn, iid_c)
        assert len(blockers) == 2
        blocker_ids = {b["item_id"] for b in blockers}
        assert blocker_ids == {iid_a, iid_b}

    def test_pending_to_active_raises_when_blockers_unresolved(self, conn, active_sprint):
        blocker = _item(conn, active_sprint["id"], "Blocker")
        blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, blocker, blocked)
        with pytest.raises(db.InvalidTransition, match="blockers remain unresolved"):
            db.set_work_item_status(conn, blocked, "active")

    def test_blocked_to_active_raises_when_blockers_unresolved(self, conn, active_sprint):
        blocker = _item(conn, active_sprint["id"], "Blocker")
        blocked = _item(conn, active_sprint["id"], "Blocked")
        db.set_work_item_status(conn, blocked, "active")
        db.set_work_item_status(conn, blocked, "blocked")
        db.add_dep(conn, blocker, blocked)
        with pytest.raises(db.InvalidTransition, match="blockers remain unresolved"):
            db.set_work_item_status(conn, blocked, "active")

    def test_pending_to_active_allowed_when_blockers_done(self, conn, active_sprint):
        blocker = _item(conn, active_sprint["id"], "Blocker")
        blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, blocker, blocked)
        db.set_work_item_status(conn, blocker, "active")
        db.set_work_item_status(conn, blocker, "done")
        db.set_work_item_status(conn, blocked, "active")
        assert db.get_work_item(conn, blocked)["status"] == "active"


# ---------------------------------------------------------------------------
# get_ready_items
# ---------------------------------------------------------------------------

class TestGetReadyItems:
    def test_no_deps_all_ready(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        ready = db.get_ready_items(conn, active_sprint["id"])
        ready_ids = {it["id"] for it in ready}
        assert iid_a in ready_ids
        assert iid_b in ready_ids

    def test_blocked_item_not_ready(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        ready = db.get_ready_items(conn, active_sprint["id"])
        ready_ids = {it["id"] for it in ready}
        assert iid_a in ready_ids   # blocker is ready
        assert iid_b not in ready_ids  # blocked until A is done

    def test_blocked_item_becomes_ready_when_blocker_done(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        conn.execute("UPDATE work_item SET status = 'done' WHERE id = ?", (iid_a,))
        conn.commit()
        ready = db.get_ready_items(conn, active_sprint["id"])
        ready_ids = {it["id"] for it in ready}
        assert iid_b in ready_ids

    def test_all_blocked_returns_empty(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        db.add_dep(conn, iid_b, iid_a)
        ready = db.get_ready_items(conn, active_sprint["id"])
        assert ready == []

    def test_active_items_excluded_from_ready(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A", status="active")
        ready = db.get_ready_items(conn, active_sprint["id"])
        ready_ids = {it["id"] for it in ready}
        assert iid_a not in ready_ids

    def test_done_items_excluded_from_ready(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A", status="done")
        ready = db.get_ready_items(conn, active_sprint["id"])
        ready_ids = {it["id"] for it in ready}
        assert iid_a not in ready_ids

    def test_ready_items_include_blockers_resolved_count(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        conn.execute("UPDATE work_item SET status = 'done' WHERE id = ?", (iid_a,))
        conn.commit()
        ready = db.get_ready_items(conn, active_sprint["id"])
        b_ready = next(it for it in ready if it["id"] == iid_b)
        assert b_ready["blockers_resolved"] == 1
        assert b_ready["unresolved_blockers"] == 0


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------

class TestDepCLI:
    def test_item_dep_add_basic(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        result = runner.invoke(cli, [
            "item", "dep", "add",
            "--id", str(iid_a),
            "--blocks-item-id", str(iid_b),
        ])
        assert result.exit_code == 0, result.output
        assert f"#{iid_a}" in result.output
        assert f"#{iid_b}" in result.output

    def test_item_dep_add_self_reference_fails(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "dep", "add",
            "--id", str(iid),
            "--blocks-item-id", str(iid),
        ])
        assert result.exit_code == 1

    def test_item_dep_list_text(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Blocker task")
        iid_b = _item(conn, active_sprint["id"], "Waiting task")
        db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, ["item", "dep", "list", "--id", str(iid_b)])
        assert result.exit_code == 0, result.output
        assert "Blocker task" in result.output
        assert "blocked by" in result.output.lower()

    def test_item_dep_list_json(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, ["item", "dep", "list", "--id", str(iid_b), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "blocked_by" in data
        assert len(data["blocked_by"]) == 1

    def test_item_dep_list_empty(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["item", "dep", "list", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "No dependencies" in result.output

    def test_item_dep_list_unknown_item(self, runner, db_path):
        result = runner.invoke(cli, ["item", "dep", "list", "--id", "9999"])
        assert result.exit_code == 1

    def test_item_dep_remove(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        dep_id = db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, [
            "item", "dep", "remove",
            "--id", str(iid_a),
            "--dep-id", str(dep_id),
        ])
        assert result.exit_code == 0, result.output
        assert db.list_deps_blocking(conn, iid_b) == []

    def test_item_dep_remove_not_found(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "dep", "remove",
            "--id", str(iid),
            "--dep-id", "9999",
        ])
        assert result.exit_code == 1

    def test_item_show_includes_blocked_by(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Prerequisite")
        iid_b = _item(conn, active_sprint["id"], "Dependent task")
        db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, ["item", "show", "--id", str(iid_b)])
        assert result.exit_code == 0, result.output
        assert "Blocked by:" in result.output
        assert "Prerequisite" in result.output

    def test_item_show_includes_blocks(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Prerequisite")
        iid_b = _item(conn, active_sprint["id"], "Dependent task")
        db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, ["item", "show", "--id", str(iid_a)])
        assert result.exit_code == 0, result.output
        assert "Blocks:" in result.output
        assert "Dependent task" in result.output

    def test_item_show_json_includes_deps(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, ["item", "show", "--id", str(iid_b), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "deps" in data
        assert len(data["deps"]["blocked_by"]) == 1

    def test_item_show_no_deps_section_when_empty(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "Blocked by:" not in result.output
        assert "Blocks:" not in result.output

    def test_item_status_active_fails_when_blockers_unresolved(self, runner, conn, active_sprint, db_path):
        blocker = _item(conn, active_sprint["id"], "Blocker")
        blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, blocker, blocked)
        result = runner.invoke(
            cli,
            ["item", "status", "--id", str(blocked), "--status", "active"],
        )
        assert result.exit_code == 1
        assert "blockers remain unresolved" in result.output


# ---------------------------------------------------------------------------
# next-work command
# ---------------------------------------------------------------------------

class TestNextWork:
    def test_next_work_lists_ready_items(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Ready task")
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Ready task" in result.output

    def test_next_work_excludes_blocked_items(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Prerequisite")
        iid_b = _item(conn, active_sprint["id"], "Blocked task")
        db.add_dep(conn, iid_a, iid_b)
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Prerequisite" in result.output
        assert "Blocked task" not in result.output

    def test_next_work_shows_unblocked_after_dep_resolved(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Prerequisite")
        iid_b = _item(conn, active_sprint["id"], "Previously blocked")
        db.add_dep(conn, iid_a, iid_b)
        conn.execute("UPDATE work_item SET status = 'done' WHERE id = ?", (iid_a,))
        conn.commit()
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Previously blocked" in result.output

    def test_next_work_empty_sprint(self, runner, db_path):
        from sprintctl.cli import cli as cli_
        from click.testing import CliRunner
        r = CliRunner()
        r.invoke(cli_, ["sprint", "create", "--name", "Empty sprint", "--status", "active"])
        result = r.invoke(cli_, ["next-work"])
        assert result.exit_code == 0, result.output
        assert "No pending items" in result.output

    def test_next_work_json_output(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Ready task")
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert any(it["id"] == iid_a for it in data)

    def test_next_work_json_output_remains_ready_items_shape(self, runner, conn, active_sprint, db_path):
        _item(conn, active_sprint["id"], "Ready task")
        expected = db.get_ready_items(conn, active_sprint["id"])
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == expected

    def test_next_work_json_explain_output(self, runner, conn, active_sprint, db_path):
        iid_ready = _item(conn, active_sprint["id"], "Ready task")
        result = runner.invoke(
            cli,
            ["next-work", "--sprint-id", str(active_sprint["id"]), "--json", "--explain"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["contract_version"] == "1"
        assert data["sprint"]["id"] == active_sprint["id"]
        assert data["summary"]["ready"] == 1
        assert data["summary"]["waiting_on_dependencies"] == 0
        assert data["next_action"]["kind"] == "start-ready-item"
        ready = data["ready_items"][0]
        assert ready["id"] == iid_ready
        assert ready["reason_code"] == "ready-unblocked"

    def test_next_work_json_explain_includes_waiting_dependency_details(
        self, runner, conn, active_sprint, db_path
    ):
        blocker = _item(conn, active_sprint["id"], "Blocker")
        blocked = _item(conn, active_sprint["id"], "Blocked task")
        db.add_dep(conn, blocker, blocked)
        conn.execute("UPDATE work_item SET status = 'active' WHERE id = ?", (blocker,))
        conn.commit()
        result = runner.invoke(
            cli,
            ["next-work", "--sprint-id", str(active_sprint["id"]), "--json", "--explain"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["summary"]["ready"] == 0
        assert data["summary"]["waiting_on_dependencies"] == 1
        assert data["next_action"]["kind"] == "unblock-dependent-work"
        waiting = data["dependency_waiting_items"][0]
        assert waiting["id"] == blocked
        assert waiting["reason_code"] == "waiting-on-dependencies"
        assert waiting["unresolved_blocker_ids"] == [blocker]

    def test_next_work_json_explain_prioritizes_active_claim(self, runner, conn, active_sprint, db_path):
        claimed = _item(conn, active_sprint["id"], "Claimed task")
        claim_id = db.create_claim(
            conn,
            claimed,
            "codex-agent",
            runtime_session_id="session-next-work",
            instance_id="instance-next-work",
        )
        result = runner.invoke(
            cli,
            ["next-work", "--sprint-id", str(active_sprint["id"]), "--json", "--explain"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["summary"]["active_claims"] == 1
        assert data["next_action"]["kind"] == "inspect-active-claim"
        assert data["next_action"]["claim_id"] == claim_id

    def test_next_work_explain_requires_json(self, runner, active_sprint):
        result = runner.invoke(cli, ["next-work", "--sprint-id", str(active_sprint["id"]), "--explain"])
        assert result.exit_code == 1
        assert "--explain requires --json" in result.output

    def test_next_work_no_active_sprint_fails(self, runner, db_path):
        result = runner.invoke(cli, ["next-work"])
        assert result.exit_code == 1

    def test_next_work_defaults_to_active_sprint(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"], "Default sprint task")
        result = runner.invoke(cli, ["next-work"])
        assert result.exit_code == 0, result.output
        assert "Default sprint task" in result.output
