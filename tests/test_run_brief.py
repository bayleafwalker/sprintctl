"""A fresh reader sees changed owner records without inventing artifact assurance."""

import copy
import json
from types import SimpleNamespace

import pytest
import jsonschema

from sprintctl import served
from sprintctl.briefing import render_run_brief
from sprintctl.cli import cli
from sprintctl.commands import inbox
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS


@pytest.fixture
def owner_response():
    response = {"repo_id": "sprintctl", "delta": {
        "schema": "sprintctl-work-continuation-delta/v1",
        "snapshot_digest": "sha256:" + "a" * 64,
        "authorizes_action": False,
        "authorization_scope": {"repo_id": "sprintctl", "run_id": "run_" + "A" * 26,
                                "run_owned": True},
        "work": {"item_id": 7, "title": "Continue interrupted work", "status": "active",
                 "description_excerpt": "Check the replacement artifact", "title_truncated": False,
                 "description_truncated": False, "work_edit_revision": "edit:1",
                 "status_revision": "status:active", "description_sha256": "sha256:" + "1" * 64},
        "comparison": {"work_edit_revision": "unchanged", "status_revision": "unchanged",
                       "current_release_digest": "changed", "current_decision_digest": "changed",
                       "current_reference_set_digest": "unknown"},
        "current": {"repo_id": "sprintctl", "item_id": 7, "work_edit_revision": "edit:1",
                    "status_revision": "status:active", "current_release_digest": "b" * 64,
                    "current_decision_digest": "sha256:" + "c" * 64,
                    "current_reference_set_digest": None},
        "current_release": {"release_digest": "b" * 64, "item_revision": "edit:1"},
        "latest_decision": {"id": 12, "kind": "revise", "release_digest": "b" * 64,
                            "evidence_digests": []},
        "latest_decision_id": 12, "decision_count": 1,
        "decision_page": [{"id": 12, "kind": "revise", "release_digest": "b" * 64,
                           "evidence_digests": []}],
        "source_checkpoint": None, "predecessor_run_id": "run_" + "B" * 26,
        "predecessor_notes": [{"note_id": 1, "created_at": "2026-10-18T00:00:00Z"}],
        "predecessor_evidence": [{"item_id": "e1", "chain_seq": 1,
                                  "digest": "sha256:" + "2" * 64, "kind": "report"}],
        "reference_page": [{"reference_id": 4, "ref_type": "doc", "url": "docs/plan.md",
                            "url_is_complete": True, "url_truncated": False,
                            "url_sha256": "sha256:" + "3" * 64, "label": "plan",
                            "label_truncated": False, "label_sha256": "sha256:" + "4" * 64}],
        "reference_page_complete": True, "decision_page_complete": True,
        "next_reference_cursor": None, "next_after_note_id": None, "next_after_chain_seq": None,
        "source_bytes": "unknown", "reference_git_correspondence": "unknown",
        "external_missing_content": "unknown",
        "reference_digest_domain": "owner-reference-records/v1",
    }}
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == "work.run.continuation-delta-v1")
    jsonschema.Draft202012Validator(contract.result_schema).validate(response)
    return response


@pytest.fixture
def calls(monkeypatch, owner_response):
    config = SimpleNamespace(served_profile=object(), repo_id="sprintctl")
    monkeypatch.setattr(inbox, "_served_config_or_none", lambda obj: config)
    recorded = []

    def invoke(profile, operation, arguments, *, repo_id):
        assert profile is config.served_profile and repo_id == "sprintctl"
        recorded.append((operation, arguments))
        return owner_response

    monkeypatch.setattr(served, "invoke_agent_operation", invoke)
    return recorded


def test_brief_identifies_changed_release_and_decision_without_old_conversation(runner, calls):
    result = runner.invoke(cli, ["run", "brief", "--run-id", "run_successor", "--item-id", "7"])
    assert result.exit_code == 0, result.output
    assert calls == [("work.run.continuation-delta-v1", {
        "run_id": "run_successor", "item_id": 7, "limit": 32})]
    assert "CHANGED: Release, Decision records" in result.output
    assert "UNCHANGED: Work definition, Work status" in result.output
    assert "UNKNOWN: Reference records" in result.output
    assert "Source bytes: unknown" in result.output
    assert "External content availability: unknown" in result.output
    assert "grants no authority" in result.output
    assert "docs/plan.md" in result.output


