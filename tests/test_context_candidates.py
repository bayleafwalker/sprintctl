"""Tests for the Tier-1 context-candidates packet (item #1160).

Covers the pure ranking function in ``sprintctl/context_candidates.py`` and
the ``sprintctl context-candidates`` CLI surface wired on top of it.
"""

import json

from sprintctl import context_candidates as cc
from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task", track="eng", description=None):
    tid = db.get_or_create_track(conn, sprint_id, track)
    iid = db.create_work_item(conn, sprint_id, tid, title)
    if description:
        db.update_work_item_description(conn, iid, description)
    return iid


def _ready(conn, sprint_id):
    return db.get_ready_items(conn, sprint_id)


# ---------------------------------------------------------------------------
# Pure ranking function
# ---------------------------------------------------------------------------

class TestBuildContextCandidates:
    def test_empty_pool_returns_empty_bounded_packet(self):
        payload = cc.build_context_candidates(ready_items=[], refs_by_item={})
        assert payload["candidates"] == []
        assert payload["truncated"] is False
        assert payload["explicit_target"] is None
        assert payload["contract_version"] == "1"

    def test_explicit_target_ranks_first_and_is_claim_eligible(self):
        items = [
            {"id": 1, "title": "A", "status": "pending", "track_name": "eng"},
            {"id": 2, "title": "B", "status": "pending", "track_name": "eng"},
        ]
        payload = cc.build_context_candidates(
            ready_items=items,
            refs_by_item={},
            explicit_item_id=2,
            explicit_item=items[1],
        )
        assert payload["candidates"][0]["item_id"] == 2
        assert payload["candidates"][0]["rank"] == cc.RANK_EXPLICIT_TARGET
        assert payload["candidates"][0]["rank_reason"] == "explicit-target"
        assert payload["candidates"][0]["claim_eligible"] is True
        # Explicit target is not duplicated when it also appears in the pool.
        ids = [c["item_id"] for c in payload["candidates"]]
        assert ids.count(2) == 1

    def test_explicit_target_not_found_is_reported_not_raised(self):
        payload = cc.build_context_candidates(
            ready_items=[],
            refs_by_item={},
            explicit_item_id=999,
            explicit_item=None,
        )
        assert payload["explicit_target"] == {"item_id": 999, "found": False}
        assert payload["candidates"] == []

    def test_only_rank_one_is_ever_claim_eligible(self):
        items = [{"id": 1, "title": "A", "status": "pending", "track_name": "eng"}]
        payload = cc.build_context_candidates(ready_items=items, refs_by_item={})
        assert payload["candidates"][0]["rank"] == cc.RANK_REPO_LEVEL
        assert payload["candidates"][0]["claim_eligible"] is False

    def test_explicit_target_not_pending_is_not_claim_eligible(self):
        item = {"id": 1, "title": "A", "status": "active", "track_name": "eng"}
        payload = cc.build_context_candidates(
            ready_items=[],
            refs_by_item={},
            explicit_item_id=1,
            explicit_item=item,
        )
        assert payload["candidates"][0]["claim_eligible"] is False

    def test_path_overlap_outranks_lexical_and_repo_level(self):
        items = [
            {"id": 1, "title": "Unrelated", "status": "pending", "track_name": "eng"},
            {"id": 2, "title": "Path match", "status": "pending", "track_name": "eng"},
        ]
        refs_by_item = {
            2: [{"ref_type": "file", "scope": {"kind": "file", "value": "sprintctl/cli.py"}}],
        }
        payload = cc.build_context_candidates(
            ready_items=items,
            refs_by_item=refs_by_item,
            target_paths=["sprintctl/cli.py"],
        )
        assert payload["candidates"][0]["item_id"] == 2
        assert payload["candidates"][0]["rank"] == cc.RANK_PATH_OVERLAP
        assert payload["candidates"][0]["claim_eligible"] is False
        assert payload["candidates"][1]["item_id"] == 1
        assert payload["candidates"][1]["rank"] == cc.RANK_REPO_LEVEL

    def test_glob_ref_matches_via_scope_ref_matches_path(self):
        items = [{"id": 1, "title": "Docs item", "status": "pending", "track_name": "eng"}]
        refs_by_item = {
            1: [{"ref_type": "glob", "scope": {"kind": "glob", "value": "docs/**/*.md"}}],
        }
        payload = cc.build_context_candidates(
            ready_items=items,
            refs_by_item=refs_by_item,
            target_paths=["docs/plans/foo.md"],
        )
        assert payload["candidates"][0]["rank"] == cc.RANK_PATH_OVERLAP

    def test_linked_doc_ranks_above_lexical_and_repo_level(self):
        items = [
            {"id": 1, "title": "Plain", "status": "pending", "track_name": "eng"},
            {"id": 2, "title": "Documented", "status": "pending", "track_name": "eng"},
        ]
        refs_by_item = {2: [{"ref_type": "doc", "url": "docs/plans/foo.md"}]}
        payload = cc.build_context_candidates(ready_items=items, refs_by_item=refs_by_item)
        assert payload["candidates"][0]["item_id"] == 2
        assert payload["candidates"][0]["rank"] == cc.RANK_LINKED_DOC

    def test_lexical_fallback_matches_query_terms(self):
        items = [
            {"id": 1, "title": "Unrelated widget", "status": "pending", "track_name": "eng"},
            {
                "id": 2,
                "title": "Authority projection cutover",
                "status": "pending",
                "track_name": "eng",
            },
        ]
        payload = cc.build_context_candidates(
            ready_items=items, refs_by_item={}, query="projection cutover"
        )
        assert payload["candidates"][0]["item_id"] == 2
        assert payload["candidates"][0]["rank"] == cc.RANK_LEXICAL
        assert payload["candidates"][1]["item_id"] == 1
        assert payload["candidates"][1]["rank"] == cc.RANK_REPO_LEVEL

    def test_lexical_fallback_orders_by_overlap_count_deterministically(self):
        items = [
            {"id": 1, "title": "authority only", "status": "pending", "track_name": "eng"},
            {
                "id": 2,
                "title": "authority projection watermark",
                "status": "pending",
                "track_name": "eng",
            },
        ]
        payload = cc.build_context_candidates(
            ready_items=items, refs_by_item={}, query="authority projection watermark"
        )
        # Item 2 overlaps on 3 terms, item 1 on 1 -- higher overlap ranks first.
        assert [c["item_id"] for c in payload["candidates"]] == [2, 1]

    def test_repo_level_fallback_preserves_incoming_order(self):
        items = [
            {"id": 3, "title": "C", "status": "pending", "track_name": "eng"},
            {"id": 1, "title": "A", "status": "pending", "track_name": "eng"},
            {"id": 2, "title": "B", "status": "pending", "track_name": "eng"},
        ]
        payload = cc.build_context_candidates(ready_items=items, refs_by_item={})
        assert [c["item_id"] for c in payload["candidates"]] == [3, 1, 2]

    def test_bound_truncates_and_reports_truncated(self):
        items = [
            {"id": i, "title": f"Item {i}", "status": "pending", "track_name": "eng"}
            for i in range(1, 8)
        ]
        payload = cc.build_context_candidates(ready_items=items, refs_by_item={}, limit=3)
        assert len(payload["candidates"]) == 3
        assert payload["bound"] == 3
        assert payload["truncated"] is True

    def test_limit_is_capped_at_max_candidate_limit(self):
        items = [
            {"id": i, "title": f"Item {i}", "status": "pending", "track_name": "eng"}
            for i in range(1, cc.MAX_CANDIDATE_LIMIT + 10)
        ]
        payload = cc.build_context_candidates(
            ready_items=items, refs_by_item={}, limit=cc.MAX_CANDIDATE_LIMIT + 5
        )
        assert payload["bound"] == cc.MAX_CANDIDATE_LIMIT
        assert len(payload["candidates"]) == cc.MAX_CANDIDATE_LIMIT

    def test_zero_or_negative_limit_raises(self):
        try:
            cc.build_context_candidates(ready_items=[], refs_by_item={}, limit=0)
        except cc.ContextCandidatesError:
            pass
        else:
            raise AssertionError("expected ContextCandidatesError")

    def test_watermark_is_passed_through_unchanged(self):
        watermark = {"ingest_offset": 42, "age_seconds": 3.5}
        payload = cc.build_context_candidates(
            ready_items=[], refs_by_item={}, watermark=watermark
        )
        assert payload["watermark"] == watermark

    def test_no_candidates_not_truncated(self):
        payload = cc.build_context_candidates(ready_items=[], refs_by_item={})
        assert payload["truncated"] is False


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

