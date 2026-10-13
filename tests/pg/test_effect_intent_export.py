"""Actual disposable PG proof for proposal-selected readonly Q4 export."""
from dataclasses import replace

import json
import os
from pathlib import Path

import jsonschema
import pytest

from sprintctl import effect_intent_export, pg
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row
from tests.pg.test_effect_intent import (
    _app, _context, _get, _items, _propose, _run, PROPOSER, ACCEPTOR,
)

pytestmark = PG_MARKS
OP = "work.effect.export-proposal-v2"


def _fixture(store, contract=None):
    item_id = _items(store)[0]
    pg.reserve(store, item_id, actor="operator", session_id="fixture", role="execution",
               acceptance_contract=contract)
    run_id = _run(store, PROPOSER)
    intent = _propose(store, item_id, run_id)
    # Native owner snapshots need an explicit full DSN connection factory;
    # conn.info.dsn is redacted by psycopg for password-bearing URLs.
    return replace(store, connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row)), intent


def _export(store, intent, context=ACCEPTOR):
    return _app(store).invoke(OP, {"intent_id": intent["intent_id"],
                                  "intent_revision": intent["revision"]}, context)


def _refused(call, code):
    with pytest.raises(ApplicationRejection) as caught:
        call()
    assert caught.value.code == code


def test_q4_export_exact_fields_cross_principal_same_namespace_no_effect(store):
    owner, intent = _fixture(store)
    before = _get(store, intent["intent_id"])
    result = _export(owner, intent)
    jsonschema.validate(result, effect_intent_export.RESULT_SCHEMA)
    assert result["run"]["principal_id"] == PROPOSER.identity.principal_id
    assert result["run"]["principal_id"] != ACCEPTOR.identity.principal_id
    assert result["intent"]["canonical_intent_digest"] == intent["canonical_intent_digest"]
    assert result["release"]["acceptance_contract"] == {"review_required": True}
    assert result["release"]["item_revision"].rsplit("@revise:", 1)[0] == result["work"]["edit_revision"]
    assert _get(store, intent["intent_id"]) == before
    assert set(result["run"]) == {"run_id", "principal_id", "workspace_id", "client_id", "grant_id"}
    assert "acceptance" not in result["intent"]
    # Explicit opt-in synthetic owner artifact for cross-owner integration.
    artifact = os.environ.get("Q4_NATIVE_EXPORT_ARTIFACT")
    if artifact:
        Path(artifact).write_text(json.dumps(result, sort_keys=True, ensure_ascii=False))


@pytest.mark.parametrize("contract", [
    {"review_required": True, "effect_verification_required": True},
    {"review_required": True, "effect_verification_required": False,
     "effect_check_revisions": {"test": "sha256:" + "a" * 64}},
    {"effect_check_revisions": {"test": "sha256:" + "a" * 64}},
])
def test_q4_required_verification_is_explicitly_unavailable(store, contract):
    owner, intent = _fixture(store, contract)
    _refused(lambda: _export(owner, intent), "proposal-verification-selection-unavailable")


def test_q4_wrong_workspace_and_repo_refuse_without_foreign_run_access(store):
    owner, intent = _fixture(store)
    wrong = _context("other-reader", {"work.effect.get", "work:read"}, workspace_id="foreign")
    _refused(lambda: _export(owner, intent, wrong), "proposal-export-unavailable")
    foreign = _context("reader", {"work.effect.get", "work:read"})
    foreign.repo_id = "itest-nonexistent-q4"
    _refused(lambda: _export(owner, intent, foreign), "proposal-export-unavailable")


def test_q4_stale_revision_and_changed_description_refuse(store):
    owner, intent = _fixture(store)
    _refused(lambda: _export(owner, {**intent, "revision": intent["revision"] + 1}), "proposal-export-stale")
    pg.update_work_item_description(store, intent["item_id"], "Changed after proposal", actor="editor")
    _refused(lambda: _export(owner, intent), "proposal-export-release-stale")


def test_q4_revise_count_change_refuses_even_when_description_unchanged(store):
    owner, intent = _fixture(store)
    old_edit = _export(owner, intent)["work"]["edit_revision"]
    pg.record_decision(store, intent["item_id"], "revise", actor="operator", rationale="Revise")
    pg.reserve(store, intent["item_id"], actor="operator", session_id="new", role="execution")
    assert pg.get_work_item_with_edit_revision(store, intent["item_id"])[1] == old_edit
    _refused(lambda: _export(owner, intent), "proposal-export-release-stale")


def test_q4_unsupported_contract_fields_never_silently_dropped(store):
    owner, intent = _fixture(store, {"review_required": True, "unknown_private": "native"})
    _refused(lambda: _export(owner, intent), "proposal-export-unsupported-contract")


def test_q4_snapshot_is_readonly_and_coherent_under_concurrent_edit(store, monkeypatch):
    owner, intent = _fixture(store)
    expected = _export(owner, intent)
    original = pg.get_effect_intent
    observations = []

    def read_and_edit(snapshot, intent_id):
        result = original(snapshot, intent_id)
        with snapshot.conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert next(iter(cur.fetchone().values())) == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with snapshot.conn.transaction():
                with snapshot.conn.cursor() as cur:
                    cur.execute("UPDATE work_item SET title=title WHERE repo_id=%s AND id=%s",
                                (snapshot.repo_id, intent["item_id"]))
        pg.update_work_item_description(store, intent["item_id"], "Concurrent new description", actor="editor")
        observations.append(True)
        return result

    monkeypatch.setattr(pg, "get_effect_intent", read_and_edit)
    assert _export(owner, intent) == expected
    assert observations == [True]
    monkeypatch.setattr(pg, "get_effect_intent", original)
    _refused(lambda: _export(owner, intent), "proposal-export-release-stale")


def test_q4_oversized_multibyte_native_output_refuses_without_truncation(store):
    item_id = _items(store)[0]
    pg.reserve(store, item_id, actor="operator", session_id="large", role="execution")
    run_id = _run(store, PROPOSER)
    intent = _propose(store, item_id, run_id, unified_diff="漢" * 900_000)
    owner = replace(store, connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row))
    _refused(lambda: _export(owner, intent), "proposal-export-size-bound")