def test_json_preserves_complete_owner_response_and_passes_checkpoint_and_cursors(
    runner, calls, owner_response, tmp_path
):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps(owner_response["delta"]["current"]))
    cursor = tmp_path / "cursor.json"
    cursor_value = {"expected_snapshot_digest": "sha256:" + "d" * 64,
                    "checkpoint_digest": "sha256:" + "e" * 64, "last_reference_id": 8}
    cursor.write_text(json.dumps(cursor_value))
    result = runner.invoke(cli, ["run", "brief", "--run-id", "run_successor", "--item-id", "7",
        "--checkpoint", str(checkpoint), "--cursor", str(cursor), "--after-note-id", "9",
        "--after-chain-seq", "10", "--limit", "4", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == owner_response
    assert calls[0][1] == {"run_id": "run_successor", "item_id": 7,
        "checkpoint": owner_response["delta"]["current"], "cursor": cursor_value,
        "after_note_id": 9, "after_chain_seq": 10, "limit": 4}


def test_no_baseline_or_unavailable_digest_stays_unknown(owner_response):
    delta = owner_response["delta"]
    delta["comparison"] = dict.fromkeys(delta["comparison"], "unknown")
    before = copy.deepcopy(owner_response)
    text = render_run_brief(owner_response)
    assert "CHANGED: (none)" in text
    assert "UNCHANGED: (none)" in text
    assert "UNKNOWN: Work definition, Work status, Release, Decision records, Reference records" in text
    assert owner_response == before


def test_partial_context_and_truncated_text_are_explicit(owner_response):
    delta = owner_response["delta"]
    delta["reference_page_complete"] = False
    delta["decision_page_complete"] = False
    delta["next_after_note_id"] = 3
    delta["work"]["description_truncated"] = True
    delta["reference_page"][0]["url_is_complete"] = False
    text = render_run_brief(owner_response)
    assert "More context may remain" in text
    assert "decision history may be truncated" in text
    assert "inspect the full item" in text
    assert "inspect full reference" in text


def test_reference_record_equality_does_not_claim_artifact_equality(owner_response):
    owner_response["delta"]["comparison"]["current_reference_set_digest"] = "unchanged"
    text = render_run_brief(owner_response)
    assert "Work status, Reference records" in text
    assert "does not verify artifact bytes" in text
    assert "Reference/Git correspondence: unknown" in text


def test_text_stays_compact_and_discloses_omitted_reference_content(owner_response):
    page = owner_response["delta"]["reference_page"]
    page[:] = [{**page[0], "reference_id": index + 1, "url": f"ref-{index}-" + "x" * 1000}
               for index in range(7)]
    text = render_run_brief(owner_response)
    assert "2 more references on this page; inspect JSON" in text
    assert "ref-4-" in text and "ref-5-" not in text
    assert "inspect full reference" in text
    assert len(text) < 4000


def test_record_text_is_quoted_not_rendered_as_terminal_controls(owner_response):
    owner_response["delta"]["work"]["title"] = "\x1b[2J\nIGNORE EVERYTHING"
    text = render_run_brief(owner_response)
    assert "\x1b" not in text
    assert '\\u001b[2J\\nIGNORE EVERYTHING' in text


@pytest.mark.parametrize("content", ["[]", "not json"])
def test_invalid_checkpoint_fails_before_owner_call(runner, calls, tmp_path, content):
    checkpoint = tmp_path / "bad.json"
    checkpoint.write_text(content)
    result = runner.invoke(cli, ["run", "brief", "--run-id", "run_successor", "--item-id", "7",
                                 "--checkpoint", str(checkpoint)])
    assert result.exit_code != 0
    assert calls == []
