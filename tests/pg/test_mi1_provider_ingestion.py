"""Frozen Vuoro provider drafts through the actual native owner and durable carrier.

Provider payloads are synthetic; PostgreSQL, run resolution, idempotency and
carrier receipts are real. No imported decoder, shared database or live grant.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from sprintctl import evidence_intake as intake, pg
from sprintctl.application import ApplicationRejection
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import PG_MARKS
from tests.pg.test_effect_intent import (
    PROPOSER, ACCEPTOR, _app, _run, _items as work_items, _propose, _get,
    _accept, _mark_applied,
)
from tests.pg.test_run_evidence import _items, _ledger_rows, _race

pytestmark = PG_MARKS
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mi1" / "provider-drafts.json"
APPEND_SCHEMA = next(c.input_schema for c in WORK_OPERATION_CONTRACTS if c.name == intake.OPERATION)


def setup(store, case):
    app = _app(store)
    run = _run(store, PROPOSER)
    binding = app.invoke("work.run.resolve-v1", {"run_id": run}, PROPOSER)
    args = deepcopy(json.loads(FIXTURE.read_text())["cases"][case])
    args.update(run_id=run, chain_seq=0, chain_prev_digest=None)
    jsonschema.validate(args, APPEND_SCHEMA)
    return app, binding, args


def capture(path, args, binding):
    return intake.capture(path, (json.dumps(args, indent=2) + "\n").encode(),
                          json.dumps(binding).encode(), repo_id=binding["repo_id"])


def sync(app, path, binding, invoke=None):
    return intake.synchronize(path, repo_id=binding["repo_id"],
        invoke=invoke or (lambda op, args: app.invoke(op, args, PROPOSER)),
        rejection_type=ApplicationRejection)


def test_provider_reply_loss_then_exact_durable_retry_retains_one_observation(store, tmp_path):
    app, binding, args = setup(store, "reply-loss")
    path = tmp_path / "producer.db"
    queued = capture(path, args, binding)
    assert capture(path, args, binding)["duplicate"]
    assert _items(store, args["run_id"]) == 0

    def lost(op, values):
        result = app.invoke(op, values, PROPOSER)
        if op == intake.OPERATION:
            raise OSError("lost committed response")
        return result

    unknown = sync(app, path, binding, lost)
    assert unknown["evidence_attempts"][0]["phase"] == "unknown"
    assert unknown["pending_evidence_request_ids"] == [queued["request_id"]]
    confirmed = sync(app, path, binding)
    assert confirmed["confirmed_evidence_request_ids"] == [queued["request_id"]]
    assert not confirmed["pending_evidence_request_ids"]
    assert _items(store, args["run_id"]) == 1
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 1
    tail = app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER)["item"]
    assert tail["claims"] == args["claims"]
    assert tail["claims"][0]["detail"]["artifact_digest"] == "sha256:" + "b" * 64
    assert sync(app, path, binding)["evidence_attempts"] == []


def test_changed_provider_artifact_under_same_delivery_refuses_and_preserves_first_history(store, tmp_path):
    app, binding, args = setup(store, "conflicting-replay")
    first_path, second_path = tmp_path / "one.db", tmp_path / "two.db"
    capture(first_path, args, binding)
    assert not sync(app, first_path, binding)["pending_evidence_request_ids"]
    before = app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER)
    changed = deepcopy(args)
    observation = changed["claims"][0]["detail"]
    observation["artifact_digest"] = "sha256:" + "c" * 64
    changed["digest"] = "sha256:" + hashlib.sha256(json.dumps(observation,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    queued = capture(second_path, changed, binding)
    report = sync(app, second_path, binding)
    assert report["evidence_attempts"][0]["code"] == "idempotency-conflict"
    assert report["pending_evidence_request_ids"] == [queued["request_id"]]
    assert app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER) == before
    assert _items(store, args["run_id"]) == 1
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 1


def test_provider_success_cannot_accept_or_apply_and_does_not_settle_work(store, tmp_path):
    app, binding, args = setup(store, "authority")
    item_id = work_items(store)[0]
    proposal = _propose(store, item_id, args["run_id"])
    before_item = dict(pg.get_work_item(store, item_id))
    path = tmp_path / "producer.db"
    capture(path, args, binding)
    assert not sync(app, path, binding)["pending_evidence_request_ids"]
    claim = app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER)["item"]["claims"][0]
    assert claim["detail"]["verdict"] == "success"
    assert claim["detail"]["artifact_digest"] != "sha256:" + proposal["canonical_intent_digest"]
    assert claim["confirms"] is None and claim["grant_id"] is None
    for action in (lambda: _accept(store, proposal, context=PROPOSER),
                   lambda: _mark_applied(store, proposal, context=PROPOSER)):
        with pytest.raises(ApplicationRejection) as refusal:
            action()
        assert refusal.value.code == "authority-required" and refusal.value.http_status == 403
    # The read does not import/use the provider integration. Stored evidence
    # remains, while work identity/status and effect authority stay with owners.
    assert _get(store, proposal["intent_id"]) == proposal
    assert dict(pg.get_work_item(store, item_id)) == before_item
    assert _items(store, args["run_id"]) == 1


def test_two_provider_producers_contend_on_real_native_key_without_duplicate_evidence(store, tmp_path, monkeypatch):
    app, binding, args = setup(store, "two-producers")
    first, second = tmp_path / "one.db", tmp_path / "two.db"
    capture(first, args, binding); capture(second, args, binding)
    result = _race(monkeypatch, store, "append_evidence",
                   lambda authority: sync(authority, first, binding),
                   lambda authority: sync(authority, second, binding))
    assert result["second_blocked"]
    assert not result["first"]["pending_evidence_request_ids"]
    assert not result["second"]["pending_evidence_request_ids"]
    assert _items(store, args["run_id"]) == 1
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 1


def test_recorded_provider_hash_cannot_replace_frozen_intent_acceptance_binding(store, tmp_path):
    app, binding, args = setup(store, "trusted-binding")
    item_id = work_items(store)[0]
    proposal = _propose(store, item_id, args["run_id"])
    before_work = dict(pg.get_work_item(store, item_id))
    path = tmp_path / "producer.db"
    capture(path, args, binding)
    assert not sync(app, path, binding)["pending_evidence_request_ids"]
    before_tail = app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER)
    claim = before_tail["item"]["claims"][0]
    provider_hash = claim["detail"]["artifact_digest"].removeprefix("sha256:")
    patch_hash = hashlib.sha256(proposal["unified_diff"].encode("utf-8")).hexdigest()
    # Three domains: provider-reported artifact, observed patch bytes, and the
    # owner's canonical digest of the entire frozen intent. Never equate them.
    assert provider_hash != patch_hash
    assert provider_hash != proposal["canonical_intent_digest"]
    assert claim["detail"]["verdict"] == "success" and claim["confirms"] is None
    for candidate in (provider_hash, before_tail["item"]["digest"].removeprefix("sha256:")):
        with pytest.raises(ApplicationRejection) as refusal:
            _accept(store, proposal, context=ACCEPTOR, canonical_intent_digest=candidate)
        assert (refusal.value.code, refusal.value.http_status) == ("effect-digest-mismatch", 409)
        assert _get(store, proposal["intent_id"]) == proposal
        assert dict(pg.get_work_item(store, item_id)) == before_work
        assert app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER) == before_tail
    # Positive control: this is the trusted digest check, not public role denial.
    # The trust-side actor can independently approve the actual frozen intent;
    # this does not assert that the provider verdict verifies its patch.
    accepted = _accept(store, proposal, context=ACCEPTOR)
    assert accepted["acceptance"]["canonical_intent_digest"] == proposal["canonical_intent_digest"]
    assert accepted["acceptance"]["acceptor_principal"] == ACCEPTOR.identity.principal_id
    assert app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER) == before_tail
    assert dict(pg.get_work_item(store, item_id)) == before_work


def test_provider_decoder_unavailability_does_not_move_native_identity_history_or_authority(store, tmp_path, monkeypatch):
    import builtins
    import sys

    app, binding, args = setup(store, "decoder-removal")
    item_id = work_items(store)[0]
    proposal = _propose(store, item_id, args["run_id"])
    before_work = dict(pg.get_work_item(store, item_id))
    path = tmp_path / "producer.db"
    queued = capture(path, args, binding)
    assert not sync(app, path, binding)["pending_evidence_request_ids"]
    before_tail = app.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER)
    assert not any(name.startswith("vuoro_evidence.ingress") for name in sys.modules)
    original_import = builtins.__import__

    def without_provider(name, *positional, **keyword):
        if name.startswith("vuoro_evidence"):
            raise ModuleNotFoundError("provider decoder deliberately unavailable")
        return original_import(name, *positional, **keyword)

    monkeypatch.setattr(builtins, "__import__", without_provider)
    with pytest.raises(ModuleNotFoundError, match="deliberately unavailable"):
        __import__("vuoro_evidence.ingress.provider")
    restarted = _app(store)
    assert restarted.invoke("work.run.resolve-v1", {"run_id": args["run_id"]}, PROPOSER) == binding
    assert restarted.invoke("work.evidence.tail-v1", {"run_id": args["run_id"]}, PROPOSER) == before_tail
    assert _get(store, proposal["intent_id"]) == proposal
    assert dict(pg.get_work_item(store, item_id)) == before_work
    assert capture(path, args, binding)["request_id"] == queued["request_id"]
    assert sync(restarted, path, binding)["evidence_attempts"] == []
    assert _ledger_rows(store, "append_evidence", args["idempotency_key"]) == 1
    with pytest.raises(ApplicationRejection) as refusal:
        _accept(store, proposal, context=PROPOSER)
    assert (refusal.value.code, refusal.value.http_status) == ("authority-required", 403)
    assert _get(store, proposal["intent_id"]) == proposal
