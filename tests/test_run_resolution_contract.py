"""Catalog contracts for schema 26: ``work.run.resolve-v2`` and the
structured approval operations.  Storage behaviour is proven against
disposable PostgreSQL in tests/pg/test_run_resolution_facts.py."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl import approval, approval_schema, db, delegation_schema, operator_request, pg_migrations
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS, catalog_operation_specs

CONTRACTS = {c.name: c for c in WORK_OPERATION_CONTRACTS}
RUN = "run_" + "A" * 26
APPROVAL = "apr_" + "B" * 26


def _resolution(**overrides):
    return {
        "repo_id": "repo", "run_id": RUN, "principal_id": "github:1:0", "workspace_id": "ws-1",
        "client_id": None, "grant_id": None, "issued_at": "2026-10-14T10:00:00.123456Z",
        "superseded_by_run_id": None, "state": "active", **overrides,
    }


def _approval(**overrides):
    return {
        "approval_id": APPROVAL, "repo_id": "repo", "workspace_id": "ws-1", "run_id": RUN,
        "transition": "rollout-resume", "rationale": None,
        "approver": {"principal_id": "github:7:0", "client_id": None, "grant_id": None},
        "recorded_at": "2026-10-14T10:00:00Z", "expires_at": "2026-10-14T11:00:00Z", **overrides,
    }


def test_resolve_v1_descriptor_bytes_are_unchanged_and_v2_is_a_new_operation():
    baseline = json.loads(
        (Path(__file__).parent / "fixtures/attempt-v0170-base-catalog.json").read_text()
    )["operation_sha256"]
    actual = {
        spec["name"]: hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for spec in catalog_operation_specs(resource_schema_available=True)
    }
    assert actual["work.run.resolve-v1"] == baseline["work.run.resolve-v1"]
    assert "work.run.resolve-v2" not in baseline


def test_resolve_v2_extends_v1_with_issue_time_and_supersession():
    v1, v2 = CONTRACTS["work.run.resolve-v1"], CONTRACTS["work.run.resolve-v2"]
    assert v2.input_schema == v1.input_schema
    assert (v2.required_authority, v2.execution_semantics, v2.idempotency) == (None, "read", "not-allowed")
    assert set(v2.result_schema["required"]) == set(v1.result_schema["required"]) | {
        "issued_at", "superseded_by_run_id", "state"}
    assert v2.result_schema["additionalProperties"] is False
    jsonschema.validate(_resolution(), v2.result_schema)
    jsonschema.validate(_resolution(superseded_by_run_id="run_" + "C" * 26, state="superseded"),
                        v2.result_schema)
    # A run has no recorded close, so the state vocabulary is exactly two words.
    assert v2.result_schema["properties"]["state"] == {"enum": ["active", "superseded"]}
    for bad in (_resolution(state="closed"), _resolution(superseded_by_run_id="nope")):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, v2.result_schema)


def test_approval_operations_name_their_authorities_and_take_no_identity_arguments():
    record, get = CONTRACTS[approval.OPERATION_RECORD], CONTRACTS[approval.OPERATION_GET]
    assert (record.required_authority, record.execution_semantics, record.idempotency) == (
        "work:approve", "write", "not-allowed")
    assert (get.required_authority, get.execution_semantics, get.idempotency) == (
        "work:read", "read", "not-allowed")
    assert set(record.input_schema["required"]) == {"transition", "run_id", "expires_at", "idempotency_key"}
    for contract in (record, get):
        assert contract.input_schema["additionalProperties"] is False
        for field in ("repo_id", "workspace_id", "principal_id", "approver", "client_id", "grant_id"):
            assert field not in contract.input_schema["properties"]
    jsonschema.validate({**_approval(), "replayed": False}, record.result_schema)
    jsonschema.validate({**_approval(rationale="ok"), "evaluated_at": "2026-10-14T10:30:00Z",
                         "expired": False}, get.result_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"approval_id": "apr_bad"}, get.input_schema)


def test_work_approve_is_limited_to_effect_approval_and_explicit_request_decisions():
    holders = {c.name for c in WORK_OPERATION_CONTRACTS if c.required_authority == "work:approve"}
    assert holders == {
        approval.OPERATION_RECORD,
        operator_request.OPERATION_ATTEST,
        operator_request.OPERATION_DECLINE,
    }


@pytest.mark.parametrize("arguments,code", [
    ({"transition": "rollout-resume", "run_id": RUN, "expires_at": "2026-10-14T11:00:00Z"}, None),
    ({"transition": "rollout-resume", "run_id": RUN, "expires_at": "2026-10-14T11:00:00+03:00"}, None),
    ({"transition": "rollout-resume", "run_id": RUN, "expires_at": "2026-10-14 11:00"}, "invalid-arguments"),
    ({"transition": "-bad", "run_id": RUN, "expires_at": "2026-10-14T11:00:00Z"}, "invalid-arguments"),
    ({"transition": "rollout-resume", "run_id": RUN, "expires_at": "2026-10-14T11:00:00Z",
      "repo_id": "x"}, "invalid-arguments"),
])
def test_record_arguments(arguments, code):
    if code is None:
        fields = approval.record_arguments(arguments)
        assert fields["expires_at"].utcoffset().total_seconds() == 0
    else:
        with pytest.raises(approval.ApprovalRefused) as caught:
            approval.record_arguments(arguments)
        assert caught.value.code == code


def test_schema29_lease_basis_floor_and_operator_request_runtime_window():
    assert pg_migrations.MINIMUM_SCHEMA_VERSION == pg_migrations.MAXIMUM_SCHEMA_VERSION == 31
    assert approval.SCHEMA_VERSION == 26
    assert "work_approval" in approval_schema.DDL
    assert f"interval '{approval_schema.MAX_LIFETIME_SECONDS} seconds'" in approval_schema.DDL
    assert "work_run_delegation" in delegation_schema.DDL


def _sqlite_app(tmp_path):
    conn = sqlite3.connect(tmp_path / "local.db")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return WorkApplication(
        repo_id="local", store=conn, backend=db, ingest_records=lambda r: [],
        arbitrate_command=lambda *a, **k: None, list_records=lambda a, l: [], list_decisions=lambda a, l: [],
    )


def _context(authorities):
    return SimpleNamespace(
        identity=SimpleNamespace(actor="a", environment="t", authorities=frozenset(authorities),
                                 principal_id="p", workspace_id="w", client_id=None, grant_id=None),
        request_id="r", basis_revision=None, catalog_revision="c", idempotency_requirement="not-allowed",
        idempotency_key=None, repo_id=None,
    )


def test_standalone_sqlite_refuses_the_schema26_operations_clearly(tmp_path):
    app = _sqlite_app(tmp_path)
    context = _context({"work:read", "work:approve"})
    for operation, arguments in (
        ("work.run.resolve-v2", {"run_id": RUN}),
        (approval.OPERATION_RECORD, {}),
        (approval.OPERATION_GET, {"approval_id": APPROVAL}),
    ):
        with pytest.raises(ApplicationRejection) as caught:
            app.invoke(operation, arguments, context)
        assert caught.value.code == "unsupported-backend", operation
        assert "PostgreSQL" in caught.value.message


def test_the_owner_refuses_a_record_without_work_approve_before_anything_else(tmp_path):
    app = _sqlite_app(tmp_path)
    with pytest.raises(ApplicationRejection) as caught:
        app.invoke(approval.OPERATION_RECORD, {}, _context({"work:read", "work:evidence", "work:write"}))
    assert (caught.value.code, caught.value.http_status) == ("authority-required", 403)
