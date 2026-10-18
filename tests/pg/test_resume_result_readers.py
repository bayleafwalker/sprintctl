"""Bounded actual-owner to fresh-process qualification, not a human recovery trial.

Every reader gets only one serialized owner response. It has no predecessor
conversation, workspace, database URL, or credentials. Protected receipts below
are synthetic fixture assertions, never execution or target-effect attestations.
"""

import json
import os
import subprocess
import sys
import uuid

import pytest

from sprintctl import effect_verification, pg
from tests.pg._shared import PG_MARKS, _PG_URL, dict_row, psycopg
from tests.pg.test_effect_intent import _app, _items, _propose
from tests.pg.test_protected_artifact_acceptance import CHECKER, accept, receipt
from tests.pg.test_releases import _edit
from tests.pg.test_run_continuation import PREDECESSOR, SUCCESSOR, _context, _run

pytestmark = PG_MARKS


@pytest.fixture(autouse=True)
def snapshot_factory(store):
    store.connection_factory = lambda: psycopg.connect(_PG_URL, row_factory=dict_row)


def _fresh_reader(response, *, kind, cwd):
    module, renderer = ({"resume": ("briefing", "render_run_brief"),
                         "effect": ("effect_explanation", "render_effect_explanation")}[kind])
    script = (f"import json,sys; from sprintctl.{module} import {renderer}; "
              f"print({renderer}(json.load(sys.stdin)))")
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps(response),
        capture_output=True, text=True, cwd=cwd, timeout=30,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("change", ["release", "decision", "no-baseline"])
def test_fresh_process_identifies_owner_changes_and_correct_unknowns(store, tmp_path, change):
    app = _app(store)
    predecessor_context = _context(**PREDECESSOR)
    successor_context = _context(**SUCCESSOR)
    predecessor = _run(app, context=predecessor_context)
    successor = _run(app, context=successor_context, predecessor_run_id=predecessor["run_id"])
    item_id = _items(store)[0]
    first_release = pg.reserve(store, item_id, actor="predecessor", session_id=uuid.uuid4().hex,
                               role="execution")["release_digest"]
    baseline = app.invoke("work.run.continuation-delta-v1", {
        "run_id": predecessor["run_id"], "item_id": item_id}, predecessor_context)
    if change == "release":
        _edit(pg, store, item_id, "Use the replacement artifact; verify its exact digest.")
        replacement = pg.reserve(store, item_id, actor="successor", session_id=uuid.uuid4().hex,
                                 role="execution")["release_digest"]
        assert replacement != first_release
    elif change == "decision":
        decision = pg.record_decision(store, item_id, "revise", actor="reviewer")
    arguments = {"run_id": successor["run_id"], "item_id": item_id}
    if change != "no-baseline":
        arguments["checkpoint"] = baseline["delta"]["current"]
    response = app.invoke("work.run.continuation-delta-v1", arguments, successor_context)
    text = _fresh_reader(response, kind="resume", cwd=tmp_path)
    assert response["delta"]["predecessor_run_id"] == predecessor["run_id"]
    assert response["delta"]["authorization_scope"]["run_id"] == successor["run_id"]
    if change == "release":
        assert response["delta"]["comparison"]["current_release_digest"] == "changed"
        assert "CHANGED: Work definition, Release" in text
        assert replacement in text
    elif change == "decision":
        assert "CHANGED: Decision records" in text
        assert response["delta"]["latest_decision"]["id"] == decision["id"]
        assert '\"kind\": \"revise\"' in text
    else:
        assert set(response["delta"]["comparison"].values()) == {"unknown"}
        assert "CHANGED: (none)" in text and "UNCHANGED: (none)" in text
    assert "Source bytes: unknown" in text
    assert "External content availability: unknown" in text
    assert "grants no authority" in text


@pytest.mark.parametrize("case", ["matching", "changed-artifact", "omitted-receipt"])
def test_independent_reader_exposes_missing_or_changed_effect_evidence(store, tmp_path, case):
    app = _app(store)
    item_id = _items(store)[0]
    reserved = pg.reserve(store, item_id, actor="operator", session_id=uuid.uuid4().hex,
        role="execution", acceptance_contract={"effect_verification_required": True,
            "effect_check_revisions": {"patch-check": "sha256:" + "d" * 64}})
    from tests.pg.test_effect_intent import PROPOSER, _run as register_run
    intent = _propose(store, item_id, register_run(store, PROPOSER))
    ref, _, _ = receipt(store, intent, pg.get_release(store, reserved["release_digest"]))
    accepted = accept(app, intent, ref)
    assert accepted["state"] == "accepted"
    before = pg.get_effect_intent(store, intent["intent_id"])
    arguments = {"intent_id": intent["intent_id"], "revision": intent["revision"],
        "canonical_intent_digest": intent["canonical_intent_digest"],
        "artifact_sha256": effect_verification.artifact_digest(intent["unified_diff"])}
    if case == "changed-artifact":
        arguments["artifact_sha256"] = "sha256:" + "0" * 64
    if case != "omitted-receipt":
        arguments["verification_ref"] = ref
    response = app.invoke("work.effect.acceptance-snapshot-v1", arguments, CHECKER)
    text = _fresh_reader(response, kind="effect", cwd=tmp_path)
    if case == "changed-artifact":
        assert "REQUESTED ARTIFACT: MISMATCH" in text
        assert "artifact-check comparison: matches" in text
    elif case == "omitted-receipt":
        assert "Receipt: not-supplied; artifact-check comparison: unknown" in text
    else:
        assert "REQUESTED ARTIFACT: matches" in text
        assert "artifact-check comparison: matches" in text
    assert "terminal work acceptance is not established" in text
    assert "target effect observation is unknown" in text
    assert "authorizes_acceptance: false" in text
    assert pg.get_effect_intent(store, intent["intent_id"]) == before
