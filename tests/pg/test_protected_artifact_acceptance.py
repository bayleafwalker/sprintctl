"""Actual-owner oracle for protected raw artifact / intent / Release acceptance.

Receipts are synthetic protected assertions, not execution attestations. Every
transition uses native APIs on disposable PG; no shared backend fault injection.
"""
import hashlib
import json
import uuid

import pytest
from psycopg.errors import CheckViolation

from sprintctl import pg
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS
from tests.pg.test_effect_intent import PROPOSER, ACCEPTOR, SECOND_ACCEPTOR, _context, _app, _run, _items, _propose, _get, _accept
from tests.pg.test_releases import _edit
from tests.pg.test_run_evidence import _race

pytestmark = PG_MARKS
CHECKER = _context(ACCEPTOR.identity.principal_id, set(ACCEPTOR.identity.authorities) | {"work:evidence"})
SECOND_CHECKER = _context(SECOND_ACCEPTOR.identity.principal_id, set(SECOND_ACCEPTOR.identity.authorities) | {"work:evidence"})


def setup(store, *, required=True):
    item = _items(store)[0]
    reservation = pg.reserve(store, item, actor="operator", session_id=uuid.uuid4().hex,
        role="execution", acceptance_contract={"effect_verification_required": required})
    intent = _propose(store, item, _run(store, PROPOSER))
    return item, reservation, intent


def detail(intent, release):
    return {"schema": "sprintctl-protected-artifact-verification/v1",
        "intent_id": intent["intent_id"], "intent_revision": intent["revision"],
        "canonical_intent_digest": intent["canonical_intent_digest"],
        "release_digest": release["release_digest"],
        "artifact": {"domain": "utf8-unified-diff/v1", "digest": "sha256:" +
            hashlib.sha256(intent["unified_diff"].encode("utf-8")).hexdigest()},
        "checks": [{"name": "patch-check", "revision": "sha256:" + "d" * 64, "status": "passed"}]}


def receipt(store, intent, release, *, context=CHECKER, mutate=None, kind="protected-artifact-verification", bad_digest=False):
    run = _run(store, context)
    body = detail(intent, release)
    if mutate:
        mutate(body)
    digest = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    item_id = "verification-" + uuid.uuid4().hex
    args = {"run_id": run, "item_id": item_id, "kind": kind,
        "ref": "fixture:protected-check-result", "digest": "sha256:" + "0" * 64 if bad_digest else digest,
        "collector": "fixture-protected-verifier/v1", "validity": {"basis": "indefinite",
            "valid_from": "2026-10-06T00:00:00+00:00", "valid_until": None, "component_digests": {}},
        "claims": [{"claim_type": "observation", "subject": intent["intent_id"], "grant_id": None,
                    "freshness": None, "confirms": None, "detail": body}],
        "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
        "idempotency_key": item_id}
    recorded = _app(store).invoke("work.evidence.append-v1", args, context)
    return {"run_id": run, "item_id": item_id}, recorded, body


def accept(app, intent, ref=None, context=CHECKER):
    args = {k: intent[k] for k in ("intent_id", "revision", "canonical_intent_digest")}
    if ref is not None:
        args["verification_ref"] = ref
    return app.invoke("work.effect.accept-v1", args, context)["intent"]


def test_required_receipt_cannot_be_omitted(store):
    item, reservation, intent = setup(store)
    before = pg.get_work_item(store, item)
    with pytest.raises(ApplicationRejection) as error:
        _accept(store, intent)
    assert error.value.code == "effect-verification-required"
    assert _get(store, intent["intent_id"]) == intent
    assert pg.get_work_item(store, item) == before


