"""
Tests for Phase 2.5: claim create / heartbeat / release / list
and enforcement of exclusive claims in item status transitions.
"""

import pytest

from sprintctl import db
from sprintctl.cli import cli


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


# ---------------------------------------------------------------------------
# Group 1: claim DB layer
# ---------------------------------------------------------------------------

class TestClaimCreate:
    def test_create_returns_id(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        assert isinstance(cid, int) and cid > 0

    def test_exclusive_conflict_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        with pytest.raises(db.ClaimConflict):
            db.create_claim(conn, iid, agent="agent-b", ttl_seconds=300)

    def test_same_agent_can_reclaim(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        # Same agent may create another claim (no conflict)
        cid2 = db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        assert cid2 > 0

    def test_non_exclusive_does_not_conflict(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", exclusive=False, ttl_seconds=300)
        # Another agent can also claim non-exclusively without conflict check
        cid2 = db.create_claim(conn, iid, agent="agent-b", exclusive=False, ttl_seconds=300)
        assert cid2 > 0

    def test_invalid_claim_type_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid claim_type"):
            db.create_claim(conn, iid, agent="agent-a", claim_type="bogus")

    def test_missing_item_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            db.create_claim(conn, 9999, agent="agent-a")


class TestClaimHeartbeat:
    def test_heartbeat_updates_expiry(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a", ttl_seconds=60)
        before = conn.execute("SELECT expires_at FROM claim WHERE id = ?", (cid,)).fetchone()[0]
        db.heartbeat_claim(conn, cid, agent="agent-a", ttl_seconds=600)
        after = conn.execute("SELECT expires_at FROM claim WHERE id = ?", (cid,)).fetchone()[0]
        assert after >= before  # extended or same

    def test_heartbeat_wrong_agent_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        with pytest.raises(ValueError, match="owned by"):
            db.heartbeat_claim(conn, cid, agent="agent-b")

    def test_heartbeat_missing_claim_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            db.heartbeat_claim(conn, 9999, agent="agent-a")


class TestClaimRelease:
    def test_release_removes_claim(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        db.release_claim(conn, cid, agent="agent-a")
        row = conn.execute("SELECT id FROM claim WHERE id = ?", (cid,)).fetchone()
        assert row is None

    def test_release_wrong_agent_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        with pytest.raises(ValueError, match="owned by"):
            db.release_claim(conn, cid, agent="agent-b")

    def test_release_unblocks_conflict(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        with pytest.raises(db.ClaimConflict):
            db.create_claim(conn, iid, agent="agent-b")
        db.release_claim(conn, cid, agent="agent-a")
        # Now agent-b can claim
        cid2 = db.create_claim(conn, iid, agent="agent-b")
        assert cid2 > 0


class TestClaimList:
    def test_list_active_claims(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        claims = db.list_claims(conn, iid)
        assert any(c["id"] == cid for c in claims)

    def test_list_empty_when_no_claims(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        assert db.list_claims(conn, iid) == []


# ---------------------------------------------------------------------------
# Group 2: exclusive claim enforcement in set_work_item_status
# ---------------------------------------------------------------------------

class TestClaimEnforcement:
    def test_transition_blocked_by_exclusive_claim(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        with pytest.raises(db.ClaimConflict, match="exclusively claimed"):
            db.set_work_item_status(conn, iid, "active", actor="agent-b")

    def test_transition_allowed_for_claim_owner(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        db.set_work_item_status(conn, iid, "active", actor="agent-a")
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_transition_allowed_without_actor(self, conn, active_sprint):
        """actor=None skips claim enforcement (backwards compat)."""
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        db.set_work_item_status(conn, iid, "active", actor=None)
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_transition_allowed_after_release(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a", ttl_seconds=300)
        db.release_claim(conn, cid, agent="agent-a")
        db.set_work_item_status(conn, iid, "active", actor="agent-b")
        assert db.get_work_item(conn, iid)["status"] == "active"


# ---------------------------------------------------------------------------
# Group 3: CLI integration
# ---------------------------------------------------------------------------

class TestClaimCLI:
    def test_claim_create_cmd(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        assert result.exit_code == 0, result.output
        assert "Claim #" in result.output
        assert "bot-1" in result.output

    def test_claim_create_conflict_exits_1(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        result = runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-2"])
        assert result.exit_code == 1
        assert "exclusively claimed" in result.output

    def test_claim_heartbeat_cmd(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        r1 = runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        cid = int(r1.output.split("Claim #")[1].split(" ")[0])
        result = runner.invoke(cli, ["claim", "heartbeat", "--id", str(cid), "--agent", "bot-1"])
        assert result.exit_code == 0, result.output
        assert "refreshed" in result.output

    def test_claim_release_cmd(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        r1 = runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        cid = int(r1.output.split("Claim #")[1].split(" ")[0])
        result = runner.invoke(cli, ["claim", "release", "--id", str(cid), "--agent", "bot-1"])
        assert result.exit_code == 0, result.output
        assert "released" in result.output

    def test_claim_list_cmd(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        result = runner.invoke(cli, ["claim", "list", "--item-id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "bot-1" in result.output

    def test_item_status_blocked_by_claim_via_cli(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        result = runner.invoke(
            cli, ["item", "status", "--id", str(iid), "--status", "active", "--actor", "bot-2"]
        )
        assert result.exit_code == 1
        assert "exclusively claimed" in result.output

    def test_item_status_allowed_for_owner_via_cli(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, ["claim", "create", "--item-id", str(iid), "--agent", "bot-1"])
        result = runner.invoke(
            cli, ["item", "status", "--id", str(iid), "--status", "active", "--actor", "bot-1"]
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Group 4: workspace metadata on claims
# ---------------------------------------------------------------------------

class TestClaimWorkspaceMetadata:
    def test_create_with_workspace_metadata(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(
            conn, iid, agent="agent-a",
            branch="feat/auth",
            worktree_path="/tmp/worktrees/auth",
            commit_sha="abc1234",
            pr_ref="owner/repo#42",
        )
        row = conn.execute("SELECT * FROM claim WHERE id = ?", (cid,)).fetchone()
        assert row["branch"] == "feat/auth"
        assert row["worktree_path"] == "/tmp/worktrees/auth"
        assert row["commit_sha"] == "abc1234"
        assert row["pr_ref"] == "owner/repo#42"

    def test_workspace_metadata_in_list_claims(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", branch="feat/api", commit_sha="def5678")
        claims = db.list_claims(conn, iid)
        assert len(claims) == 1
        assert claims[0]["branch"] == "feat/api"
        assert claims[0]["commit_sha"] == "def5678"

    def test_workspace_metadata_defaults_to_null(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a")
        claims = db.list_claims(conn, iid)
        assert claims[0]["branch"] is None
        assert claims[0]["commit_sha"] is None
        assert claims[0]["pr_ref"] is None
        assert claims[0]["worktree_path"] is None

    def test_claim_create_cli_with_workspace_flags(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "claim", "create", "--item-id", str(iid), "--agent", "bot-1",
            "--branch", "feat/auth", "--commit-sha", "abc1234", "--pr-ref", "owner/repo#99",
        ])
        assert result.exit_code == 0, result.output
        claims = db.list_claims(conn, iid)
        assert claims[0]["branch"] == "feat/auth"
        assert claims[0]["commit_sha"] == "abc1234"
        assert claims[0]["pr_ref"] == "owner/repo#99"

    def test_item_show_displays_workspace_metadata(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, [
            "claim", "create", "--item-id", str(iid), "--agent", "bot-1",
            "--branch", "feat/login", "--pr-ref", "owner/repo#7",
        ])
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "feat/login" in result.output
        assert "owner/repo#7" in result.output

    def test_item_show_json_includes_claims(self, runner, conn, active_sprint, db_path):
        import json as _json
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, [
            "claim", "create", "--item-id", str(iid), "--agent", "bot-1",
            "--branch", "feat/check",
        ])
        result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = _json.loads(result.output)
        assert "item" in data
        assert "active_claims" in data
        assert len(data["active_claims"]) == 1
        assert data["active_claims"][0]["branch"] == "feat/check"
