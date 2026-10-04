"""Actual native owner/ledger histories, through the durable producer carrier."""

import json
import sqlite3
import uuid

import pytest

from sprintctl import evidence_intake as intake
from sprintctl import outbox, pg
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS
from tests.pg.test_run_evidence import (
    _app,
    _append_args,
    _context,
    _items,
    _ledger_rows,
    _new_run,
)

pytestmark = PG_MARKS


def setup(store, tmp_path):
    app = _app(store)
    run = _new_run(app, "intake-" + str(uuid.uuid4()))
    binding = app.invoke("work.run.resolve-v1", {"run_id": run}, _context())
    path = tmp_path / "outbox.sqlite3"
    args = _append_args(run, "intake-key-" + str(uuid.uuid4()))
    return app, binding, path, args


def capture(path, args, binding, **kwargs):
    # Preserve formatting/newline independently of owner canonical digest.
    raw = (json.dumps(args, indent=3) + "\n").encode()
    return intake.capture(
        path, raw, json.dumps(binding).encode(), repo_id=binding["repo_id"], **kwargs
    )


def sync(app, path, binding, invoke=None):
    return intake.synchronize(
        path,
        repo_id=binding["repo_id"],
        invoke=invoke or (lambda op, args: app.invoke(op, args, _context())),
        rejection_type=ApplicationRejection,
    )


