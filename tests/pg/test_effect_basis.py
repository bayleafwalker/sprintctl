"""Actual readonly native currentness DATA; never protected approval authority."""
from dataclasses import replace
import json
import os
from pathlib import Path

import jsonschema
import pytest
from sprintctl import effect_basis, pg
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row
from tests.pg.test_effect_intent import _app, _context, _accept, _reject, _mark_applied, ACCEPTOR
from tests.pg.test_protected_artifact_acceptance import setup
from tests.pg.test_effect_verification_export import append

pytestmark = PG_MARKS
OP = "work.effect.basis-v1"


def read(store, intent, context=ACCEPTOR):
    owner = replace(store, connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row))
    result = _app(owner).invoke(OP, {"intent_id": intent["intent_id"], "intent_revision": intent["revision"]}, context)
    jsonschema.validate(result, effect_basis.RESULT_SCHEMA)
    return result


def refuse(call, code):
    with pytest.raises(ApplicationRejection) as caught:
        call()
    assert caught.value.code == code


@pytest.mark.parametrize("state", ["proposed", "accepted", "rejected", "applied"])
def test_known_public_intent_states_are_only_closed_data(store, state):
    _, _, intent = setup(store, required=False)
    original = read(store, intent)
    if state in ("accepted", "applied"):
        _accept(store, intent)
    if state == "applied":
        _mark_applied(store, intent)
    if state == "rejected":
        _reject(store, intent)
    result = read(store, intent)
    assert result["intent"]["state"] == state
    expected = {**original, "intent": {**original["intent"], "state": state}}
    assert result == expected
    assert set(result) == {"schema", "repo_id", "workspace_id", "intent", "run", "work", "release"}
    assert set(result["intent"]) == {"intent_id", "revision", "state", "canonical_intent_digest", "release_digest", "item_id", "repository", "base_commit", "title", "rationale", "unified_diff", "run_id"}
    path = os.environ.get("Q4_NATIVE_BASIS_EXPORT_DIR")
    if path:
        directory = Path(path); directory.mkdir(parents=True, exist_ok=True)
        (directory / (state + ".json")).write_text(json.dumps(result, ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize("status", ["pending", "active", "blocked", "done"])
def test_actual_work_status_is_explicit_not_inferred_from_revision(store, status):
    item, _, intent = setup(store, required=False)
    # Native status API records a terminal decision for done, preserving guards.
    if status != "pending":
        pg.set_work_item_status(store, item, "active", actor="operator")
    if status in ("blocked", "done"):
        pg.set_work_item_status(store, item, status, actor="operator")
    result = read(store, intent)
    assert result["work"]["status"] == status
    assert result["work"]["status_revision"] == pg.item_status_revision(pg.get_work_item(store, item))


def test_readonly_authorized_namespace_does_not_require_writer_or_current_run_owner(store):
    _, _, intent = setup(store, required=False)
    reader = _context("separate-reader", {"work:read", "work.effect.get"})
    assert read(store, intent, reader)["run"]["principal_id"] != reader.identity.principal_id
    foreign = _context("foreign", {"work:read", "work.effect.get"}, workspace_id="foreign")
    refuse(lambda: read(store, intent, foreign), "effect-basis-unavailable")
    for authorities in ({"work:read"}, {"work.effect.get"}):
        refuse(lambda: read(store, intent, _context("reader", authorities)), "authority-required")
    refuse(lambda: read(store, {**intent, "revision": intent["revision"] + 1}), "effect-basis-stale")
    for key in ("namespace", "repo_id", "expected_hash", "status", "verification_ref"):
        with pytest.raises(ApplicationRejection) as caught:
            _app(store).invoke(OP, {"intent_id": intent["intent_id"], "intent_revision":1, key:"forged"}, reader)
        assert caught.value.code == "invalid-arguments"
    with pytest.raises(ApplicationRejection) as caught:
        _app(store).invoke(OP, {"intent_id":intent["intent_id"], "intent_revision":True}, reader)
    assert caught.value.code == "invalid-arguments"


def test_description_edit_keeps_old_valid_selected_release_data(store):
    item, _, intent = setup(store, required=False)
    old = read(store, intent)
    pg.update_work_item_description(store, item, "Edited without new release", actor="editor")
    result = read(store, intent)
    assert result["release"] == old["release"]
    assert result["work"]["item_revision"] != old["work"]["item_revision"]
    assert result["work"]["edit_revision"] != old["work"]["edit_revision"]
    assert result["intent"] == old["intent"]
    # A frozen-copy consumer compares BOTH bases; retaining the old Release
    # does not hide current description drift or authorize new content.
    assert result["work"]["item_revision"] != result["release"]["item_revision"]


def test_revise_without_release_is_null_then_new_release_is_actual_data(store):
    item, _, intent = setup(store, required=False)
    original = read(store, intent)
    pg.record_decision(store, item, "revise", actor="operator", rationale="Revise")
    revised = read(store, intent)
    assert revised["release"] is None
    assert revised["work"]["edit_revision"] == original["work"]["edit_revision"]
    assert revised["work"]["item_revision"] != original["work"]["item_revision"]
    pg.reserve(store, item, actor="operator", session_id="basis-new", role="execution")
    new = read(store, intent)
    assert new["release"]["item_revision"] == new["work"]["item_revision"]
    assert new["release"]["release_digest"] != original["release"]["release_digest"]
    assert new["intent"] == original["intent"]


def test_snapshot_is_readonly_and_atomic_under_status_description_revise_release_changes(store, monkeypatch):
    item, _, intent = setup(store, required=False)
    original = read(store, intent)
    getter = pg.get_effect_intent
    def interleave(snapshot, intent_id):
        result = getter(snapshot, intent_id)
        with snapshot.conn.cursor() as cur:
            cur.execute("SHOW transaction_isolation"); assert cur.fetchone()["transaction_isolation"] == "repeatable read"
            cur.execute("SHOW transaction_read_only"); assert cur.fetchone()["transaction_read_only"] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with snapshot.conn.transaction(), snapshot.conn.cursor() as cur:
                cur.execute("UPDATE work_item SET status='blocked' WHERE repo_id=%s AND id=%s", (store.repo_id, item))
        pg.set_work_item_status(store, item, "active", actor="editor")
        pg.update_work_item_description(store, item, "Concurrent edit", actor="editor")
        pg.record_decision(store, item, "revise", actor="operator", rationale="Concurrent revise")
        pg.reserve(store, item, actor="operator", session_id="basis-concurrent", role="execution")
        pg.set_work_item_status(store, item, "blocked", actor="editor")
        return result
    monkeypatch.setattr(pg, "get_effect_intent", interleave)
    assert read(store, intent) == original
    monkeypatch.setattr(pg, "get_effect_intent", getter)
    successor = read(store, intent)
    assert successor["work"]["status"] == "blocked"
    assert successor["work"]["item_revision"] != original["work"]["item_revision"]
    assert successor["release"] != original["release"]


@pytest.mark.parametrize("required,map_present", [(True,False), (False,True), (False,False)])
def test_current_receipts_never_replace_or_poison_frozen_copy_currentness(store, monkeypatch, required, map_present):
    _, _, intent = setup(store, required=required,
                          check_revisions={"patch-check":"sha256:"+"d"*64} if map_present else None)
    def forbidden(*args, **kwargs):
        raise AssertionError("basis must never query current evidence")
    monkeypatch.setattr(pg, "effect_proposal_verification_rows", forbidden)
    before = read(store, intent)  # No required-evidence count gate on currentness.
    append(store, intent)
    assert read(store, intent) == before
    append(store, intent, mutate=lambda body: body.update(release_digest="0"*64))
    assert read(store, intent) == before
    with store.conn.cursor() as cur:
        cur.execute("DELETE FROM evidence_item WHERE repo_id=%s", (store.repo_id,))
    store.conn.commit()
    assert read(store, intent) == before


@pytest.mark.parametrize("part", ["intent", "release", "work"])
def test_corrupt_source_data_refuses_without_replacing_frozen_content(store, monkeypatch, part):
    _, _, intent = setup(store, required=False)
    if part == "intent":
        original = pg.get_effect_intent
        monkeypatch.setattr(pg, "get_effect_intent", lambda *args: {**original(*args), "canonical_intent_digest":"0"*64})
    else:
        original = pg.effect_proposal_release_basis
        def corrupt(*args):
            item, revision, release = original(*args)
            if part == "release": release = {**release, "release_digest":"0"*64}
            else: item = {**item, "status":"unknown"}
            return item, revision, release
        monkeypatch.setattr(pg, "effect_proposal_release_basis", corrupt)
    refuse(lambda: read(store, intent), "effect-basis-invalid")


def test_wrong_repository_missing_source_run_and_wrong_stored_principal_refuse(store, monkeypatch):
    _, _, intent = setup(store, required=False)
    refuse(lambda: read(replace(store, repo_id="other-repository"), intent), "effect-basis-unavailable")
    getter = pg.get_run
    monkeypatch.setattr(pg, "get_run", lambda *args: None)
    refuse(lambda: read(store, intent), "effect-basis-unavailable")
    monkeypatch.setattr(pg, "get_run", lambda *args: {**getter(*args), "principal_id":"wrong-native-owner"})
    refuse(lambda: read(store, intent), "effect-basis-unavailable")


def test_native_unicode_diff_above_canonical_budget_refuses_without_truncation(store):
    from tests.pg.test_effect_intent import _propose
    item, _, original = setup(store, required=False)
    oversized = _propose(store, item, original["run_id"], unified_diff="🧪" * 600_000)
    refuse(lambda: read(store, oversized), "effect-basis-size-bound")


def test_exact_utf8_200_character_locator_routes_without_old_ascii_128_gate(store):
    _, _, intent = setup(store, required=False)
    refuse(lambda: read(store, {**intent, "intent_id":"é"*200}), "effect-basis-unavailable")
    refuse(lambda: read(store, {**intent, "intent_id":"é"*201}), "invalid-arguments")
