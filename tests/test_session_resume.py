import json

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task", track="eng"):
    tid = db.get_or_create_track(conn, sprint_id, track)
    return db.create_work_item(conn, sprint_id, tid, title)


class TestSessionResumeCommand:
    def test_resume_fails_without_active_sprint(self, runner, db_path):
        result = runner.invoke(cli, ["session", "resume"])
        assert result.exit_code != 0
        assert "No active sprint found" in result.output

    def test_resume_text_includes_expected_sections(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Ready task")
        result = runner.invoke(cli, ["session", "resume", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Session resume for sprint" in result.output
        assert "Recommended sequence:" in result.output
        assert "Next action:" in result.output
        assert "Reservation status:" in result.output
        assert "usage --context snapshot:" in result.output
        assert "next-work --explain snapshot:" in result.output

    def test_resume_json_has_frozen_top_level_shape(self, runner, active_sprint):
        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert list(data.keys()) == [
            "contract_version",
            "generated_at",
            "sprint",
            "context",
            "next_work",
            "git_context",
            "reservation_status",
            "next_action",
            "recommended_sequence",
            "recommended_sequence_bundle",
        ]
        assert data["contract_version"] == "2"
        assert data["generated_at"].endswith("Z")

    def test_resume_json_embeds_existing_contracts(self, runner, conn, active_sprint):
        ready_id = _item(conn, active_sprint["id"], "Ready task")
        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["context"]["contract_version"] == "1"
        assert data["next_work"]["contract_version"] == "1"
        assert data["next_action"] == data["context"]["next_action"]
        assert data["next_action"] == data["next_work"]["next_action"]
        assert list(data["reservation_status"].keys()) == ["current_identity", "active_reservations"]
        assert data["next_work"]["ready_items"][0]["id"] == ready_id
        assert data["next_work"]["recommended_commands"] == [
            f"sprintctl reservation reserve --item-id {ready_id} --actor <name> --session-id <session-id> --json",
            f"sprintctl item show --id {ready_id}",
        ]
        next_work_bundle = data["next_work"]["recommended_command_bundle"]
        assert next_work_bundle["bundle_version"] == "1"
        assert next_work_bundle["next_action_kind"] == "start-ready-item"
        assert [step["kind"] for step in next_work_bundle["steps"]] == ["reservation-reserve", "item-show"]

    def test_resume_json_uses_single_next_action_even_when_context_conflicts_exist(
        self, runner, conn, active_sprint
    ):
        _item(conn, active_sprint["id"], "Ready task")
        blocked_id = _item(conn, active_sprint["id"], "Blocked task")
        db.set_work_item_status(conn, blocked_id, "active")
        db.set_work_item_status(conn, blocked_id, "blocked")
        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["next_action"]["kind"] == "triage-blocked-item"
        assert data["next_action"] == data["next_work"]["next_action"]
        assert data["next_work"]["recommended_commands"] == [f"sprintctl item show --id {blocked_id}"]
        next_work_bundle = data["next_work"]["recommended_command_bundle"]
        assert next_work_bundle["next_action_kind"] == "triage-blocked-item"
        assert [step["kind"] for step in next_work_bundle["steps"]] == ["item-show"]
        assert [step["command"] for step in next_work_bundle["steps"]] == data["next_work"][
            "recommended_commands"
        ]

    def test_resume_json_recommends_reclaiming_unclaimed_active_item(
        self, runner, conn, active_sprint
    ):
        iid = _item(conn, active_sprint["id"], "Interrupted task")
        db.set_work_item_status(conn, iid, "active")

        result = runner.invoke(cli, ["session", "resume", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["context"]["active_unclaimed_items"] == [
            {"id": iid, "title": "Interrupted task", "track": "eng"}
        ]
        assert data["next_work"]["summary"]["active_unclaimed"] == 1
        assert data["next_work"]["active_unclaimed_items"][0]["id"] == iid
        assert data["next_action"]["kind"] == "resume-unreserved-active-item"
        assert data["next_work"]["recommended_commands"] == [
            f"sprintctl reservation reserve --item-id {iid} --actor <name> --session-id <session-id> --json",
            f"sprintctl item show --id {iid}",
        ]

    def test_resume_json_respects_sprint_id(self, runner, conn):
        sid = db.create_sprint(conn, "Manual Sprint", "goal", "2026-04-02", "2026-04-16", "planned")
        result = runner.invoke(cli, ["session", "resume", "--sprint-id", str(sid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["sprint"]["id"] == sid
        assert data["context"]["sprint"]["id"] == sid
        assert data["next_work"]["sprint"]["id"] == sid

    def test_resume_json_recommended_sequence_includes_command_surface(self, runner, active_sprint):
        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        commands = data["recommended_sequence"]
        assert commands[0].startswith("sprintctl usage --context --sprint-id ")
        assert commands[1].startswith("sprintctl next-work --sprint-id ")
        assert commands[2] == "sprintctl reservation list --all --json"
        sequence_bundle = data["recommended_sequence_bundle"]
        assert sequence_bundle["bundle_version"] == "1"
        assert sequence_bundle["next_action_kind"] == data["next_action"]["kind"]
        assert [step["command"] for step in sequence_bundle["steps"]] == commands
        assert [step["kind"] for step in sequence_bundle["steps"]] == [
            "usage-context",
            "next-work",
            "reservation-list",
        ]
        assert sequence_bundle["steps"][0]["is_executable"] is True
        assert sequence_bundle["steps"][1]["is_executable"] is True
        assert sequence_bundle["steps"][2]["is_executable"] is True

    def test_resume_json_surfaces_session_bound_reservation_status(self, runner, conn, active_sprint, monkeypatch):
        iid = _item(conn, active_sprint["id"], "Reserved task")
        monkeypatch.setenv("SPRINTCTL_INSTANCE_ID", "proc-session-resume")
        monkeypatch.setenv("SPRINTCTL_RUNTIME_SESSION_ID", "thread-session-resume")

        reservation = db.reserve(conn, iid, actor="bot-1", session_id="thread-session-resume")

        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        status = data["reservation_status"]
        assert status["current_identity"] == {
            "runtime_session_id": "thread-session-resume",
            "instance_id": "proc-session-resume",
        }
        assert len(status["active_reservations"]) == 1
        entry = status["active_reservations"][0]
        assert entry["id"] == reservation["id"]
        assert entry["session_matches"] is True
        assert entry["refs"] == []
