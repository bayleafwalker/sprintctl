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
        assert "Claim recovery:" in result.output
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
            "claim_recovery",
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
        assert list(data["claim_recovery"].keys()) == ["current_identity", "active_claims"]
        assert data["next_work"]["ready_items"][0]["id"] == ready_id
        assert data["next_work"]["recommended_commands"] == [
            f"sprintctl claim start --item-id {ready_id} --actor <name> --ttl 600 --json",
            f"sprintctl item show --id {ready_id}",
        ]
        next_work_bundle = data["next_work"]["recommended_command_bundle"]
        assert next_work_bundle["bundle_version"] == "1"
        assert next_work_bundle["next_action_kind"] == "start-ready-item"
        assert [step["kind"] for step in next_work_bundle["steps"]] == ["claim-start", "item-show"]

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
        assert data["next_action"]["kind"] == "resume-unclaimed-active-item"
        assert data["next_work"]["recommended_commands"] == [
            f"sprintctl claim start --item-id {iid} --actor <name> --ttl 600 --json",
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
        assert commands[2] == "sprintctl claim resume --json"
        sequence_bundle = data["recommended_sequence_bundle"]
        assert sequence_bundle["bundle_version"] == "1"
        assert sequence_bundle["next_action_kind"] == data["next_action"]["kind"]
        assert [step["command"] for step in sequence_bundle["steps"]] == commands
        assert [step["kind"] for step in sequence_bundle["steps"]] == [
            "usage-context",
            "next-work",
            "claim-resume",
        ]
        assert all(step["is_executable"] for step in sequence_bundle["steps"])

    def test_resume_json_claim_recovery_surfaces_local_token_status(self, runner, conn, active_sprint, monkeypatch):
        iid = _item(conn, active_sprint["id"], "Claimed task")
        monkeypatch.setenv("SPRINTCTL_INSTANCE_ID", "proc-session-resume")
        monkeypatch.setenv("SPRINTCTL_RUNTIME_SESSION_ID", "thread-session-resume")

        created = runner.invoke(
            cli,
            [
                "claim",
                "start",
                "--item-id",
                str(iid),
                "--agent",
                "bot-1",
                "--instance-id",
                "proc-session-resume",
                "--runtime-session-id",
                "thread-session-resume",
                "--json",
            ],
        )
        assert created.exit_code == 0, created.output
        claim = json.loads(created.output)

        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        recovery = data["claim_recovery"]
        assert recovery["current_identity"] == {
            "runtime_session_id": "thread-session-resume",
            "instance_id": "proc-session-resume",
        }
        assert len(recovery["active_claims"]) == 1
        claim_recovery = recovery["active_claims"][0]
        assert claim_recovery["claim_id"] == claim["claim_id"]
        assert claim_recovery["recovery_token_exists"] is True
        assert claim_recovery["runtime_session_id_matches"] is True
        assert claim_recovery["instance_id_matches"] is True
        assert claim_recovery["plausible_identity_match"] is True
        assert claim_recovery["recovery_token_path"].endswith(f"claim-{claim['claim_id']}.json")
