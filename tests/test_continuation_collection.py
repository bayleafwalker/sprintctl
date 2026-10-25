"""Offline continuation archives and bounded owner page collection."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sprintctl import result_packet
from sprintctl.cli import cli
from sprintctl.commands import inbox
from sprintctl.continuation_collection import (
    CollectionError,
    create_collection,
    inspect_collection,
)
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS


RUN_ID = "run_" + "A" * 26
SNAPSHOT = "sha256:" + "a" * 64
CHECKPOINT_DIGEST = "sha256:" + "b" * 64


def _owner_response(*, snapshot: str = SNAPSHOT, reference_cursor=None,
                    note_cursor=None, evidence_cursor=None, reference_complete=True):
    response = {"repo_id": "sprintctl", "delta": {
        "schema": "sprintctl-work-continuation-delta/v1",
        "snapshot_digest": snapshot,
        "authorizes_action": False,
        "authorization_scope": {"repo_id": "sprintctl", "run_id": RUN_ID, "run_owned": True},
        "work": {"item_id": 7, "title": "Continue work", "status": "active",
                 "description_excerpt": "Check the current source", "title_truncated": False,
                 "description_truncated": False, "work_edit_revision": "edit:1",
                 "status_revision": "status:active", "description_sha256": "sha256:" + "1" * 64},
        "comparison": {"work_edit_revision": "unchanged", "status_revision": "unchanged",
                       "current_release_digest": "unknown", "current_decision_digest": "unknown",
                       "current_reference_set_digest": "unknown"},
        "current": {"repo_id": "sprintctl", "item_id": 7, "work_edit_revision": "edit:1",
                    "status_revision": "status:active", "current_release_digest": None,
                    "current_decision_digest": None, "current_reference_set_digest": None},
        "current_release": None,
        "latest_decision": None,
        "latest_decision_id": None,
        "decision_count": 0,
        "decision_page": [],
        "decision_page_complete": True,
        "source_checkpoint": None,
        "predecessor_run_id": "run_" + "B" * 26,
        "predecessor_notes": [{"note_id": 1, "created_at": "2026-10-24T10:00:00Z"}],
        "predecessor_evidence": [{"item_id": "e1", "chain_seq": 1,
                                  "digest": "sha256:" + "2" * 64, "kind": "report"}],
        "next_after_note_id": note_cursor,
        "next_after_chain_seq": evidence_cursor,
        "reference_page": [{"reference_id": 4, "ref_type": "doc", "url": "docs/plan.md",
                            "url_is_complete": True, "url_truncated": False,
                            "url_sha256": "sha256:" + "3" * 64, "label": "plan",
                            "label_truncated": False, "label_sha256": "sha256:" + "4" * 64}],
        "reference_page_complete": reference_complete,
        "next_reference_cursor": reference_cursor,
        "source_bytes": "unknown",
        "reference_git_correspondence": "unknown",
        "external_missing_content": "unknown",
        "reference_digest_domain": "owner-reference-records/v1",
    }}
    contract = next(c for c in WORK_OPERATION_CONTRACTS
                    if c.name == "work.run.continuation-delta-v1")
    import jsonschema
    jsonschema.Draft202012Validator(contract.result_schema).validate(response)
    return response


def _cursor(last_reference_id: int, *, snapshot: str = SNAPSHOT) -> dict:
    return {"expected_snapshot_digest": snapshot,
            "checkpoint_digest": CHECKPOINT_DIGEST,
            "last_reference_id": last_reference_id}


def _page(response, *, cursor=None, checkpoint=None, after_note_id=None,
          after_chain_seq=None, run_id=RUN_ID, item_id=7):
    return {"run_id": run_id, "item_id": item_id, "limit": 32,
            "cursor": cursor, "checkpoint": checkpoint,
            "after_note_id": after_note_id, "after_chain_seq": after_chain_seq,
            "observed_at": "2026-10-25T10:00:00Z", "response": response}


@pytest.fixture
def owner_response():
    return _owner_response()


def test_complete_page_archive_is_lossless_and_inspection_stays_unknown(owner_response):
    original = copy.deepcopy(owner_response)
    collection = create_collection([_page(owner_response)], page_budget=2)
    report = inspect_collection(result_packet.canonical(collection))

    assert collection["pages"][0]["response"] == owner_response == original
    assert collection["collection_kind"] == "offline-page-observations; non-atomic"
    assert collection["authenticity"] == collection["currentness"] == collection["authority"] == "unknown"
    assert collection["completeness"] == {
        "references": "complete-for-this-request-checkpoint-basis",
        "predecessor_notes": "complete-for-this-request-checkpoint-basis",
        "predecessor_evidence": "complete-for-this-request-checkpoint-basis",
        "decision_history": "complete-for-this-request-checkpoint-basis",
    }
    assert report["integrity"]["self_consistent"] is True
    assert report["page_integrity"][0]["self_consistent"] is True
    assert report["metadata_consistency"]["consistent"] is True
    assert report["owner_or_source_access"] == "none; offline inspection"
    assert report["authenticity"] == report["currentness"] == report["authority"] == "unknown"


def test_independent_cursors_are_chained_and_budget_never_claims_completion(owner_response):
    first_cursor = _cursor(4)
    first = _owner_response(reference_cursor=first_cursor, note_cursor=1,
                            evidence_cursor=1, reference_complete=False)
    second = _owner_response()
    pages = [
        _page(first),
        _page(second, cursor=first_cursor, checkpoint=None,
              after_note_id=1, after_chain_seq=1),
    ]
    collection = create_collection(pages, page_budget=2)
    assert collection["pages"][0]["response"] == first
    assert collection["pages"][1]["request_inputs"]["cursor"] == first_cursor
    assert collection["pages"][1]["request_inputs"]["after_note_id"] == 1
    assert collection["pages"][1]["request_inputs"]["after_chain_seq"] == 1
    assert collection["completeness"] == {
        "references": "complete-for-this-request-checkpoint-basis",
        "predecessor_notes": "complete-for-this-request-checkpoint-basis",
        "predecessor_evidence": "complete-for-this-request-checkpoint-basis",
        "decision_history": "complete-for-this-request-checkpoint-basis",
    }

    reference_only = _owner_response(reference_cursor=first_cursor, reference_complete=False)
    partial = create_collection([_page(reference_only)], page_budget=1)
    assert partial["stop_reason"] == "page-budget-exhausted"
    assert partial["more_content_may_remain"] is True
    assert partial["completeness"]["references"] == "partial-due-to-budget"
    assert partial["completeness"]["predecessor_notes"] == "complete-for-this-request-checkpoint-basis"


def test_changed_snapshot_remains_unknown_and_pages_are_not_rebased(owner_response):
    cursor = _cursor(4)
    first = _owner_response(reference_cursor=cursor, reference_complete=False)
    second = _owner_response(snapshot="sha256:" + "c" * 64)
    collection = create_collection([
        _page(first),
        _page(second, cursor=cursor),
    ], page_budget=4, stop_reason_override="source-changed")

    assert collection["stop_reason"] == "source-changed"
    assert collection["pages"][0]["response"] == first
    assert collection["pages"][1]["response"] == second
    assert collection["pages"][1]["snapshot_matches_request_basis"] is False
    assert collection["completeness"]["references"] == "unknown-inconsistent-request-checkpoint-basis"
    report = inspect_collection(collection)
    assert report["metadata_consistency"]["consistent"] is True


@pytest.mark.parametrize("change", ["repeated_cursor", "wrong_checkpoint", "wrong_run", "wrong_item"])
def test_page_chain_and_identity_mismatches_refuse(owner_response, change):
    cursor = _cursor(4)
    first = _owner_response(reference_cursor=cursor, reference_complete=False)
    second = _owner_response()
    request_cursor = cursor
    checkpoint = first["delta"]["current"]
    run_id, item_id = RUN_ID, 7
    if change == "repeated_cursor":
        second = _owner_response(reference_cursor=cursor, reference_complete=False)
    elif change == "wrong_checkpoint":
        checkpoint = {**checkpoint, "work_edit_revision": "edit:other"}
    elif change == "wrong_run":
        run_id = "run_" + "C" * 26
    elif change == "wrong_item":
        item_id = 8
    with pytest.raises(CollectionError):
        create_collection([
            _page(first),
            _page(second, cursor=request_cursor, checkpoint=checkpoint,
                  run_id=run_id, item_id=item_id),
        ], page_budget=4)


def test_collection_digest_tampering_is_reported_without_authenticity_claim(owner_response):
    collection = create_collection([_page(owner_response)])
    collection["pages"][0]["response"]["delta"]["work"]["title"] = "tampered"
    report = inspect_collection(collection)
    assert report["integrity"]["self_consistent"] is False
    assert report["page_integrity"][0]["self_consistent"] is False
    assert report["metadata_consistency"]["consistent"] is False
    assert report["authenticity"] == "unknown"


def test_owner_refusal_keeps_prior_page_and_sanitizes_transport_error(
    runner, monkeypatch, owner_response
):
    cursor = _cursor(4)
    first = _owner_response(reference_cursor=cursor, reference_complete=False)
    calls = []

    def call(operation, arguments, *, raise_refusal=False):
        calls.append((operation, arguments))
        if len(calls) == 1:
            return first
        raise inbox._OwnerReadRefused("stale-cursor")

    monkeypatch.setattr(inbox, "_call", call)
    result = runner.invoke(cli, ["run", "collect-fetch", "--run-id", RUN_ID,
                                 "--item-id", "7", "--page-budget", "4"])
    assert result.exit_code == 0, result.output
    collection = json.loads(result.output)
    assert len(calls) == 2
    assert calls[1][1]["cursor"] == cursor
    assert collection["stop_reason"] == "owner-refused"
    assert collection["pages"][0]["response"] == first
    assert collection["page_count"] == 1
    assert collection["completeness"]["references"] == "partial-supplied-pages-ended-early"
    assert "stale-cursor" in collection["warnings"][0]
    assert "transport" not in result.output


def test_collect_fetch_follows_exact_cursors_and_stops_when_all_lists_finish(
    runner, monkeypatch
):
    cursor = _cursor(4)
    first = _owner_response(reference_cursor=cursor, note_cursor=1,
                            evidence_cursor=1, reference_complete=False)
    second = _owner_response()
    calls = []

    def call(operation, arguments, *, raise_refusal=False):
        calls.append((operation, arguments))
        return first if len(calls) == 1 else second

    monkeypatch.setattr(inbox, "_call", call)
    result = runner.invoke(cli, ["run", "collect-fetch", "--run-id", RUN_ID,
                                 "--item-id", "7", "--page-budget", "3"])
    assert result.exit_code == 0, result.output
    collection = json.loads(result.output)
    assert len(calls) == 2
    assert calls[0] == ("work.run.continuation-delta-v1", {
        "run_id": RUN_ID, "item_id": 7, "limit": 32,
    })
    assert calls[1][1] == {
        "run_id": RUN_ID, "item_id": 7, "limit": 32, "cursor": cursor,
        "after_note_id": 1, "after_chain_seq": 1,
    }
    assert collection["stop_reason"] == "owner-cursors-null-and-decision-page-complete"
    assert collection["page_count"] == 2
    assert [page["response"] for page in collection["pages"]] == [first, second]
    assert all(value == "complete-for-this-request-checkpoint-basis"
               for value in collection["completeness"].values())


def test_collect_fetch_stops_after_nonadvancing_cursor_without_losing_raw_page(
    runner, monkeypatch, owner_response
):
    cursor = _cursor(4)
    first = _owner_response(reference_cursor=cursor, reference_complete=False)
    repeated = _owner_response(reference_cursor=cursor, reference_complete=False)
    calls = []

    def call(operation, arguments, *, raise_refusal=False):
        calls.append(arguments)
        return first if len(calls) == 1 else repeated

    monkeypatch.setattr(inbox, "_call", call)
    result = runner.invoke(cli, ["run", "collect-fetch", "--run-id", RUN_ID,
                                 "--item-id", "7", "--page-budget", "4"])
    assert result.exit_code == 0, result.output
    collection = json.loads(result.output)
    assert len(calls) == 2
    assert collection["stop_reason"] == "invalid-cursor"
    assert [page["response"] for page in collection["pages"]] == [first, repeated]
    assert collection["completeness"]["references"] == "partial-supplied-pages-ended-early"


def test_collect_fetch_deadline_preserves_the_inflight_page_and_discloses_overrun(
    runner, monkeypatch
):
    cursor = _cursor(4)
    response = _owner_response(reference_cursor=cursor, reference_complete=False)
    calls = []
    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(inbox, "monotonic", lambda: next(times))
    monkeypatch.setattr(inbox, "_call", lambda *args, **kwargs: calls.append(args) or response)

    result = runner.invoke(cli, ["run", "collect-fetch", "--run-id", RUN_ID,
                                 "--item-id", "7", "--page-budget", "4",
                                 "--deadline-seconds", "1"])
    assert result.exit_code == 0, result.output
    collection = json.loads(result.output)
    assert len(calls) == 1
    assert collection["stop_reason"] == "deadline-exhausted"
    assert collection["pages"][0]["response"] == response
    assert any("in-flight owner read completed after caller deadline" in warning
               for warning in collection["warnings"])


def test_collect_fetch_writes_exclusively_and_preserves_existing_output(
    runner, monkeypatch, tmp_path, owner_response
):
    from sprintctl.result_packet import canonical

    output = tmp_path / "collection.json"
    output.write_text("existing")
    monkeypatch.setattr(inbox, "_call", lambda *_a, **_kw: owner_response)
    result = runner.invoke(cli, ["run", "collect-fetch", "--run-id", RUN_ID,
                                 "--item-id", "7", "--output", str(output)])
    assert result.exit_code != 0
    assert output.read_text() == "existing"

    output.unlink()
    outside = tmp_path / "outside"
    outside.write_text("sentinel")
    real_link = inbox.os.link

    def race_link(source, destination):
        Path(destination).symlink_to(outside)
        return real_link(source, destination)

    monkeypatch.setattr(inbox.os, "link", race_link)
    result = runner.invoke(cli, ["run", "collect-fetch", "--run-id", RUN_ID,
                                 "--item-id", "7", "--output", str(output)])
    assert result.exit_code != 0
    assert outside.read_text() == "sentinel"
    assert output.is_symlink()