def test_exact_raw_artifact_protected_receipt_is_preserved_in_acceptance_without_settlement(store):
    item, release, intent = setup(store)
    assert intent["release_digest"] == release["release_digest"]
    before = pg.get_work_item(store, item)
    ref, recorded, body = receipt(store, intent, release)
    accepted = accept(_app(store), intent, ref)
    proof = accepted["acceptance"]["verification"]
    assert proof["receipt"] == body
    assert proof["evidence_digest"] == recorded["item"]["digest"]
    assert proof["verifier_principal"] == CHECKER.identity.principal_id
    assert proof["run_id"] == ref["run_id"] and proof["item_id"] == ref["item_id"]
    assert body["artifact"]["digest"] != "sha256:" + intent["canonical_intent_digest"]
    assert _get(store, intent["intent_id"]) == accepted
    assert pg.get_work_item(store, item) == before


@pytest.mark.parametrize("change", ["raw-artifact", "intent-id", "intent-revision", "canonical-digest", "release", "failed-check", "unknown-check-revision", "receipt-digest", "provider-kind"])
def test_recorded_success_with_wrong_binding_cannot_satisfy_required_acceptance(store, change):
    item, release, intent = setup(store)
    def mutate(body):
        if change == "raw-artifact": body["artifact"]["digest"] = "sha256:" + "b" * 64
        if change == "intent-id": body["intent_id"] = "intent_" + "0" * 26
        if change == "intent-revision": body["intent_revision"] += 1
        if change == "canonical-digest": body["canonical_intent_digest"] = "c" * 64
        if change == "release": body["release_digest"] = "f" * 64
        if change == "failed-check": body["checks"][0]["status"] = "failed"
        if change == "unknown-check-revision": body["checks"][0]["revision"] = "unknown"
    ref, recorded, _ = receipt(store, intent, release, mutate=mutate,
        bad_digest=change == "receipt-digest", kind="provider-observation" if change == "provider-kind" else "protected-artifact-verification")
    before = pg.get_work_item(store, item)
    with pytest.raises(ApplicationRejection) as error:
        accept(_app(store), intent, ref)
    assert error.value.code == "effect-verification-refused"
    assert _get(store, intent["intent_id"]) == intent
    assert pg.get_work_item(store, item) == before
    assert _app(store).invoke("work.evidence.tail-v1", {"run_id": ref["run_id"]}, CHECKER)["item"] == recorded["item"]


def test_public_writer_receipt_cannot_be_consumed_as_protected_identity(store):
    item, release, intent = setup(store)
    ref, _, _ = receipt(store, intent, release, context=PROPOSER)
    with pytest.raises(ApplicationRejection) as error:
        accept(_app(store), intent, ref)
    assert error.value.code == "effect-verification-refused"
    assert _get(store, intent["intent_id"]) == intent


def test_release_revision_moves_after_receipt_refuses_and_cannot_downgrade_frozen_requirement(store):
    item, release, intent = setup(store)
    ref, _, _ = receipt(store, intent, release)
    _edit(pg, store, item, "Changed released requirement")
    pg.release_reservation(store, release["id"])
    pg.reserve(store, item, actor="operator", session_id=uuid.uuid4().hex, role="execution",
               acceptance_contract={"effect_verification_required": False})
    for verification_ref in (ref, None):
        with pytest.raises(ApplicationRejection) as error:
            accept(_app(store), intent, verification_ref)
        assert error.value.code == "effect-release-mismatch"
    assert _get(store, intent["intent_id"]) == intent


def test_legacy_unreleased_proposal_keeps_digest_and_has_no_invented_verification(store):
    item = _items(store)[0]
    intent = _propose(store, item, _run(store, PROPOSER))
    accepted = _accept(store, intent)
    assert "release_digest" not in accepted
    assert "verification" not in accepted["acceptance"]
    assert accepted["canonical_intent_digest"] == intent["canonical_intent_digest"]


