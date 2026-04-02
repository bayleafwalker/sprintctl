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
            "next_action",
            "recommended_sequence",
        ]
        assert data["contract_version"] == "1"
        assert data["generated_at"].endswith("Z")

    def test_resume_json_embeds_existing_contracts(self, runner, conn, active_sprint):
        ready_id = _item(conn, active_sprint["id"], "Ready task")
        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["context"]["contract_version"] == "1"
        assert data["next_work"]["contract_version"] == "1"
        assert data["next_action"] == data["context"]["next_action"]
        assert data["next_work"]["ready_items"][0]["id"] == ready_id

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
        assert commands[2] == "sprintctl claim resume --json"
