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
        assert data["local_recovery"]["recovery_token_exists"] is True
        assert data["local_recovery"]["recovery_token_path"].endswith(f"claim-{data['claim_id']}.json")

    def test_claim_start_cmd_json_creates_claim_and_activates_item(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(
            cli,
            [
                "claim", "start",
                "--item-id", str(iid),
                "--agent", "bot-1",
                "--ttl", "900",
                "--runtime-session-id", "thread-1",
                "--instance-id", "proc-1",
                "--branch", "feat/auth",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["operation"] == "claim_start"
        assert data["status_transition_applied"] is True
        assert data["item_status_before"] == "pending"
        assert data["item_status_after"] == "active"
        assert data["claim_id"] == data["claim"]["claim_id"]
        assert data["claim_token"] == data["claim"]["claim_token"]
        assert data["claim"]["claim_type"] == "execute"
        assert data["claim"]["claim_token"]
        assert data["claim"]["runtime_session_id"] == "thread-1"
        assert data["claim"]["instance_id"] == "proc-1"
        assert data["local_recovery"]["recovery_token_exists"] is True
        assert data["local_recovery"]["recovery_token_path"].endswith(f"claim-{data['claim_id']}.json")
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_claim_start_cmd_active_item_skips_status_transition(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active", actor="seed")

        result = runner.invoke(
            cli,
            [
                "claim", "start",
                "--item-id", str(iid),
                "--agent", "bot-1",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["item_status_before"] == "active"
        assert data["item_status_after"] == "active"
        assert data["status_transition_applied"] is False

    def test_claim_start_cmd_releases_claim_if_status_transition_fails(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active", actor="seed")
        db.set_work_item_status(conn, iid, "done", actor="seed")

        result = runner.invoke(
            cli,
            [
                "claim", "start",
                "--item-id", str(iid),
                "--agent", "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "could not be moved to active" in result.output
        assert "Claim #" in result.output and "was released" in result.output
        assert db.list_claims(conn, iid, active_only=False) == []

    def test_claim_start_cmd_releases_claim_if_unexpected_transition_error(self, runner, conn, active_sprint, db_path, monkeypatch):
        iid = _item(conn, active_sprint["id"])

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic transition failure")

        monkeypatch.setattr(db, "set_work_item_status", _boom)

        result = runner.invoke(
            cli,
            [
                "claim", "start",
                "--item-id", str(iid),
                "--agent", "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "synthetic transition failure" in result.output
        assert "Claim #" in result.output and "was released" in result.output
        assert db.list_claims(conn, iid, active_only=False) == []

    def test_item_done_from_claim_cmd_json_marks_done_and_releases_claim(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["operation"] == "item_done_from_claim"
        assert data["item_status_before"] == "active"
        assert data["item_status_after"] == "done"
        assert data["claim_released"] is True
        assert data["claim_still_present"] is False
        assert db.get_work_item(conn, iid)["status"] == "done"
        assert db.get_claim(conn, claim["claim_id"]) is None

    def test_item_done_from_claim_cmd_infers_item_id_from_claim(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["operation"] == "item_done_from_claim"
        assert data["item_id"] == iid
        assert data["item_status_after"] == "done"
        assert data["claim_released"] is True
        assert db.get_work_item(conn, iid)["status"] == "done"

    def test_item_done_from_claim_cmd_keep_claim_retains_claim(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
                "--keep-claim",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["claim_released"] is False
        assert data["claim_still_present"] is True
        assert data["keep_claim"] is True
        assert db.get_work_item(conn, iid)["status"] == "done"
        assert db.get_claim(conn, claim["claim_id"]) is not None

    def test_item_done_from_claim_cmd_wrong_token_fails_and_status_stays_active(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                "wrong-token",
                "--actor",
                "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "Invalid claim_token" in result.output
        assert db.get_work_item(conn, iid)["status"] == "active"
        assert db.get_claim(conn, claim["claim_id"]) is not None

    def test_item_done_from_claim_cmd_rejects_claim_item_mismatch(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], title="Task A")
        iid_b = _item(conn, active_sprint["id"], title="Task B")
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid_a), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)
        db.set_work_item_status(conn, iid_b, "active", actor="seed")

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid_b),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert f"belongs to item #{iid_a}" in result.output
        assert db.get_work_item(conn, iid_a)["status"] == "active"
        assert db.get_work_item(conn, iid_b)["status"] == "active"

    def test_item_done_from_claim_cmd_rejects_expired_claim(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)
        conn.execute(
            "UPDATE claim SET expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-5 minutes') WHERE id = ?",
            (claim["claim_id"],),
        )
        conn.commit()

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "is expired" in result.output
        assert db.get_work_item(conn, iid)["status"] == "active"
        assert db.get_claim(conn, claim["claim_id"]) is not None

    def test_item_done_from_claim_cmd_release_failure_returns_json_and_nonzero(
        self, runner, conn, active_sprint, db_path, monkeypatch
    ):
        iid = _item(conn, active_sprint["id"])
        started = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        claim = json.loads(started.output)

        def _release_fail(*args, **kwargs):
            raise ValueError("synthetic release failure")

        monkeypatch.setattr(db, "release_claim", _release_fail)

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
                "--json",
            ],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["operation"] == "item_done_from_claim"
        assert data["item_status_after"] == "done"
        assert data["claim_released"] is False
        assert data["claim_still_present"] is True
        assert "synthetic release failure" in data["release_error"]
        assert db.get_work_item(conn, iid)["status"] == "done"
        assert db.get_claim(conn, claim["claim_id"]) is not None

    def test_item_done_from_claim_cmd_rejects_non_execute_claim(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active", actor="seed")
        claim = _claim(conn, iid, agent="bot-1", claim_type="review")

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "requires an active exclusive execute claim" in result.output
        assert db.get_work_item(conn, iid)["status"] == "active"
        assert db.get_claim(conn, claim["claim_id"]) is not None

    def test_item_done_from_claim_cmd_rejects_non_exclusive_claim(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "active", actor="seed")
        claim = _claim(conn, iid, agent="bot-1", claim_type="execute", exclusive=False)

        result = runner.invoke(
            cli,
            [
                "item",
                "done-from-claim",
                "--id",
                str(iid),
                "--claim-id",
                str(claim["claim_id"]),
                "--claim-token",
                claim["claim_token"],
                "--actor",
                "bot-1",
            ],
        )
        assert result.exit_code == 1
        assert "requires an active exclusive execute claim" in result.output
        assert db.get_work_item(conn, iid)["status"] == "active"
        assert db.get_claim(conn, claim["claim_id"]) is not None

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
        assert bundle["bundle_type"] == "handoff"
        assert bundle["bundle_version"] == "1"
        assert bundle["claim_identity_model"]["ownership_proof"] == "claim_id+claim_token"
        assert bundle["claim_identity_model"]["claim_tokens_included"] is False
        assert "summary" in bundle
        assert "work" in bundle
        assert "recent_decisions" in bundle
        assert "next_action" in bundle
        assert "freshness" in bundle
        assert "evidence" in bundle
        active_claim = bundle["active_claims"][0]
        assert active_claim["claim_id"] == claim["claim_id"]
        assert active_claim["claim_token_present"] is True
        assert active_claim["identity_status"] == "proven"
        assert "claim_token" not in active_claim


class TestClaimShow:
    def test_claim_show_returns_token_with_valid_proof(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1", "--json"]
        )
        claim = json.loads(created.output)

        result = runner.invoke(
            cli,
            ["claim", "show", "--id", str(claim["claim_id"]), "--claim-token", claim["claim_token"], "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["claim_token"] == claim["claim_token"]
        assert data["claim_id"] == claim["claim_id"]
        assert data["identity_status"] == "proven"

    def test_claim_show_fails_with_wrong_token(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1", "--json"]
        )
        claim = json.loads(created.output)

        result = runner.invoke(
            cli,
            ["claim", "show", "--id", str(claim["claim_id"]), "--claim-token", "wrong-token"],
        )
        assert result.exit_code == 1
        assert "Invalid claim_token" in result.output

    def test_claim_show_fails_for_missing_claim(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, ["claim", "show", "--id", "9999", "--claim-token", "x"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestClaimResume:
    def test_resume_finds_claim_by_instance_id(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="bot-1", instance_id="proc-resume-1")
        results = db.find_claim_by_identity(conn, instance_id="proc-resume-1")
        assert len(results) == 1
        assert results[0]["claim_id"] == claim["claim_id"]
        assert "claim_token" not in results[0]

    def test_resume_finds_claim_by_runtime_session_id(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="bot-1", runtime_session_id="thread-resume-1")
        results = db.find_claim_by_identity(conn, runtime_session_id="thread-resume-1")
        assert len(results) == 1
        assert results[0]["claim_id"] == claim["claim_id"]

    def test_resume_finds_claim_by_hostname_and_pid(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="bot-1", hostname="host-x", pid=7777)
        results = db.find_claim_by_identity(conn, hostname="host-x", pid=7777)
        assert len(results) == 1
        assert results[0]["claim_id"] == claim["claim_id"]

    def test_resume_requires_at_least_one_identity_field(self, conn):
        with pytest.raises(ValueError, match="At least one"):
            db.find_claim_by_identity(conn)

    def test_resume_does_not_return_expired_claims(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="bot-1", instance_id="proc-exp", ttl_seconds=1)
        conn.execute(
            "UPDATE claim SET expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-10 seconds') WHERE id = ?",
            (cid,),
        )
        conn.commit()
        results = db.find_claim_by_identity(conn, instance_id="proc-exp", active_only=True)
        assert len(results) == 0

    def test_resume_cmd_json_output(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(
            cli,
            ["claim", "create", "--item-id", str(iid), "--agent", "bot-1",
             "--instance-id", "proc-resume-cli"],
        )
        result = runner.invoke(cli, ["claim", "resume", "--instance-id", "proc-resume-cli", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["identity"]["instance_id"] == "proc-resume-cli"
        assert "claim_token" not in data[0]
        assert data[0]["local_recovery"]["recovery_token_exists"] is True
        assert data[0]["local_recovery"]["recovery_token_path"].endswith(f"claim-{data[0]['claim_id']}.json")

    def test_resume_cmd_can_filter_by_item_id(self, runner, conn, active_sprint, db_path):
        iid_a = _item(conn, active_sprint["id"], "Task A")
        iid_b = _item(conn, active_sprint["id"], "Task B")
        for item_id in (iid_a, iid_b):
            result = runner.invoke(
                cli,
                [
                    "claim",
                    "create",
                    "--item-id",
                    str(item_id),
                    "--agent",
                    "bot-1",
                    "--instance-id",
                    "proc-resume-filter",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output

        result = runner.invoke(
            cli,
            [
                "claim",
                "resume",
                "--instance-id",
                "proc-resume-filter",
                "--item-id",
                str(iid_b),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["work_item_id"] == iid_b

    def test_resume_cmd_no_results(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, ["claim", "resume", "--instance-id", "nobody"])
        assert result.exit_code == 0
        assert "No active claims" in result.output

    def test_claim_recover_cmd_json_returns_locally_persisted_token(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli,
            [
                "claim",
                "create",
                "--item-id",
                str(iid),
                "--agent",
                "bot-1",
                "--runtime-session-id",
                "thread-recover",
                "--instance-id",
                "proc-recover",
                "--json",
            ],
        )
        assert created.exit_code == 0, created.output
        claim = json.loads(created.output)

        result = runner.invoke(cli, ["claim", "recover", "--id", str(claim["claim_id"]), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["claim"]["claim_id"] == claim["claim_id"]
        assert data["claim_token"] == claim["claim_token"]
        assert data["local_recovery"]["recovery_token_exists"] is True
        assert data["local_recovery"]["recovery_token_path"] == claim["local_recovery"]["recovery_token_path"]

    def test_claim_recover_cmd_by_item_id_and_release_cleanup(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        created = runner.invoke(
            cli,
            ["claim", "start", "--item-id", str(iid), "--agent", "bot-1", "--json"],
        )
        assert created.exit_code == 0, created.output
        claim = json.loads(created.output)
        recovery_path = db_path.parent / "claim-recovery" / f"claim-{claim['claim_id']}.json"
        assert recovery_path.exists()

        recovered = runner.invoke(cli, ["claim", "recover", "--item-id", str(iid), "--json"])
        assert recovered.exit_code == 0, recovered.output
        recovered_data = json.loads(recovered.output)
        assert recovered_data["claim_token"] == claim["claim_token"]

        released = runner.invoke(
            cli,
            ["claim", "release", "--id", str(claim["claim_id"]), "--claim-token", claim["claim_token"]],
        )
        assert released.exit_code == 0, released.output
        assert not recovery_path.exists()


class TestCoordinateHierarchy:
    def test_subagent_can_claim_execute_under_coordinate(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        coord = _claim(conn, iid, agent="orchestrator", claim_type="coordinate")
        # Sub-agent creates execute claim under the coordinate claim
        sub_cid = db.create_claim(
            conn,
            iid,
            agent="worker-1",
            claim_type="execute",
            coordinate_claim_id=coord["claim_id"],
            coordinate_claim_token=coord["claim_token"],
        )
        sub = db.get_claim(conn, sub_cid)
        assert sub is not None
        assert sub["actor"] == "worker-1"

    def test_subagent_claim_fails_with_wrong_coordinate_token(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _claim(conn, iid, agent="orchestrator", claim_type="coordinate")
        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.create_claim(
                conn,
                iid,
                agent="worker-1",
                claim_type="execute",
                coordinate_claim_id=1,
                coordinate_claim_token="wrong-token",
            )

    def test_execute_claim_still_conflicts_without_coordinate_proof(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _claim(conn, iid, agent="orchestrator", claim_type="coordinate")
        with pytest.raises(db.ClaimConflict, match="exclusively claimed"):
            db.create_claim(conn, iid, agent="worker-1", claim_type="execute")

    def test_execute_claim_conflicts_with_execute_not_coordinate(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        exec_claim = _claim(conn, iid, agent="agent-a", claim_type="execute")
        with pytest.raises(db.ClaimConflict, match="exclusively claimed"):
            db.create_claim(
                conn,
                iid,
                agent="agent-b",
                claim_type="execute",
                coordinate_claim_id=exec_claim["claim_id"],
                coordinate_claim_token=exec_claim["claim_token"],
            )

    def test_subagent_cli_coordinate_claim_id_flags(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        coord_result = runner.invoke(
            cli,
            ["claim", "create", "--item-id", str(iid), "--agent", "orchestrator", "--type", "coordinate", "--json"],
        )
        assert coord_result.exit_code == 0, coord_result.output
        coord = json.loads(coord_result.output)

        sub_result = runner.invoke(
            cli,
            [
                "claim", "create",
                "--item-id", str(iid),
                "--agent", "worker-1",
                "--type", "execute",
                "--coordinate-claim-id", str(coord["claim_id"]),
                "--coordinate-claim-token", coord["claim_token"],
                "--json",
            ],
        )
        assert sub_result.exit_code == 0, sub_result.output
        sub = json.loads(sub_result.output)
        assert sub["actor"] == "worker-1"
        assert sub["claim_type"] == "execute"


class TestAgentProtocol:
    def test_agent_protocol_cmd_text(self, runner, db_path):
        result = runner.invoke(cli, ["agent-protocol"])
        assert result.exit_code == 0, result.output
        assert "claim create" in result.output
        assert "heartbeat" in result.output
        assert "handoff" in result.output
        assert "release" in result.output

    def test_agent_protocol_cmd_json(self, runner, db_path):
        result = runner.invoke(cli, ["agent-protocol", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["sprintctl_agent_protocol_version"] == "1"
        assert "lifecycle" in data
        assert "session_resumption" in data
        assert "shutdown_checklist" in data
        assert "environment_hints" in data
        assert "coordinate" in data["claim_model"]["claim_types"]
        assert "~/.sprintctl/sprintctl.db" in data["environment_hints"]["SPRINTCTL_DB"]
        startup_cmd = data["lifecycle"]["1_startup"]["command"]
        assert startup_cmd.startswith("sprintctl claim start")
        assert "Preferred for execute flow" not in startup_cmd


class TestHandoffBundleShutdownProtocol:
    def test_handoff_bundle_includes_shutdown_protocol(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(
            cli, ["handoff", "--sprint-id", str(active_sprint["id"]), "--output", "-"]
        )
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)
        assert "agent_shutdown_protocol" in bundle
        proto = bundle["agent_shutdown_protocol"]
        assert "required_before_termination" in proto
        assert "resumption_hint" in proto
        assert len(proto["required_before_termination"]) >= 2
        assert "resume_instructions" in bundle

    def test_second_handoff_bundle_tracks_previous_handoff(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"], "Task")
        first = runner.invoke(cli, ["handoff", "--sprint-id", str(active_sprint["id"]), "--output", "-"])
        assert first.exit_code == 0, first.output

        db.create_event(
            conn,
            active_sprint["id"],
            "agent",
            event_type="decision",
            work_item_id=iid,
            payload={"summary": "Pinned handoff to working-memory contract"},
        )

        second = runner.invoke(cli, ["handoff", "--sprint-id", str(active_sprint["id"]), "--output", "-"])
        assert second.exit_code == 0, second.output
        bundle = json.loads(second.output)
        delta = bundle["delta_since_last_handoff"]
        assert delta["previous_handoff_at"] is not None
        assert delta["event_count"] >= 1
