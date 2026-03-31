"""
Tests for Phase 6: worktree-awareness + git-context handoff.

item note accepts --git-branch, --git-sha, --git-worktree.
New `git-context` CLI command reads the current git state.
claim handoff accepts --git-branch / --git-sha to include in payload.
"""

import json
import subprocess

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


def _claim(conn, sprint_id, work_item_id, actor="agent"):
    cid = db.create_claim(conn, work_item_id, agent=actor, claim_type="execute")
    claim = db.get_claim(conn, cid, include_secret=True)
    return cid, claim["claim_token"]


# ---------------------------------------------------------------------------
# item note with git context
# ---------------------------------------------------------------------------


def _payload(event: dict) -> dict:
    """Deserialize the payload field of an event row."""
    p = event.get("payload", "{}")
    return json.loads(p) if isinstance(p, str) else p


class TestItemNoteGitContext:
    def test_git_branch_stored_in_payload(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "decision",
            "--summary", "made a choice",
            "--git-branch", "feature/my-branch",
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        events = [
            e for e in db.list_events(conn, active_sprint["id"])
            if e.get("work_item_id") == iid
        ]
        assert len(events) == 1
        assert _payload(events[0])["git_branch"] == "feature/my-branch"

    def test_git_sha_stored_in_payload(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "update",
            "--summary", "progress",
            "--git-sha", "abc1234",
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        events = [e for e in db.list_events(conn, active_sprint["id"])
                  if e.get("work_item_id") == iid]
        assert _payload(events[0])["git_sha"] == "abc1234"

    def test_git_worktree_stored_in_payload(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "update",
            "--summary", "working in worktree",
            "--git-worktree", "/projects/dev/sprintctl-wt-feature",
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        events = [e for e in db.list_events(conn, active_sprint["id"])
                  if e.get("work_item_id") == iid]
        assert _payload(events[0])["git_worktree"] == "/projects/dev/sprintctl-wt-feature"

    def test_all_git_fields_combined(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "claim-handoff",
            "--summary", "handing off",
            "--git-branch", "feat/x",
            "--git-sha", "deadbeef",
            "--git-worktree", "/tmp/wt",
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        events = [e for e in db.list_events(conn, active_sprint["id"])
                  if e.get("work_item_id") == iid]
        p = _payload(events[0])
        assert p["git_branch"] == "feat/x"
        assert p["git_sha"] == "deadbeef"
        assert p["git_worktree"] == "/tmp/wt"

    def test_git_fields_absent_when_not_provided(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "decision",
            "--summary", "simple note",
            "--actor", "agent",
        ])
        events = [e for e in db.list_events(conn, active_sprint["id"])
                  if e.get("work_item_id") == iid]
        p = _payload(events[0])
        assert "git_branch" not in p
        assert "git_sha" not in p
        assert "git_worktree" not in p

    def test_git_context_combined_with_evidence(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        src_eid = db.create_event(conn, active_sprint["id"], "agent", event_type="decision",
                                  work_item_id=iid, payload={"summary": "x"})
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "pattern-noted",
            "--summary", "pattern with full context",
            "--evidence-event-id", str(src_eid),
            "--git-branch", "main",
            "--git-sha", "cafe999",
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        candidates = db.list_knowledge_candidates(conn, active_sprint["id"])
        p = candidates[0]["payload"]
        assert p["evidence_event_id"] == src_eid
        assert p["git_branch"] == "main"
        assert p["git_sha"] == "cafe999"


# ---------------------------------------------------------------------------
# git-context command
# ---------------------------------------------------------------------------


class TestGitContextCommand:
    def test_git_context_exits_zero_in_git_repo(self, runner, db_path):
        result = runner.invoke(cli, ["git-context"])
        assert result.exit_code == 0, result.output

    def test_git_context_json_has_branch(self, runner, db_path):
        result = runner.invoke(cli, ["git-context", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "branch" in data

    def test_git_context_json_has_sha(self, runner, db_path):
        result = runner.invoke(cli, ["git-context", "--json"])
        data = json.loads(result.output)
        assert "sha" in data

    def test_git_context_json_has_worktree(self, runner, db_path):
        result = runner.invoke(cli, ["git-context", "--json"])
        data = json.loads(result.output)
        assert "worktree" in data

    def test_git_context_text_output_contains_branch(self, runner, db_path):
        result = runner.invoke(cli, ["git-context"])
        assert "branch" in result.output.lower() or "Branch" in result.output

    def test_git_context_outside_repo(self, runner, db_path, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["git-context"])
        # Should exit non-zero or show a clear "not a git repo" message
        assert result.exit_code != 0 or "not a git" in result.output.lower()


# ---------------------------------------------------------------------------
# claim handoff with git context
# ---------------------------------------------------------------------------


class TestClaimHandoffGitContext:
    def test_claim_handoff_branch_recorded(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        claim_id, token = _claim(conn, active_sprint["id"], iid)
        result = runner.invoke(cli, [
            "claim", "handoff",
            "--id", str(claim_id),
            "--claim-token", token,
            "--actor", "agent-2",
            "--branch", "feat/handoff-branch",
        ])
        assert result.exit_code == 0, result.output
        # New claim should carry branch on the claim record
        claims = db.list_claims_by_sprint(conn, active_sprint["id"], active_only=False)
        new_claim = next((c for c in claims if c["actor"] == "agent-2"), None)
        assert new_claim is not None
        assert new_claim["branch"] == "feat/handoff-branch"

    def test_claim_handoff_commit_sha_recorded(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        claim_id, token = _claim(conn, active_sprint["id"], iid)
        result = runner.invoke(cli, [
            "claim", "handoff",
            "--id", str(claim_id),
            "--claim-token", token,
            "--actor", "agent-2",
            "--branch", "main",
            "--commit-sha", "abc1234",
        ])
        assert result.exit_code == 0, result.output
        claims = db.list_claims_by_sprint(conn, active_sprint["id"], active_only=False)
        new_claim = next((c for c in claims if c["actor"] == "agent-2"), None)
        assert new_claim is not None
        assert new_claim["commit_sha"] == "abc1234"

    def test_claim_handoff_git_context_in_event_payload(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        claim_id, token = _claim(conn, active_sprint["id"], iid)
        result = runner.invoke(cli, [
            "claim", "handoff",
            "--id", str(claim_id),
            "--claim-token", token,
            "--actor", "agent-2",
            "--branch", "main",
            "--commit-sha", "abc1234",
        ])
        assert result.exit_code == 0, result.output
        events = db.list_events(conn, active_sprint["id"])
        handoff_events = [e for e in events if e["event_type"] in ("claim-handoff", "claim-ownership-corrected")]
        assert len(handoff_events) > 0
        last = handoff_events[-1]
        payload = json.loads(last["payload"])
        # to_identity carries the git context from the claim record
        assert payload["to_identity"]["branch"] == "main"
        assert payload["to_identity"]["commit_sha"] == "abc1234"
