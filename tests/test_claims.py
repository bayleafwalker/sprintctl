"""
Tests for token-backed claim identity, ownership proof, and explicit handoff flows.
"""

import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


def _claim(conn, item_id, agent="agent-a", **kwargs) -> dict:
    cid = db.create_claim(conn, item_id, agent=agent, **kwargs)
    claim = db.get_claim(conn, cid, include_secret=True)
    assert claim is not None
    return claim


class TestClaimCreate:
    def test_create_returns_claim_id_and_token(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(
            conn,
            iid,
            agent="agent-a",
            runtime_session_id="thread-1",
            instance_id="proc-1",
        )
        assert claim["claim_id"] > 0
        assert claim["claim_token"]
        assert claim["actor"] == "agent-a"
        assert claim["runtime_session_id"] == "thread-1"
        assert claim["instance_id"] == "proc-1"
        assert claim["identity_status"] == "proven"
        assert claim["ownership_proof"]["type"] == "claim_id+claim_token"

    def test_same_actor_same_workspace_different_runtime_conflicts(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _claim(
            conn,
            iid,
            agent="agent-a",
            runtime_session_id="thread-1",
            instance_id="proc-1",
            branch="feat/auth",
            worktree_path="/tmp/worktrees/auth",
            commit_sha="abc1234",
        )
        with pytest.raises(db.ClaimConflict, match="exclusively claimed"):
            db.create_claim(
                conn,
                iid,
                agent="agent-a",
                runtime_session_id="thread-2",
                instance_id="proc-2",
                branch="feat/auth",
                worktree_path="/tmp/worktrees/auth",
                commit_sha="abc1234",
            )

    def test_non_exclusive_does_not_conflict(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _claim(conn, iid, agent="agent-a", exclusive=False)
        claim2 = _claim(conn, iid, agent="agent-b", exclusive=False)
        assert claim2["actor"] == "agent-b"

    def test_invalid_claim_type_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid claim_type"):
            db.create_claim(conn, iid, agent="agent-a", claim_type="bogus")

    def test_missing_item_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            db.create_claim(conn, 9999, agent="agent-a")


class TestClaimOwnership:
    def test_same_runtime_session_can_resume_in_new_process(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(
            conn,
            iid,
            agent="agent-a",
            runtime_session_id="thread-1",
            instance_id="proc-1",
            hostname="host-a",
            pid=100,
            ttl_seconds=60,
        )
        before = conn.execute("SELECT expires_at FROM claim WHERE id = ?", (claim["claim_id"],)).fetchone()[0]

        db.heartbeat_claim(
            conn,
            claim["claim_id"],
            claim["claim_token"],
            ttl_seconds=600,
            actor="agent-a",
            runtime_session_id="thread-1",
            instance_id="proc-2",
            hostname="host-a",
            pid=200,
        )

        after = db.get_claim(conn, claim["claim_id"], include_secret=True)
        assert after is not None
        assert after["runtime_session_id"] == "thread-1"
        assert after["instance_id"] == "proc-2"
        assert after["pid"] == 200
        assert after["expires_at"] >= before

    def test_heartbeat_wrong_token_raises_and_emits_coordination_failure(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")

        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.heartbeat_claim(conn, claim["claim_id"], "wrong-token", actor="agent-b")

        events = db.list_events(conn, active_sprint["id"])
        assert events[-1]["event_type"] == "coordination-failure"
        payload = json.loads(events[-1]["payload"])
        assert payload["operation"] == "heartbeat"
        assert payload["reason"] == "invalid-claim-proof"

    def test_release_wrong_token_raises_and_emits_coordination_failure(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")

        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.release_claim(conn, claim["claim_id"], "wrong-token", actor="agent-b")

        events = db.list_events(conn, active_sprint["id"])
        assert events[-1]["event_type"] == "coordination-failure"
        payload = json.loads(events[-1]["payload"])
        assert payload["operation"] == "release"
        assert payload["reason"] == "invalid-claim-proof"

    def test_release_removes_claim_with_valid_token(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        db.release_claim(conn, claim["claim_id"], claim["claim_token"], actor="agent-a")
        assert db.get_claim(conn, claim["claim_id"]) is None

    def test_explicit_handoff_success_rotates_token(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(
            conn,
            iid,
            agent="agent-a",
            runtime_session_id="thread-1",
            instance_id="proc-1",
        )

        handed = db.handoff_claim(
            conn,
            claim["claim_id"],
            claim["claim_token"],
            actor="agent-b",
            mode="rotate",
            runtime_session_id="thread-2",
            instance_id="proc-2",
            performed_by="agent-a",
            note="Handing execution to the next live session.",
        )

        assert handed["actor"] == "agent-b"
        assert handed["runtime_session_id"] == "thread-2"
        assert handed["instance_id"] == "proc-2"
        assert handed["claim_token"] != claim["claim_token"]

        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.heartbeat_claim(conn, claim["claim_id"], claim["claim_token"], actor="agent-a")

        db.heartbeat_claim(
            conn,
            claim["claim_id"],
            handed["claim_token"],
            actor="agent-b",
            runtime_session_id="thread-2",
            instance_id="proc-2",
        )

        events = db.list_events(conn, active_sprint["id"])
        handoff_events = [e for e in events if e["event_type"] == "claim-handoff"]
        assert handoff_events
        payload = json.loads(handoff_events[-1]["payload"])
        assert payload["mode"] == "rotate"
        assert payload["from_identity"]["actor"] == "agent-a"
        assert payload["to_identity"]["actor"] == "agent-b"

    def test_legacy_ambiguous_claim_detection(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        conn.execute("UPDATE claim SET claim_token = NULL WHERE id = ?", (claim["claim_id"],))
        conn.commit()

        listed = db.list_claims(conn, iid)
        assert listed[0]["identity_status"] == "legacy_ambiguous"
        assert listed[0]["claim_token_present"] is False

        with pytest.raises(ValueError, match="legacy ambiguous claim"):
            db.heartbeat_claim(conn, claim["claim_id"], None, actor="agent-a")

        events = db.list_events(conn, active_sprint["id"])
        assert events[-1]["event_type"] == "claim-ambiguity-detected"

    def test_legacy_ambiguous_claim_can_be_adopted_via_explicit_handoff(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        conn.execute("UPDATE claim SET claim_token = NULL WHERE id = ?", (claim["claim_id"],))
        conn.commit()

        adopted = db.handoff_claim(
            conn,
            claim["claim_id"],
            None,
            actor="agent-b",
            mode="rotate",
            runtime_session_id="thread-2",
            instance_id="proc-2",
            performed_by="human",
            allow_legacy_adopt=True,
        )

        assert adopted["actor"] == "agent-b"
        assert adopted["claim_token"]
        assert adopted["identity_status"] == "proven"

        events = db.list_events(conn, active_sprint["id"])
        assert events[-1]["event_type"] == "claim-ownership-corrected"


class TestClaimEnforcement:
    def test_transition_blocked_without_claim_proof(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _claim(conn, iid, agent="agent-a")
        with pytest.raises(db.ClaimConflict, match="Provide --claim-id and --claim-token"):
            db.set_work_item_status(conn, iid, "active", actor="agent-b")

    def test_transition_allowed_with_claim_proof(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        db.set_work_item_status(
            conn,
            iid,
            "active",
            actor="agent-a",
            claim_id=claim["claim_id"],
            claim_token=claim["claim_token"],
        )
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_transition_allowed_after_release(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        db.release_claim(conn, claim["claim_id"], claim["claim_token"], actor="agent-a")
        db.set_work_item_status(conn, iid, "active", actor="agent-b")
        assert db.get_work_item(conn, iid)["status"] == "active"


class TestClaimJSONAndCLI:
    def test_claim_create_cmd_json_includes_token_and_identity(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(
            cli,
            [
                "claim", "create",
                "--item-id", str(iid),
                "--agent", "bot-1",
                "--runtime-session-id", "thread-1",
                "--instance-id", "proc-1",
                "--branch", "feat/auth",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["claim_id"] > 0
        assert data["claim_token"]
        assert data["actor"] == "bot-1"
        assert data["runtime_session_id"] == "thread-1"
        assert data["instance_id"] == "proc-1"
        assert data["branch"] == "feat/auth"
        assert data["identity"]["advisory"]["branch"] == "feat/auth"

    def test_claim_heartbeat_cmd_with_token(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli,
            ["claim", "create", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(created.output)
        result = runner.invoke(
            cli,
            [
                "claim", "heartbeat",
                "--id", str(claim["claim_id"]),
                "--claim-token", claim["claim_token"],
                "--agent", "bot-1",
                "--runtime-session-id", "thread-1",
                "--instance-id", "proc-2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "refreshed" in result.output

    def test_claim_release_cmd_with_wrong_token_fails(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli,
            ["claim", "create", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(created.output)
        result = runner.invoke(
            cli,
            [
                "claim", "release",
                "--id", str(claim["claim_id"]),
                "--claim-token", "wrong-token",
                "--agent", "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "Invalid claim_token" in result.output

    def test_item_status_requires_claim_proof_via_cli(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        result = runner.invoke(
            cli, ["item", "status", "--id", str(iid), "--status", "active", "--actor", "bot-1"]
        )
        assert result.exit_code == 1
        assert "Provide --claim-id and --claim-token" in result.output

    def test_item_status_allowed_for_owner_via_cli_with_claim_proof(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli,
            ["claim", "create", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(created.output)
        result = runner.invoke(
            cli,
            [
                "item", "status",
                "--id", str(iid),
                "--status", "active",
                "--actor", "bot-1",
                "--claim-id", str(claim["claim_id"]),
                "--claim-token", claim["claim_token"],
            ],
        )
        assert result.exit_code == 0, result.output

    def test_claim_handoff_cmd_json_bundle(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli,
            ["claim", "create", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(created.output)

        result = runner.invoke(
            cli,
            [
                "claim", "handoff",
                "--id", str(claim["claim_id"]),
                "--claim-token", claim["claim_token"],
                "--agent", "bot-2",
                "--mode", "rotate",
                "--performed-by", "bot-1",
                "--runtime-session-id", "thread-2",
                "--instance-id", "proc-2",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)
        assert bundle["bundle_type"] == "claim_handoff"
        assert bundle["claim"]["actor"] == "bot-2"
        assert bundle["claim"]["claim_token"]
        assert bundle["claim"]["claim_token"] != claim["claim_token"]

    def test_claim_list_json_shows_legacy_ambiguity(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="bot-1")
        conn.execute("UPDATE claim SET claim_token = NULL WHERE id = ?", (claim["claim_id"],))
        conn.commit()

        result = runner.invoke(cli, ["claim", "list", "--item-id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data[0]["claim_id"] == claim["claim_id"]
        assert data[0]["identity_status"] == "legacy_ambiguous"
        assert data[0]["claim_token_present"] is False
        assert "claim_token" not in data[0]

    def test_item_show_json_includes_new_identity_fields(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        _claim(
            conn,
            iid,
            agent="bot-1",
            runtime_session_id="thread-1",
            instance_id="proc-1",
            branch="feat/check",
            hostname="host-a",
            pid=123,
        )
        result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        claim = data["active_claims"][0]
        assert claim["claim_id"] > 0
        assert claim["actor"] == "bot-1"
        assert claim["runtime_session_id"] == "thread-1"
        assert claim["instance_id"] == "proc-1"
        assert claim["hostname"] == "host-a"
        assert claim["pid"] == 123
        assert claim["identity"]["advisory"]["branch"] == "feat/check"

    def test_handoff_bundle_surfaces_identity_without_secret(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(
            conn,
            iid,
            agent="bot-1",
            runtime_session_id="thread-1",
            instance_id="proc-1",
        )
        result = runner.invoke(
            cli,
            ["handoff", "--sprint-id", str(active_sprint["id"]), "--output", "-"],
        )
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)
        assert bundle["claim_identity_model"]["ownership_proof"] == "claim_id+claim_token"
        assert bundle["claim_identity_model"]["claim_tokens_included"] is False
        active_claim = bundle["active_claims"][0]
        assert active_claim["claim_id"] == claim["claim_id"]
        assert active_claim["claim_token_present"] is True
        assert active_claim["identity_status"] == "proven"
        assert "claim_token" not in active_claim
