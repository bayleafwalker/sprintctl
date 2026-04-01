import json

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


class TestDecisionPayloadContract:
    def test_decision_payload_is_canonicalized_in_db_layer(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_event(
            conn,
            active_sprint["id"],
            "agent",
            event_type="decision",
            work_item_id=iid,
            payload={"summary": "freeze contract"},
        )
        event = db.list_events(conn, active_sprint["id"])[0]
        payload = json.loads(event["payload"])
        assert list(payload.keys())[:3] == ["summary", "detail", "tags"]
        assert payload["summary"] == "freeze contract"
        assert payload["detail"] is None
        assert payload["tags"] == []

    def test_decision_payload_preserves_known_context_fields(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(
            cli,
            [
                "item", "note",
                "--id", str(iid),
                "--type", "decision",
                "--summary", "Use typed contracts",
                "--git-branch", "main",
                "--git-sha", "abc1234",
                "--git-worktree", "/tmp/wt",
                "--actor", "agent",
            ],
        )
        assert result.exit_code == 0, result.output
        event = db.list_events(conn, active_sprint["id"])[0]
        payload = json.loads(event["payload"])
        assert payload["summary"] == "Use typed contracts"
        assert payload["tags"] == []
        assert payload["git_branch"] == "main"
        assert payload["git_sha"] == "abc1234"
        assert payload["git_worktree"] == "/tmp/wt"


class TestClaimHandoffPayloadContract:
    def test_claim_handoff_payload_is_canonicalized(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        claim = db.get_claim(conn, cid, include_secret=True)
        assert claim is not None

        db.handoff_claim(
            conn,
            claim["claim_id"],
            claim["claim_token"],
            actor="agent-b",
            mode="rotate",
            performed_by="agent-a",
            note="handoff note",
        )
        events = db.list_events(conn, active_sprint["id"])
        payload = json.loads([e for e in events if e["event_type"] == "claim-handoff"][-1]["payload"])
        assert list(payload.keys())[:9] == [
            "summary",
            "detail",
            "tags",
            "operation",
            "mode",
            "legacy_adopted",
            "token_rotated",
            "from_identity",
            "to_identity",
        ]
        assert payload["operation"] == "handoff"
        assert payload["mode"] == "rotate"
        assert payload["legacy_adopted"] is False
        assert payload["from_identity"]["actor"] == "agent-a"
        assert payload["to_identity"]["actor"] == "agent-b"