class TestContextCandidatesCLI:
    def test_requires_active_sprint(self, runner, db_path):
        result = runner.invoke(cli, ["context-candidates"])
        assert result.exit_code != 0

    def test_json_shape_with_no_items(self, runner, active_sprint):
        result = runner.invoke(cli, ["context-candidates", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["contract_version"] == "1"
        assert data["candidates"] == []
        assert data["sprint"]["id"] == active_sprint["id"]
        assert "projection" in data
        assert "watermark" in data

    def test_json_lists_ready_items_as_repo_level_candidates(self, runner, conn, active_sprint):
        iid = _item(conn, active_sprint["id"], "Ready Item")
        result = runner.invoke(cli, ["context-candidates", "--json"])
        data = json.loads(result.output)
        assert len(data["candidates"]) == 1
        assert data["candidates"][0]["item_id"] == iid
        assert data["candidates"][0]["rank_reason"] == "repo-level"
        assert data["candidates"][0]["claim_eligible"] is False

    def test_explicit_item_id_is_claim_eligible(self, runner, conn, active_sprint):
        iid = _item(conn, active_sprint["id"], "Target Item")
        result = runner.invoke(
            cli, ["context-candidates", "--item-id", str(iid), "--json"]
        )
        data = json.loads(result.output)
        assert data["explicit_target"] == {"item_id": iid, "found": True}
        explicit = next(c for c in data["candidates"] if c["item_id"] == iid)
        assert explicit["rank"] == 1
        assert explicit["claim_eligible"] is True

    def test_explicit_item_id_not_found(self, runner, active_sprint):
        result = runner.invoke(
            cli, ["context-candidates", "--item-id", "999999", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["explicit_target"] == {"item_id": 999999, "found": False}

    def test_path_option_ranks_matching_item_first(self, runner, conn, active_sprint):
        matching = _item(conn, active_sprint["id"], "Matches path")
        db.add_ref(conn, matching, "file", "sprintctl/cli.py")
        _item(conn, active_sprint["id"], "No match")
        result = runner.invoke(
            cli, ["context-candidates", "--path", "sprintctl/cli.py", "--json"]
        )
        data = json.loads(result.output)
        assert data["candidates"][0]["item_id"] == matching
        assert data["candidates"][0]["rank_reason"] == "path-scope-overlap"

    def test_query_option_ranks_lexical_match_first(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Unrelated widget task")
        matching = _item(conn, active_sprint["id"], "Authority cutover dogfood")
        result = runner.invoke(
            cli, ["context-candidates", "--query", "authority cutover", "--json"]
        )
        data = json.loads(result.output)
        assert data["candidates"][0]["item_id"] == matching
        assert data["candidates"][0]["rank_reason"] == "lexical-match"

    def test_limit_bounds_output(self, runner, conn, active_sprint):
        for i in range(4):
            _item(conn, active_sprint["id"], f"Item {i}")
        result = runner.invoke(
            cli, ["context-candidates", "--limit", "2", "--json"]
        )
        data = json.loads(result.output)
        assert len(data["candidates"]) == 2
        assert data["truncated"] is True

    def test_limit_zero_is_rejected(self, runner, active_sprint):
        result = runner.invoke(cli, ["context-candidates", "--limit", "0"])
        assert result.exit_code != 0

    def test_text_output_renders_table(self, runner, conn, active_sprint):
        _item(conn, active_sprint["id"], "Text Item")
        result = runner.invoke(cli, ["context-candidates"])
        assert result.exit_code == 0, result.output
        assert "Text Item" in result.output

    def test_explicit_target_not_found_text_output(self, runner, active_sprint):
        result = runner.invoke(cli, ["context-candidates", "--item-id", "999999"])
        assert result.exit_code == 0, result.output
        assert "not found" in result.output
