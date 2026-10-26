"""Read-only completion assessment and nonmutating follow-ups."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import uuid

import jsonschema
import pytest

from sprintctl import pg
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection

pytestmark = PG_MARKS
OP = "work.completion.assess-v1"
CONTRACT = next(contract for contract in WORK_OPERATION_CONTRACTS if contract.name == OP)


def _context(*, authorities=("work:read",)):
    identity = SimpleNamespace(
        actor="completion-test",
        environment="vuoro-test",
        authorities=frozenset(authorities),
        principal_id="completion-test:principal",
        workspace_id="completion-test:workspace",
        client_id=None,
        grant_id=None,
    )
    return SimpleNamespace(
        identity=identity,
        request_id="completion-test-request",
        basis_revision=None,
        catalog_revision="completion-test-catalog",
        idempotency_requirement="not-allowed",
        idempotency_key=None,
    )


@pytest.fixture
def prepared(store, work_item_id):
    store = replace(pg.get_connection(_PG_URL), repo_id=store.repo_id)
    assert_disposable_connection(store.conn)
    app = WorkApplication.postgres(store)
    reservation = pg.reserve(
        store, work_item_id, actor="completion-test", session_id="completion-test"
    )
    release = pg.get_release(store, reservation["release_digest"])
    args = {
        "item_id": work_item_id,
        "expected_revision": release["item_revision"],
        "release_digest": release["release_digest"],
    }
    try:
        yield app, store, work_item_id, args
    finally:
        store.conn.close()


def _assess(app, args, context=None):
    result = app.invoke(OP, args, context or _context())
    jsonschema.validate(result, CONTRACT.result_schema)
    return result


def test_assessment_is_read_only_bounded_and_exact_basis(prepared):
    app, store, item_id, args = prepared
    before = {
        "decisions": store.conn.execute(
            "SELECT count(*) AS n FROM work_decision WHERE repo_id=%s AND work_item_id=%s",
            (store.repo_id, item_id),
        ).fetchone()["n"],
        "leases": store.conn.execute(
            "SELECT count(*) AS n FROM work_lease WHERE repo_id=%s AND work_item_id=%s",
            (store.repo_id, item_id),
        ).fetchone()["n"],
        "reports": store.conn.execute(
            "SELECT count(*) AS n FROM work_outcome_report WHERE repo_id=%s AND work_item_id=%s",
            (store.repo_id, item_id),
        ).fetchone()["n"],
    }

    first = _assess(app, args)
    second = _assess(app, args)

    after = {
        "decisions": store.conn.execute(
            "SELECT count(*) AS n FROM work_decision WHERE repo_id=%s AND work_item_id=%s",
            (store.repo_id, item_id),
        ).fetchone()["n"],
        "leases": store.conn.execute(
            "SELECT count(*) AS n FROM work_lease WHERE repo_id=%s AND work_item_id=%s",
            (store.repo_id, item_id),
        ).fetchone()["n"],
        "reports": store.conn.execute(
            "SELECT count(*) AS n FROM work_outcome_report WHERE repo_id=%s AND work_item_id=%s",
            (store.repo_id, item_id),
        ).fetchone()["n"],
    }
    assert before == after
    assert first["basis_status"] == "current"
    assert first["assessment"]["status"] == "incomplete"
    assert first["authorizes_acceptance"] is False
    assert first["follow_up"]["suggestions"][0]["action_kind"] == "reverify"
    assert first["snapshot_digest"] == second["snapshot_digest"]
    assert (
        first["follow_up"]["suggestions"][0]["recommendation_id"]
        == second["follow_up"]["suggestions"][0]["recommendation_id"]
    )


def test_stale_or_unbound_release_cannot_be_assessed_as_current(prepared):
    app, _store, _item_id, args = prepared
    stale = _assess(app, {**args, "release_digest": "0" * 64})
    assert stale["basis_status"] == "stale"
    assert stale["assessment"]["status"] == "stale-basis"


def test_release_frozen_before_an_edit_is_not_current(prepared):
    app, store, item_id, args = prepared
    pg.update_work_item_description(
        store,
        item_id,
        "Edited after the assessment basis was frozen",
        expected_revision=args["expected_revision"].rsplit("@revise:", 1)[0],
        actor="completion-test",
    )

    stale = _assess(app, args)

    assert stale["basis_status"] == "stale"
    assert (
        stale["current_basis"]["release_item_revision"]
        != stale["current_basis"]["expected_revision"]
    )
    assert stale["assessment"]["status"] == "stale-basis"


def test_existing_report_outcome_writer_is_the_only_acceptance_path(prepared):
    app, store, item_id, args = prepared
    context = _context(authorities=("work:read", "work:evidence", "work:claim"))
    run = app.invoke("work.run.register-v1", {
        "harness_id": "fixture-harness",
        "harness_build": "1",
        "model_id": "fixture-model",
        "recipe_id": "completion-acceptance",
        "observed_profile": {"instruction_digest": "sha256:" + "a" * 64, "skill_digests": []},
        "idempotency_key": "completion-run-register",
    }, context)["run"]["run_id"]
    lease = app.invoke("work.lease.acquire-v1", {
        "item_id": item_id,
        "run_id": run,
        "idempotency_key": "completion-lease-acquire",
    }, context)["lease"]

    pending = _assess(app, args)
    assert pending["follow_up"]["suggestions"][0]["action_kind"] == "report-outcome"
    assert pending["follow_up"]["suggestions"][0]["next_operation"] == "work.lease.report-outcome-v1"

    report = app.invoke("work.lease.report-outcome-v1", {
        "lease_id": lease["lease_id"],
        "run_id": run,
        "outcome": "succeeded",
        "summary": "fixture verification passed",
        "payload": {"result": "ok"},
        "checks": [{"name": "tests", "status": "passed"}],
        "idempotency_key": "completion-outcome-report",
    }, context)
    assert report["settled"] is True

    current_release = pg.current_release(store, item_id)
    accepted = _assess(app, {
        "item_id": item_id,
        "expected_revision": current_release["item_revision"],
        "release_digest": current_release["release_digest"],
    })
    assert accepted["assessment"]["status"] == "accepted-current"
    assert accepted["owner_facts"]["terminal_decision"]["kind"] == "accept"
    assert accepted["authorizes_acceptance"] is False


def test_authority_and_idempotency_boundaries(prepared):
    app, _store, _item_id, args = prepared
    with pytest.raises(ApplicationRejection) as denied:
        _assess(app, args, _context(authorities=()))
    assert denied.value.code == "authority-required"

    context = _context()
    context.idempotency_key = "completion-read-key"
    with pytest.raises(ApplicationRejection) as keyed:
        _assess(app, args, context)
    assert keyed.value.code == "invalid-arguments"
