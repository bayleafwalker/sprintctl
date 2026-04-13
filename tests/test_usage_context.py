import json

from sprintctl import db
from sprintctl.cli import cli


def _add_item(conn, sprint_id, title, track="eng"):
    tid = db.get_or_create_track(conn, sprint_id, track)
    return db.create_work_item(conn, sprint_id, tid, title)


class TestUsageContext:
    def test_context_exits_zero_with_active_sprint(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code == 0, result.output

    def test_context_fails_without_active_sprint(self, runner, db_path):
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code != 0

    def test_context_text_uses_contract_sections(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"], "Ready Item")
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code == 0, result.output
        assert "Active claims" in result.output
        assert "Conflicts" in result.output
        assert "Ready to start" in result.output
        assert "Blocked items" in result.output
        assert "Stale items" in result.output
        assert "Recent decisions" in result.output
        assert "Next action" in result.output

    def test_context_json_has_frozen_top_level_shape(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert list(data.keys()) == [
            "contract_version",
            "sprint",
            "summary",
            "active_claims",
            "active_unclaimed_items",
            "conflicts",
            "ready_items",
            "blocked_items",
            "stale_items",
            "recent_decisions",
            "next_action",
        ]
        assert data["contract_version"] == "1"

    def test_context_json_summary_counts(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"], "Item A")
        _add_item(conn, active_sprint["id"], "Item B")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert data["summary"]["total"] == 2
        assert data["summary"]["pending"] == 2
        assert data["summary"]["done"] == 0
        assert data["summary"]["ready"] == 2
        assert data["summary"]["waiting_on_dependencies"] == 0
        assert data["summary"]["active_unclaimed"] == 0

    def test_context_json_has_ready_items(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"], "Ready Item")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert len(data["ready_items"]) == 1
        assert data["ready_items"][0]["title"] == "Ready Item"
        assert data["next_action"]["kind"] == "start-ready-item"

    def test_context_json_has_blocked_items(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "Blocked Item")
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "blocked")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert data["summary"]["blocked"] == 1
        assert any(item["id"] == iid for item in data["blocked_items"])
        assert any(conflict["kind"] == "blocked-work" for conflict in data["conflicts"])

    def test_context_json_dependency_conflict_and_next_action(self, runner, conn, active_sprint):
        blocker = _add_item(conn, active_sprint["id"], "Blocker")
        blocked = _add_item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, blocker, blocked)
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        conflict_kinds = [conflict["kind"] for conflict in data["conflicts"]]
        assert "dependency-blocked" in conflict_kinds
        assert data["summary"]["ready"] == 1
        assert data["summary"]["waiting_on_dependencies"] == 1
        assert data["next_action"]["kind"] == "unblock-dependent-work"
        assert data["next_action"]["item_id"] == blocked
        assert data["next_action"]["blocker_item_id"] == blocker

    def test_context_json_includes_active_claims_key(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert "active_claims" in data
        assert isinstance(data["active_claims"], list)

    def test_context_json_flags_active_items_without_live_claims(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "Interrupted task")
        db.set_work_item_status(conn, iid, "active")

        result = runner.invoke(cli, ["usage", "--context", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["summary"]["active_unclaimed"] == 1
        assert data["active_unclaimed_items"] == [
            {"id": iid, "title": "Interrupted task", "track": "eng"}
        ]
        assert data["conflicts"][0]["kind"] == "unclaimed-active-work"
        assert data["next_action"]["kind"] == "resume-unclaimed-active-item"
        assert data["next_action"]["item_id"] == iid

    def test_context_json_includes_recent_decisions_and_summary(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"], "Item")
        db.create_event(
            conn,
            sprint_id=active_sprint["id"],
            work_item_id=iid,
            source_type="actor",
            actor="agent",
            event_type="decision",
            payload={"summary": "Use working-memory handoff contract"},
        )
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert len(data["recent_decisions"]) == 1
        assert data["recent_decisions"][0]["event_type"] == "decision"
        assert data["recent_decisions"][0]["summary"] == "Use working-memory handoff contract"

    def test_context_json_by_sprint_id(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "Other Sprint", "goal", "2026-04-01", "2026-04-30", "planned")
        result = runner.invoke(cli, ["usage", "--context", "--sprint-id", str(sid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["sprint"]["id"] == sid
        assert data["sprint"]["name"] == "Other Sprint"

    def test_bare_usage_still_works_after_context_flag_added(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        assert result.exit_code == 0
        assert "SPRINT" in result.output
        assert "ITEM" in result.output
        assert "--context" in result.output