def test_offline_capture_restart_and_lost_reply_replay(store, tmp_path):
    app, binding, path, args = setup(store, tmp_path)
    first = capture(path, args, binding)
    assert capture(path, args, binding)["duplicate"]
    assert _items(store, args["run_id"]) == 0

    def lost(op, values):
        result = app.invoke(op, values, _context())
        if op == intake.OPERATION:
            raise OSError("reply lost after owner commit")
        return result

    report = sync(app, path, binding, lost)
    assert report["evidence_attempts"][0]["phase"] == "unknown"
    assert report["pending_evidence_request_ids"] == [first["request_id"]]
    with pytest.raises(ValueError, match="key already captured"):
        capture(path, {**args, "chain_seq": 1}, binding, supersedes=first["request_id"])
    replay = sync(app, path, binding)
    assert replay["confirmed_evidence_request_ids"] == [first["request_id"]]
    assert not replay["pending_evidence_request_ids"]
    assert sync(app, path, binding)["evidence_attempts"] == []
    assert _items(store, args["run_id"]) == 1
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 1
    conn = outbox.open_outbox(path)
    assert (
        bytes(conn.execute("SELECT source FROM native_evidence_request").fetchone()[0])
        == (json.dumps(args, indent=3) + "\n").encode()
    )
    for table in ("native_evidence_request", "native_evidence_attempt"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
    conn.close()


def test_invalid_predecessor_rolls_back_and_explicit_correction_preserves_original(
    store, tmp_path
):
    app, binding, path, args = setup(store, tmp_path)
    args["chain_prev_digest"] = "wrong"
    initial = capture(path, args, binding)
    report = sync(app, path, binding)
    assert report["evidence_attempts"][0] == {
        "request_id": initial["request_id"],
        "phase": "rejected",
        "code": "evidence-chain-conflict",
        "operation": intake.OPERATION,
    }
    assert _items(store, args["run_id"]) == 0
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 0
    with pytest.raises(ValueError, match="only the expected chain tail"):
        capture(
            path,
            {**args, "digest": "changed", "chain_prev_digest": None},
            binding,
            supersedes=initial["request_id"],
        )
    corrected = capture(
        path,
        {**args, "chain_prev_digest": None},
        binding,
        supersedes=initial["request_id"],
    )
    report = sync(app, path, binding)
    assert report["confirmed_evidence_request_ids"] == [corrected["request_id"]]
    conn = outbox.open_outbox(path)
    assert (
        conn.execute("SELECT count(*) FROM native_evidence_request").fetchone()[0] == 2
    )
    assert (
        json.loads(
            conn.execute(
                "SELECT arguments_json FROM native_evidence_request WHERE request_id=?",
                (initial["request_id"],),
            ).fetchone()[0]
        )["chain_prev_digest"]
        == "wrong"
    )
    conn.close()


def test_stale_tail_refusal_then_corrected_retry_matches_online_history(
    store, tmp_path
):
    app, binding, path, args = setup(store, tmp_path)
    queued = capture(path, args, binding)
    first = app.invoke(
        intake.OPERATION,
        _append_args(args["run_id"], "online-first", item_id="first"),
        _context(),
    )
    assert (
        sync(app, path, binding)["evidence_attempts"][0]["code"]
        == "evidence-chain-conflict"
    )
    corrected = {
        **args,
        "chain_seq": 1,
        "chain_prev_digest": pg.evidence_entry_digest(first["item"]),
    }
    capture(path, corrected, binding, supersedes=queued["request_id"])
    assert not sync(app, path, binding)["pending_evidence_request_ids"]
    online_replay = app.invoke(intake.OPERATION, corrected, _context())
    conn = outbox.open_outbox(path)
    stored = json.loads(
        conn.execute(
            "SELECT result_json FROM native_evidence_attempt WHERE phase='confirmed'"
        ).fetchone()[0]
    )
    assert stored == online_replay
    conn.close()
    assert _items(store, args["run_id"]) == 2


def test_binding_refusal_and_idempotency_conflict_never_confirm(store, tmp_path):
    app, binding, path, args = setup(store, tmp_path)
    capture(path, args, binding)
    report = sync(
        app,
        path,
        binding,
        lambda op, values: app.invoke(op, values, _context(client_id="other-client")),
    )
    assert report["evidence_attempts"][0]["code"] == "run-not-found"
    assert _items(store, args["run_id"]) == 0
    # The native owner may have already used this key independently.
    app.invoke(intake.OPERATION, {**args, "digest": "different"}, _context())
    report = sync(app, path, binding)
    assert report["evidence_attempts"][0]["phase"] == "rejected"
    assert report["evidence_attempts"][0]["code"] == "idempotency-conflict"
    assert len(report["pending_evidence_request_ids"]) == 1
    assert _items(store, args["run_id"]) == 1


def test_malformed_reply_is_uncertain_and_cannot_authorize_correction(store, tmp_path):
    app, binding, path, args = setup(store, tmp_path)
    queued = capture(path, args, binding)

    def malformed(op, values):
        result = app.invoke(op, values, _context())
        if op == intake.OPERATION:
            result = {**result, "run_id": "different"}
        return result

    assert (
        sync(app, path, binding, malformed)["evidence_attempts"][0]["phase"]
        == "unknown"
    )
    with pytest.raises(ValueError, match="key already captured"):
        capture(
            path, {**args, "chain_seq": 1}, binding, supersedes=queued["request_id"]
        )
    assert not sync(app, path, binding)["pending_evidence_request_ids"]


def test_two_durable_producers_race_through_native_ledger(store, tmp_path, monkeypatch):
    from tests.pg.test_run_evidence import _race

    _authority, binding, path, args = setup(store, tmp_path)
    other = tmp_path / "second-producer.db"
    capture(path, args, binding)
    capture(other, args, binding)
    outcomes = _race(
        monkeypatch,
        store,
        "append_evidence",
        lambda authority: sync(authority, path, binding),
        lambda authority: sync(authority, other, binding),
    )
    assert outcomes["second_blocked"]
    assert not outcomes["first"]["pending_evidence_request_ids"]
    assert not outcomes["second"]["pending_evidence_request_ids"]
    assert _items(store, args["run_id"]) == 1
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 1


def test_matching_content_but_unrelated_chain_receipt_stays_unconfirmed(
    store, tmp_path
):
    app, binding, path, args = setup(store, tmp_path)
    capture(path, args, binding)

    def altered(op, values):
        result = app.invoke(op, values, _context())
        if op == intake.OPERATION:
            result = {
                **result,
                "item": {
                    **result["item"],
                    "chain_seq": 99,
                    "chain_prev_digest": "unrelated-tail",
                },
            }
        return result

    assert (
        sync(app, path, binding, altered)["evidence_attempts"][0]["phase"] == "unknown"
    )
    assert not sync(app, path, binding)["pending_evidence_request_ids"]


def test_owner_replay_from_different_tail_needs_explicit_reconciliation(
    store, tmp_path
):
    app, binding, path, args = setup(store, tmp_path)
    capture(path, args, binding)
    first = app.invoke(
        intake.OPERATION,
        _append_args(
            args["run_id"], "first-" + args["idempotency_key"], item_id="first"
        ),
        _context(),
    )
    accepted = {
        **args,
        "chain_seq": 1,
        "chain_prev_digest": pg.evidence_entry_digest(first["item"]),
    }
    app.invoke(intake.OPERATION, accepted, _context())
    report = sync(app, path, binding)
    assert report["evidence_attempts"][0]["phase"] == "unknown"
    assert len(report["pending_evidence_request_ids"]) == 1
    # Native canonical key replay still succeeds; the carrier has not altered it.
    assert app.invoke(intake.OPERATION, args, _context())["item"]["chain_seq"] == 1


def test_nested_json_receipt_types_must_match_original_request(store, tmp_path):
    app, binding, path, args = setup(store, tmp_path)
    args["provenance"] = {"ordinal": 1}
    capture(path, args, binding)

    def altered(op, values):
        result = app.invoke(op, values, _context())
        if op == intake.OPERATION:
            result = {
                **result,
                "item": {**result["item"], "provenance": {"ordinal": True}},
            }
        return result

    assert (
        sync(app, path, binding, altered)["evidence_attempts"][0]["phase"] == "unknown"
    )
    assert not sync(app, path, binding)["pending_evidence_request_ids"]


def test_correction_keeps_original_position_ahead_of_later_run(store, tmp_path):
    app, binding, path, args = setup(store, tmp_path)
    args["chain_prev_digest"] = "wrong"
    first = capture(path, args, binding)
    other_run = _new_run(app, "later-" + str(uuid.uuid4()))
    other_binding = app.invoke("work.run.resolve-v1", {"run_id": other_run}, _context())
    other_args = _append_args(other_run, "later-key-" + str(uuid.uuid4()))
    second = capture(path, other_args, other_binding)
    assert sync(app, path, binding)["pending_evidence_request_ids"] == [
        first["request_id"],
        second["request_id"],
    ]
    corrected = capture(
        path,
        {**args, "chain_prev_digest": None},
        binding,
        supersedes=first["request_id"],
    )
    assert intake.status(path)["pending_evidence_request_ids"] == [
        corrected["request_id"],
        second["request_id"],
    ]
    result = sync(app, path, binding)
    assert result["confirmed_evidence_request_ids"] == [
        corrected["request_id"],
        second["request_id"],
    ]
    assert not result["pending_evidence_request_ids"]
    assert _items(store, args["run_id"]) == _items(store, other_run) == 1


def test_jsonb_numeric_receipt_normalization_still_confirms(store, tmp_path):
    app, binding, path, args = setup(store, tmp_path)
    args["provenance"] = {"ordinal": 1e2, "fraction": 1.5, "boolean": False}
    capture(path, args, binding)

    def normalized(op, values):
        result = app.invoke(op, values, _context())
        if op == intake.OPERATION:
            result = {
                **result,
                "item": {
                    **result["item"],
                    "provenance": {"ordinal": 100, "fraction": 1.5, "boolean": False},
                },
            }
        return result

    assert not sync(app, path, binding, normalized)["pending_evidence_request_ids"]
    assert _items(store, args["run_id"]) == 1