def test_verified_acceptance_metadata_is_immutable_even_during_application_transition(store):
    _, release, intent = setup(store)
    ref, _, _ = receipt(store, intent, release)
    accepted = accept(_app(store), intent, ref)
    with pytest.raises(CheckViolation):
        with store.conn.transaction(), store.conn.cursor() as cur:
            cur.execute("UPDATE work_effect_intent SET state='applied', applied_at=clock_timestamp(), applier_principal='fixture', applied_commit_sha=repeat('a',40), applied_pr_url='https://example.test/pr/1', verification_binding='{}'::jsonb WHERE repo_id=%s AND intent_id=%s",
                        (store.repo_id, intent["intent_id"]))
    assert _get(store, intent["intent_id"]) == accepted


def test_two_verified_acceptors_serialize_on_actual_owner_commit_boundary(store, monkeypatch):
    _, release, intent = setup(store)
    first, _, _ = receipt(store, intent, release)
    second, _, _ = receipt(store, intent, release, context=SECOND_CHECKER)
    outcome = _race(monkeypatch, store, "_effect_verification_locked",
                    lambda app: accept(app, intent, first),
                    lambda app: accept(app, intent, second, SECOND_CHECKER))
    assert outcome["second_blocked"]
    assert outcome["first"]["acceptance"]["verification"]["item_id"] == first["item_id"]
    assert outcome["second"].code == "effect-invalid-transition"


def test_application_keeps_proof_and_refuses_changed_work_release(store):
    item, release, intent = setup(store)
    ref, _, _ = receipt(store, intent, release)
    accepted = accept(_app(store), intent, ref)
    _edit(pg, store, item, "New requirement after approval")
    with pytest.raises(pg.EffectRefused) as error:
        pg.mark_effect_intent_applied(store, intent["intent_id"], revision=intent["revision"],
            canonical_intent_digest=intent["canonical_intent_digest"], applier_principal="fixture",
            commit_sha="a" * 40, pr_url="https://example.test/pr/1")
    assert error.value.code == "effect-release-mismatch"
    assert _get(store, intent["intent_id"]) == accepted


def test_application_preserves_exact_accepted_verification(store):
    _, release, intent = setup(store)
    ref, _, _ = receipt(store, intent, release)
    accepted = accept(_app(store), intent, ref)
    applied = pg.mark_effect_intent_applied(store, intent["intent_id"], revision=intent["revision"],
        canonical_intent_digest=intent["canonical_intent_digest"], applier_principal="fixture",
        commit_sha="a" * 40, pr_url="https://example.test/pr/1")
    assert applied["state"] == "applied"
    assert applied["acceptance"] == accepted["acceptance"]


@pytest.mark.parametrize("changed_binding", ["workspace_id", "client_id", "grant_id"])
def test_same_principal_receipt_from_other_native_binding_is_refused(store, changed_binding):
    _, release, intent = setup(store)
    other = _context(CHECKER.identity.principal_id, set(CHECKER.identity.authorities))
    setattr(other.identity, changed_binding, "other-binding")
    ref, _, _ = receipt(store, intent, release, context=other)
    with pytest.raises(ApplicationRejection) as error:
        accept(_app(store), intent, ref)
    assert error.value.code == "effect-verification-refused"
    assert _get(store, intent["intent_id"]) == intent


def test_work_edit_waits_for_acceptance_commit_then_invalidates_application(store, monkeypatch):
    item, release, intent = setup(store)
    ref, _, _ = receipt(store, intent, release)
    outcome = _race(monkeypatch, store, "_effect_verification_locked",
        lambda app: accept(app, intent, ref),
        lambda app: _edit(pg, app.store, item, "Requirement changed after commit"))
    assert outcome["second_blocked"]
    assert outcome["first"]["state"] == "accepted"
    assert not isinstance(outcome["second"], BaseException)
    with pytest.raises(pg.EffectRefused) as error:
        pg.mark_effect_intent_applied(store, intent["intent_id"], revision=intent["revision"],
            canonical_intent_digest=intent["canonical_intent_digest"], applier_principal="fixture",
            commit_sha="a" * 40, pr_url="https://example.test/pr/1")
    assert error.value.code == "effect-release-mismatch"
