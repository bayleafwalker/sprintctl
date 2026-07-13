"""
Tests for the refs model (schema v7): external references on work items.
"""

import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

class TestRefDB:
    def test_add_and_list_ref(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        ref_id = db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1", "PR #1")
        refs = db.list_refs(conn, iid)
        assert len(refs) == 1
        assert refs[0]["id"] == ref_id
        assert refs[0]["ref_type"] == "pr"
        assert refs[0]["url"] == "https://github.com/org/repo/pull/1"
        assert refs[0]["label"] == "PR #1"
        assert refs[0]["work_item_id"] == iid

    def test_add_ref_no_label(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "issue", "https://github.com/org/repo/issues/42")
        refs = db.list_refs(conn, iid)
        assert refs[0]["label"] == ""

    def test_multiple_refs_on_same_item(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1")
        db.add_ref(conn, iid, "issue", "https://github.com/org/repo/issues/5")
        db.add_ref(conn, iid, "doc", "https://docs.example.com/spec")
        refs = db.list_refs(conn, iid)
        assert len(refs) == 3
        types = {r["ref_type"] for r in refs}
        assert types == {"pr", "issue", "doc"}

    def test_list_refs_empty(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        assert db.list_refs(conn, iid) == []

    def test_remove_ref(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        rid = db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/99")
        db.remove_ref(conn, rid, iid)
        assert db.list_refs(conn, iid) == []

    def test_remove_ref_not_found_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, 9999, iid)

    def test_remove_ref_wrong_item_raises(self, conn, active_sprint):
        iid1 = _item(conn, active_sprint["id"], "Item A")
        iid2 = _item(conn, active_sprint["id"], "Item B")
        rid = db.add_ref(conn, iid1, "pr", "https://github.com/org/repo/pull/1")
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, rid, iid2)

    def test_add_ref_invalid_type_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid ref_type"):
            db.add_ref(conn, iid, "wiki", "https://example.com")

    def test_add_ref_invalid_url_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid ref URL"):
            db.add_ref(conn, iid, "doc", "not-a-url")

    def test_add_ref_missing_item_raises(self, conn):
        with pytest.raises(ValueError, match="not found"):
            db.add_ref(conn, 9999, "pr", "https://github.com/org/repo/pull/1")

    def test_refs_deleted_on_item_cascade(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1")
        conn.execute("DELETE FROM work_item WHERE id = ?", (iid,))
        conn.commit()
        rows = conn.execute("SELECT * FROM ref WHERE work_item_id = ?", (iid,)).fetchall()
        assert rows == []

    def test_all_ref_types_accepted(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        for ref_type in ("pr", "issue", "doc", "other"):
            db.add_ref(conn, iid, ref_type, f"https://example.com/{ref_type}")
        refs = db.list_refs(conn, iid)
        assert len(refs) == 4


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------

class TestRefCLI:
    def test_item_ref_add_basic(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "ref", "add",
            "--id", str(iid),
            "--type", "pr",
            "--url", "https://github.com/org/repo/pull/7",
        ])
        assert result.exit_code == 0, result.output
        assert "pr" in result.output
        assert "github.com" in result.output

    def test_item_ref_add_with_label(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "ref", "add",
            "--id", str(iid),
            "--type", "issue",
            "--url", "https://github.com/org/repo/issues/42",
            "--label", "Bug #42",
        ])
        assert result.exit_code == 0, result.output
        refs = db.list_refs(conn, iid)
        assert refs[0]["label"] == "Bug #42"

    def test_item_ref_add_invalid_url_fails(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "ref", "add",
            "--id", str(iid),
            "--type", "pr",
            "--url", "relative/path",
        ])
        assert result.exit_code == 1
        assert "Invalid ref URL" in result.output

    def test_item_ref_add_doc_accepts_repo_relative_path(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "ref", "add",
            "--id", str(iid),
            "--type", "doc",
            "--url", "docs/plans/my-plan.md",
            "--label", "Plan",
        ])
        assert result.exit_code == 0, result.output
        refs = db.list_refs(conn, iid)
        assert refs[0]["url"] == "docs/plans/my-plan.md"
        assert refs[0]["ref_type"] == "doc"

    def test_item_ref_list_text(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "https://docs.example.com", "Spec")
        result = runner.invoke(cli, ["item", "ref", "list", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "doc" in result.output
        assert "docs.example.com" in result.output

    def test_item_ref_list_json(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1", "PR")
        result = runner.invoke(cli, ["item", "ref", "list", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["ref_type"] == "pr"
        assert data[0]["url"] == "https://github.com/org/repo/pull/1"

    def test_item_ref_list_empty(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["item", "ref", "list", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "No refs" in result.output

    def test_item_ref_list_unknown_item(self, runner, db_path):
        result = runner.invoke(cli, ["item", "ref", "list", "--id", "9999"])
        assert result.exit_code == 1

    def test_item_ref_remove(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        rid = db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1")
        result = runner.invoke(cli, [
            "item", "ref", "remove",
            "--id", str(iid),
            "--ref-id", str(rid),
        ])
        assert result.exit_code == 0, result.output
        assert db.list_refs(conn, iid) == []

    def test_item_ref_remove_wrong_item_fails(self, runner, conn, active_sprint, db_path):
        iid1 = _item(conn, active_sprint["id"], "Item A")
        iid2 = _item(conn, active_sprint["id"], "Item B")
        rid = db.add_ref(conn, iid1, "pr", "https://github.com/org/repo/pull/1")
        result = runner.invoke(cli, [
            "item", "ref", "remove",
            "--id", str(iid2),
            "--ref-id", str(rid),
        ])
        assert result.exit_code == 1

    def test_item_show_includes_refs_text(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/3", "Feature PR")
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "Refs:" in result.output
        assert "github.com" in result.output

    def test_item_show_json_includes_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "https://docs.example.com/spec", "Spec")
        result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "refs" in data
        assert len(data["refs"]) == 1
        assert data["refs"][0]["ref_type"] == "doc"

    def test_item_show_no_refs_placeholder_when_empty(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "Refs: (none" in result.output
        assert f"item ref add --id {iid} --type doc" in result.output


# ---------------------------------------------------------------------------
# Export / import round-trip with refs
# ---------------------------------------------------------------------------

class TestRefExportImport:
    def test_export_includes_refs(self, runner, conn, active_sprint, db_path, tmp_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/5", "PR #5")
        out = tmp_path / "sprint.json"
        result = runner.invoke(cli, [
            "export", "--sprint-id", str(active_sprint["id"]), "--output", str(out),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(out.read_text())
        assert "refs" in data
        assert str(iid) in data["refs"] or iid in data["refs"]

    def test_import_restores_refs(self, runner, conn, active_sprint, db_path, tmp_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "issue", "https://github.com/org/repo/issues/99", "Bug")
        out = tmp_path / "sprint.json"
        runner.invoke(cli, [
            "export", "--sprint-id", str(active_sprint["id"]), "--output", str(out),
        ])
        result = runner.invoke(cli, ["import", "--file", str(out)])
        assert result.exit_code == 0, result.output
        # Find the newly imported sprint (highest id)
        sprints = db.list_sprints(conn)
        new_sprint = sprints[0]  # most recently created
        items = db.list_work_items(conn, sprint_id=new_sprint["id"])
        assert len(items) == 1
        imported_refs = db.list_refs(conn, items[0]["id"])
        assert len(imported_refs) == 1
        assert imported_refs[0]["ref_type"] == "issue"
        assert imported_refs[0]["url"] == "https://github.com/org/repo/issues/99"
        assert imported_refs[0]["label"] == "Bug"

    def test_import_sprint_with_no_refs_succeeds(self, runner, conn, active_sprint, db_path, tmp_path):
        _item(conn, active_sprint["id"])
        out = tmp_path / "sprint.json"
        runner.invoke(cli, [
            "export", "--sprint-id", str(active_sprint["id"]), "--output", str(out),
        ])
        result = runner.invoke(cli, ["import", "--file", str(out)])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Ref surfacing on work-selection and resume surfaces
# ---------------------------------------------------------------------------

class TestRefSurfacing:
    def test_list_refs_for_items_bulk(self, conn, active_sprint):
        iid1 = _item(conn, active_sprint["id"], "Item A")
        iid2 = _item(conn, active_sprint["id"], "Item B")
        iid3 = _item(conn, active_sprint["id"], "Item C")
        db.add_ref(conn, iid1, "doc", "docs/plans/a.md", "Plan A")
        db.add_ref(conn, iid1, "pr", "https://github.com/org/repo/pull/1")
        db.add_ref(conn, iid2, "doc", "docs/plans/b.md")
        refs = db.list_refs_for_items(conn, [iid1, iid2, iid3])
        assert set(refs.keys()) == {iid1, iid2}
        assert [r["url"] for r in refs[iid1]] == [
            "docs/plans/a.md",
            "https://github.com/org/repo/pull/1",
        ]
        assert db.list_refs_for_items(conn, []) == {}

    def test_next_work_explain_json_includes_ready_item_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "docs/plans/plan.md", "Plan")
        result = runner.invoke(cli, [
            "next-work", "--sprint-id", str(active_sprint["id"]), "--json", "--explain",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ready = data["ready_items"][0]
        assert ready["id"] == iid
        assert len(ready["refs"]) == 1
        assert ready["refs"][0]["ref_type"] == "doc"
        assert ready["refs"][0]["url"] == "docs/plans/plan.md"

    def test_next_work_explain_text_lists_ready_item_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        other = _item(conn, active_sprint["id"], "No-doc task")
        db.add_ref(conn, iid, "doc", "docs/plans/plan.md", "Plan")
        result = runner.invoke(cli, [
            "next-work", "--sprint-id", str(active_sprint["id"]), "--explain",
        ])
        assert result.exit_code == 0, result.output
        assert f"#{iid}  [doc]  docs/plans/plan.md  Plan" in result.output
        assert f"(no refs: #{other})" in result.output

    def test_claim_start_text_echoes_item_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "docs/plans/plan.md", "Plan")
        result = runner.invoke(cli, [
            "claim", "start", "--item-id", str(iid), "--actor", "agent-a",
        ])
        assert result.exit_code == 0, result.output
        assert f"Refs on item #{iid}:" in result.output
        assert "[doc]  docs/plans/plan.md  Plan" in result.output

    def test_claim_start_text_nudges_when_no_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "claim", "start", "--item-id", str(iid), "--actor", "agent-a",
        ])
        assert result.exit_code == 0, result.output
        assert "Refs: (none" in result.output

    def test_claim_start_json_includes_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "docs/plans/plan.md")
        result = runner.invoke(cli, [
            "claim", "start", "--item-id", str(iid), "--actor", "agent-a", "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["refs"][0]["url"] == "docs/plans/plan.md"

    def test_claim_create_json_includes_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "docs/plans/plan.md")
        result = runner.invoke(cli, [
            "claim", "create", "--item-id", str(iid), "--actor", "agent-a", "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["refs"][0]["url"] == "docs/plans/plan.md"

    def test_session_resume_includes_claimed_item_refs(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "doc", "docs/plans/plan.md", "Plan")
        db.set_work_item_status(conn, iid, "active")
        db.create_claim(conn, iid, agent="agent-a")
        result = runner.invoke(cli, ["session", "resume", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        recovery_claims = data["claim_recovery"]["active_claims"]
        assert len(recovery_claims) == 1
        assert recovery_claims[0]["refs"][0]["url"] == "docs/plans/plan.md"

        text_result = runner.invoke(cli, ["session", "resume"])
        assert text_result.exit_code == 0, text_result.output
        assert "ref: [doc]  docs/plans/plan.md  Plan" in text_result.output
