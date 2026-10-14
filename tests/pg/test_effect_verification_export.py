"""Actual native append/read histories for the closed V5-compatible subset."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import uuid

import jsonschema
import pytest

from sprintctl import pg, effect_verification
from sprintctl.application import ApplicationRejection
from sprintctl.effect_verification_export import RESULT_SCHEMA_V3
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row
from tests.pg.test_effect_intent import _app, _run, _context, ACCEPTOR
from tests.pg.test_protected_artifact_acceptance import setup, detail, CHECKER

pytestmark = PG_MARKS
OP = "work.effect.export-proposal-v3"


def owner(store):
    return replace(store, connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row))


def export(store, intent, context=ACCEPTOR):
    result = _app(owner(store)).invoke(OP, {"intent_id": intent["intent_id"], "intent_revision": intent["revision"]}, context)
    jsonschema.validate(result, RESULT_SCHEMA_V3)
    return result


def append(store, intent, *, context=CHECKER, mutate=None, claims_mutate=None,
           item_id=None, digest_transform=None):
    release = pg.get_release(store, intent["release_digest"])
    body = detail(intent, release)
    if mutate:
        mutate(body)
    claims = [{"claim_type": "observation", "subject": intent["intent_id"], "grant_id": None,
               "freshness": None, "confirms": None, "detail": body}]
    if claims_mutate:
        claims_mutate(claims)
    digest = effect_verification.receipt_digest(body)
    if digest_transform:
        digest = digest_transform(body)
    run = _run(store, context)
    key = "verification-" + uuid.uuid4().hex
    args = {"run_id": run, "item_id": item_id or key, "kind": effect_verification.KIND,
        "ref": "fixture:verification", "digest": digest, "collector": "fixture:v3",
        "validity": {"basis": "indefinite", "valid_from": "2026-10-13T00:00:00+00:00",
                     "valid_until": None, "component_digests": {}},
        "claims": claims, "provenance": {}, "chain_seq": 0, "chain_prev_digest": None,
        "idempotency_key": key}
    recorded = _app(store).invoke("work.evidence.append-v1", args, context)
    return run, args["item_id"], recorded["item"]


def artifact(name, value):
    destination = os.environ.get("Q4_NATIVE_V3_EXPORT_DIR")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (name + ".json")).write_text(json.dumps(value, sort_keys=True, ensure_ascii=False))


def refused(call, code):
    with pytest.raises(ApplicationRejection) as caught:
        call()
    assert caught.value.code == code


@pytest.mark.parametrize("required,map_present", [(False, False), (True, False), (False, True)])
def test_existing_receipt_association_needs_no_selection_ceremony(store, required, map_present):
    _, _, intent = setup(store, required=required,
        check_revisions={"patch-check": "sha256:" + "d" * 64} if map_present else None)
    if required or map_present:
        refused(lambda: export(store, intent), "proposal-verification-required")
    else:
        empty = export(store, intent)
        assert empty["verification_evidence"] == []
        artifact("optional-empty", empty)
    run, item_id, recorded = append(store, intent)
    result = export(store, intent)
    evidence = result["verification_evidence"]
    assert len(evidence) == 1 and evidence[0]["run"]["run_id"] == run
    assert evidence[0]["item"]["item_id"] == item_id
    assert evidence[0]["entry_digest"] == pg.evidence_entry_digest(recorded)
    assert evidence[0]["run"]["principal_id"] == CHECKER.identity.principal_id
    assert result["intent"]["run_id"] != run
    assert result["release"]["acceptance_contract"]["effect_verification_required"] is required
    assert pg.get_effect_intent(store, intent["intent_id"])["state"] == "proposed"
    artifact("map-only-false" if map_present else "required-true" if required else "optional-one", result)
    single_artifact = os.environ.get("Q4_NATIVE_V3_EXPORT_ARTIFACT")
    if single_artifact and map_present:
        Path(single_artifact).write_text(json.dumps(result, sort_keys=True, ensure_ascii=False))


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("change", ["intent-id", "revision", "digest", "release", "artifact", "failed", "stale-check", "receipt-digest", "claim-type", "claim-grant", "claim-confirms", "second-claim"])
def test_all_associated_candidates_validate_or_entire_bundle_refuses(store, required, change):
    _, _, intent = setup(store, required=required, check_revisions={"patch-check": "sha256:" + "d" * 64})
    append(store, intent)  # A good row cannot hide a later bad candidate.
    def mutate(body):
        if change == "intent-id": body["intent_id"] = "other-intent"
        if change == "revision": body["intent_revision"] += 1
        if change == "digest": body["canonical_intent_digest"] = "0" * 64
        if change == "release": body["release_digest"] = "0" * 64
        if change == "artifact": body["artifact"]["digest"] = "sha256:" + "0" * 64
        if change == "failed": body["checks"][0]["status"] = "failed"
        if change == "stale-check": body["checks"][0]["revision"] = "sha256:" + "0" * 64
    def claims_mutate(claims):
        if change == "claim-type": claims[0]["claim_type"] = "inference"
        if change == "claim-grant": claims[0]["grant_id"] = "claimed-grant"
        if change == "claim-confirms": claims[0]["confirms"] = "claimed-confirmation"
        if change == "second-claim":
            matching = dict(claims[0]); claims[0]["subject"] = "unrelated"
            claims.append(matching)
    append(store, intent, mutate=mutate, claims_mutate=claims_mutate,
           digest_transform=(lambda body: "sha256:" + "0" * 64) if change == "receipt-digest" else None)
    refused(lambda: export(store, intent), "proposal-verification-candidate-invalid")


def test_foreign_workspace_and_other_proposal_are_not_visible_candidates(store):
    _, _, intent = setup(store, required=True)
    foreign = _context("foreign", {"work:evidence", "work:read"}, workspace_id="different")
    append(store, intent, context=foreign, mutate=lambda b: b.update(release_digest="0" * 64))
    _, _, other = setup(store, required=True)
    append(store, other)
    refused(lambda: export(store, intent), "proposal-verification-required")
    append(store, intent)
    assert len(export(store, intent)["verification_evidence"]) == 1


def test_native_unicode_item_id_refuses_without_rewriting_native_hash(store):
    _, _, intent = setup(store, required=True)
    _, _, recorded = append(store, intent, item_id="verification-é")
    effect_verification.validate_receipt(recorded, intent, intent["release_digest"])
    refused(lambda: export(store, intent), "proposal-verification-hash-domain-unsupported")
    assert pg.evidence_entry_digest(recorded) != "sha256:" + hashlib.sha256(json.dumps(
        {k: recorded[k] for k in ("item_id", "digest", "chain_seq", "chain_prev_digest")},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def test_native_unicode_check_name_supported_but_wrong_ascii_escaped_digest_refuses(store):
    _, _, intent = setup(store, required=True)
    append(store, intent, mutate=lambda b: b["checks"][0].update(name="vérification"))
    unicode = export(store, intent)
    assert unicode["verification_evidence"][0]["receipt"]["checks"][0]["name"] == "vérification"
    artifact("unicode-check", unicode)
    append(store, intent, mutate=lambda b: b["checks"][0].update(name="vérification"),
           digest_transform=lambda b: "sha256:" + hashlib.sha256(json.dumps(
               b, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())
    refused(lambda: export(store, intent), "proposal-verification-candidate-invalid")


def test_exact_set_is_deterministic_bounded_and_append_changes_future_export(store):
    _, _, intent = setup(store, required=True)
    for _ in range(29): append(store, intent)
    first = export(store, intent)
    keys = [(e["run"]["run_id"], e["item"]["chain_seq"], e["item"]["item_id"]) for e in first["verification_evidence"]]
    assert keys == sorted(keys) and len(keys) == 29
    assert export(store, intent) == first
    append(store, intent)
    refused(lambda: export(store, intent), "proposal-verification-overflow")
    assert len(first["verification_evidence"]) == 29


@pytest.mark.parametrize("required", [False, True])
def test_missing_native_run_never_silently_omitted_when_fk_bypassed(store, required):
    _, _, intent = setup(store, required=required)
    run, item_id, _ = append(store, intent)
    with store.conn.cursor() as cur:
        cur.execute("ALTER TABLE evidence_item DROP CONSTRAINT evidence_item_repo_id_run_id_fkey")
        cur.execute("UPDATE evidence_item SET run_id=%s WHERE repo_id=%s AND run_id=%s AND item_id=%s",
                    ("run-orphan", store.repo_id, run, item_id))
    store.conn.commit()
    try:
        refused(lambda: export(store, intent), "proposal-verification-candidate-invalid")
    finally:
        with store.conn.cursor() as cur:
            cur.execute("UPDATE evidence_item SET run_id=%s WHERE repo_id=%s AND run_id='run-orphan' AND item_id=%s",
                        (run, store.repo_id, item_id))
            cur.execute("ALTER TABLE evidence_item ADD CONSTRAINT evidence_item_repo_id_run_id_fkey "
                        "FOREIGN KEY(repo_id,run_id) REFERENCES run(repo_id,run_id) ON DELETE CASCADE")
        store.conn.commit()


def test_receipt_append_between_reads_observes_one_readonly_snapshot(store, monkeypatch):
    _, _, intent = setup(store, required=False)
    original = pg.effect_proposal_verification_rows
    writes = []
    def append_during_read(snapshot, intent_id, workspace_id):
        with snapshot.conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()["transaction_read_only"] == "on"
        append(store, intent)
        writes.append(True)
        return original(snapshot, intent_id, workspace_id)
    monkeypatch.setattr(pg, "effect_proposal_verification_rows", append_during_read)
    assert export(store, intent)["verification_evidence"] == []
    assert writes == [True]
    monkeypatch.setattr(pg, "effect_proposal_verification_rows", original)
    assert len(export(store, intent)["verification_evidence"]) == 1


def test_later_native_append_preserves_first_snapshot_and_changes_new_exact_set(store):
    _, _, intent = setup(store, required=True)
    append(store, intent)
    first = export(store, intent)
    artifact("append-first", first)
    append(store, intent)
    second = export(store, intent)
    artifact("append-second", second)
    assert len(first["verification_evidence"]) == 1
    assert len(second["verification_evidence"]) == 2
    assert first["verification_evidence"][0] in second["verification_evidence"]


def test_optional_proposal_still_refuses_malformed_associated_receipt(store):
    _, _, intent = setup(store, required=False)
    append(store, intent)
    append(store, intent, mutate=lambda body: body.update(release_digest="0" * 64))
    refused(lambda: export(store, intent), "proposal-verification-candidate-invalid")


def test_v3_refuses_schema23_even_when_exact_index_was_left_in_place(store):
    _, _, intent = setup(store, required=False)
    with store.conn.cursor() as cur:
        cur.execute("UPDATE schema_version SET version=23")
    store.conn.commit()
    try:
        refused(lambda: export(store, intent), "proposal-verification-index-unavailable")
    finally:
        with store.conn.cursor() as cur:
            cur.execute("UPDATE schema_version SET version=26")
        store.conn.commit()
