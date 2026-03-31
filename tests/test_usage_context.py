import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _add_item(conn, sprint_id, title, track="eng"):
    tid = db.get_or_create_track(conn, sprint_id, track)
    return db.create_work_item(conn, sprint_id, tid, title)


class TestUsageContext:
    def test_context_exits_zero_with_active_sprint(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code == 0, result.output

    def test_context_shows_sprint_name_and_goal(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context"])
        assert active_sprint["name"] in result.output
        assert active_sprint["goal"] in result.output

    def test_context_fails_without_active_sprint(self, runner, db_path):
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code != 0

    def test_context_shows_item_summary(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"],"Item A")
        _add_item(conn, active_sprint["id"],"Item B")
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code == 0, result.output
        assert "2 total" in result.output or "pending" in result.output

    def test_context_shows_ready_items(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"],"Ready Item")
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code == 0, result.output
        assert "Ready to start" in result.output
        assert "Ready Item" in result.output

    def test_context_ready_truncated_to_five(self, runner, conn, active_sprint):
        for i in range(8):
            _add_item(conn, active_sprint["id"],f"Item {i}")
        result = runner.invoke(cli, ["usage", "--context"])
        assert result.exit_code == 0, result.output
        assert "more" in result.output

    def test_context_json_exits_zero(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "sprint" in data
        assert "summary" in data

    def test_context_json_sprint_fields(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert data["sprint"]["id"] == active_sprint["id"]
        assert data["sprint"]["name"] == active_sprint["name"]

    def test_context_json_summary_counts(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"],"Item A")
        _add_item(conn, active_sprint["id"],"Item B")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert data["summary"]["total"] == 2
        assert data["summary"]["pending"] == 2
        assert data["summary"]["done"] == 0

    def test_context_json_has_ready_items(self, runner, conn, active_sprint):
        _add_item(conn, active_sprint["id"],"Ready Item")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert len(data["ready_items"]) == 1
        assert data["ready_items"][0]["title"] == "Ready Item"

    def test_context_json_has_blocked_items(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"],"Blocked Item")
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "blocked")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert data["summary"]["blocked"] == 1
        assert any(it["id"] == iid for it in data["blocked_items"])

    def test_context_json_blocked_item_not_in_ready(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"],"Blocked Item")
        db.set_work_item_status(conn, iid, "active")
        db.set_work_item_status(conn, iid, "blocked")
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        ready_ids = [it["id"] for it in data["ready_items"]]
        assert iid not in ready_ids

    def test_context_json_includes_active_claims_key(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert "active_claims" in data
        assert isinstance(data["active_claims"], list)

    def test_context_json_includes_recent_decisions_key(self, runner, active_sprint):
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert "recent_decisions" in data
        assert isinstance(data["recent_decisions"], list)

    def test_context_json_recent_decisions_from_knowledge_events(self, runner, conn, active_sprint):
        iid = _add_item(conn, active_sprint["id"],"Item")
        db.create_event(
            conn,
            sprint_id=active_sprint["id"],
            work_item_id=iid,
            source_type="actor",
            actor="agent",
            event_type="lesson-learned",
            payload={"summary": "caching helps"},
        )
        result = runner.invoke(cli, ["usage", "--context", "--json"])
        data = json.loads(result.output)
        assert len(data["recent_decisions"]) == 1
        assert data["recent_decisions"][0]["event_type"] == "lesson-learned"

    def test_context_by_sprint_id(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "Other Sprint", "goal", "2026-04-01", "2026-04-30", "planned")
        result = runner.invoke(cli, ["usage", "--context", "--sprint-id", str(sid)])
        assert result.exit_code == 0, result.output
        assert "Other Sprint" in result.output

    def test_bare_usage_still_works_after_context_flag_added(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        assert result.exit_code == 0
        assert "SPRINT" in result.output
        assert "ITEM" in result.output
        assert "--context" in result.output
