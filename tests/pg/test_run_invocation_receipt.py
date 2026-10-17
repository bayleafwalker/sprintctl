"""Atomic owner registration correlation and exact current-owner receipt reads."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl import pg, pg_migrations
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import (
    _PG_URL,
    PG_MARKS,
    assert_disposable_connection,
    dict_row,
    psycopg,
)

pytestmark = PG_MARKS
CONTRACTS = {item.name: item for item in WORK_OPERATION_CONTRACTS}
INVOCATION = "inv_0123456789ABCDEFGHJKMNPQRS"
ATTEMPT = "att_0123456789ABCDEFGHJKMNPQRS"


@pytest.fixture(scope="module")
def receipt_store(pg_test_scope, store):
    # Initialize the disposable test database's public cleanup relations too;
    # this fixture's actual owner rows live in its private schema below.
    del store
    schema = "run_receipt_" + uuid.uuid4().hex
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = pg_test_scope("run-invocation-receipt")
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        store = pg.PgStore(conn=conn, repo_id=repo_id)
        pg_migrations.migrate_schema(store)
        yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


def _context(*, invocation_id=None, attempt_id=None, principal_epoch=None,
             principal_id="issuer:subject:0", workspace_id="ws-receipt",
             client_id="client-1", grant_id="grant-1", authorities=None):
    identity = SimpleNamespace(
        actor="receipt-test", environment="pg-test",
        authorities=frozenset(authorities or {"work:evidence", "work:read"}),
        principal_id=principal_id, workspace_id=workspace_id,
        client_id=client_id, grant_id=grant_id,
        invocation_id=invocation_id, attempt_id=attempt_id,
        principal_epoch=principal_epoch,
    )
    return SimpleNamespace(identity=identity, request_id="request-test", basis_revision=None,
                           catalog_revision="test-catalog", idempotency_requirement="required",
                           idempotency_key=None, repo_id=None)


class _CheckedApplication:
    def __init__(self, store):
        self.inner = WorkApplication.postgres(store)

    def invoke(self, operation, arguments, context):
        jsonschema.validate(arguments, CONTRACTS[operation].input_schema)
        result = self.inner.invoke(operation, arguments, context)
        jsonschema.validate(result, CONTRACTS[operation].result_schema)
        return result


def _args(key=None):
    return {
        "harness_id": "test-harness", "harness_build": "build-1",
        "model_id": "model-1", "recipe_id": "recipe-1",
        "observed_profile": {"instruction_digest": "sha256:" + "a" * 64,
                              "skill_digests": []},
        "idempotency_key": key or "receipt-" + uuid.uuid4().hex,
    }


def _correlated_context(invocation=INVOCATION, attempt=ATTEMPT, **kwargs):
    kwargs.setdefault("principal_epoch", 0)
    return _context(invocation_id=invocation, attempt_id=attempt,
                    **kwargs)


def _ids():
    return ("inv_" + uuid.uuid4().hex[:26].upper(),
            "att_" + uuid.uuid4().hex[:26].upper())


def test_correlated_register_and_exact_receipt_replay(receipt_store):
    store = receipt_store
    app = _CheckedApplication(store)
    args = _args()
    invocation, attempt = _ids()
    first = app.invoke("work.run.register-correlated-v1", args,
                       _correlated_context(invocation, attempt))
    assert first["correlation_status"] == "bound"
    assert first["receipt"]["owner_record_id"] == first["run"]["run_id"]
    assert first["receipt"]["owner_binding"]["principal_epoch"] == 0
    assert "owner_request_digest" not in first["receipt"]
    assert "request_digest" not in first["run"]

    second_attempt = "att_" + uuid.uuid4().hex[:26].upper()
    replay = app.invoke(
        "work.run.register-correlated-v1", args,
        _correlated_context(invocation, second_attempt),
    )
    assert replay["run"] == first["run"]
    assert replay["correlation_status"] == "bound"
    assert replay["receipt"]["attempt_id"] == second_attempt

    exact_retry = app.invoke(
        "work.run.register-correlated-v1", args,
        _correlated_context(invocation, second_attempt),
    )
    assert exact_retry == replay
    assert app.invoke("work.run.invocation-receipt-get-v1", {
        "run_id": first["run"]["run_id"], "invocation_id": invocation,
        "attempt_id": second_attempt,
    }, _correlated_context(invocation, second_attempt, authorities={"work:read"})) == replay["receipt"]


def test_correlated_register_refuses_missing_or_partial_verified_metadata(receipt_store):
    app = _CheckedApplication(receipt_store)
    for context in (
        _context(principal_epoch=0),
        _context(invocation_id=INVOCATION, principal_epoch=0),
        _context(attempt_id=ATTEMPT, principal_epoch=0),
        _context(invocation_id=INVOCATION, attempt_id=ATTEMPT, principal_epoch=True),
    ):
        with pytest.raises(ApplicationRejection) as raised:
            app.invoke("work.run.register-correlated-v1", _args(), context)
        assert raised.value.code == "invocation-correlation-required"


def test_legacy_replay_is_not_retroactively_adopted(receipt_store):
    store = receipt_store
    app = _CheckedApplication(store)
    args = _args()
    legacy = app.invoke("work.run.register-v1", args, _context())
    invocation, attempt = _ids()
    correlated = app.invoke("work.run.register-correlated-v1", args,
                            _correlated_context(invocation, attempt))
    assert correlated["run"] == legacy["run"]
    assert correlated["correlation_status"] == "legacy-unbound"
    assert correlated["receipt"] is None
    with store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM work_run_invocation_binding WHERE run_id=%s",
                    (legacy["run"]["run_id"],))
        assert cur.fetchone()["n"] == 0
    store.conn.rollback()


def test_legacy_unbound_replay_rejects_attempt_id_bound_to_another_run(receipt_store):
    app = _CheckedApplication(receipt_store)
    correlated_args = _args()
    invocation, attempt = _ids()
    app.invoke(
        "work.run.register-correlated-v1",
        correlated_args,
        _correlated_context(invocation, attempt),
    )

    legacy_args = _args()
    legacy = app.invoke("work.run.register-v1", legacy_args, _context())
    new_invocation, _new_attempt = _ids()
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke(
            "work.run.register-correlated-v1",
            legacy_args,
            _correlated_context(new_invocation, attempt),
        )
    assert raised.value.code == "invocation-correlation-conflict"

    with receipt_store.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_attempt WHERE attempt_id=%s",
            (attempt,),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_binding WHERE run_id=%s",
            (legacy["run"]["run_id"],),
        )
        assert cur.fetchone()["n"] == 0
    receipt_store.conn.rollback()


def test_idempotency_replay_cannot_bind_a_different_invocation_or_attempt(receipt_store):
    app = _CheckedApplication(receipt_store)
    args = _args()
    invocation, attempt = _ids()
    first = app.invoke("work.run.register-correlated-v1", args,
                       _correlated_context(invocation, attempt))
    other_invocation, other_attempt = _ids()
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.register-correlated-v1", args,
                   _correlated_context(invocation=other_invocation, attempt=other_attempt))
    assert raised.value.code == "invocation-correlation-conflict"
    foreign_invocation = "inv_" + uuid.uuid4().hex[:26].upper()
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.register-correlated-v1", _args(),
                   _correlated_context(invocation=foreign_invocation, attempt=attempt))
    assert raised.value.code == "invocation-correlation-conflict"
    assert first["correlation_status"] == "bound"


@pytest.mark.parametrize("reuse_attempt", [True, False])
def test_new_run_cannot_reuse_existing_invocation_or_attempt_atomically(
    receipt_store, reuse_attempt
):
    store = receipt_store
    app = _CheckedApplication(store)
    invocation, attempt = _ids()
    original_args = _args()
    original = app.invoke(
        "work.run.register-correlated-v1",
        original_args,
        _correlated_context(invocation, attempt),
    )
    original_run_id = original["run"]["run_id"]
    second_attempt = attempt if reuse_attempt else _ids()[1]
    conflicting_args = _args()

    with pytest.raises(ApplicationRejection) as raised:
        app.invoke(
            "work.run.register-correlated-v1",
            conflicting_args,
            _correlated_context(invocation, second_attempt),
        )
    assert (raised.value.code, raised.value.http_status) == (
        "invocation-correlation-conflict",
        409,
    )

    receipt_query = {
        "run_id": original_run_id,
        "invocation_id": invocation,
        "attempt_id": attempt,
    }
    assert app.invoke(
        "work.run.invocation-receipt-get-v1",
        receipt_query,
        _correlated_context(invocation, attempt, authorities={"work:read"}),
    ) == original["receipt"]
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM run WHERE repo_id=%s AND idempotency_key=%s",
            (store.repo_id, original_args["idempotency_key"]),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_idempotency_ledger "
            "WHERE repo_id=%s AND idempotency_key=%s",
            (store.repo_id, original_args["idempotency_key"]),
        )
        assert cur.fetchone()["n"] == 1
        for table in ("run", "work_idempotency_ledger"):
            cur.execute(
                f"SELECT count(*) AS n FROM {table} "
                "WHERE repo_id=%s AND idempotency_key=%s",
                (store.repo_id, conflicting_args["idempotency_key"]),
            )
            assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_binding "
            "WHERE invocation_id=%s",
            (invocation,),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_attempt "
            "WHERE invocation_id=%s",
            (invocation,),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_attempt "
            "WHERE attempt_id=%s",
            (second_attempt,),
        )
        expected_attempt_count = 1 if reuse_attempt else 0
        assert cur.fetchone()["n"] == expected_attempt_count
    store.conn.rollback()


def test_existing_invocation_epoch_collision_is_a_conflict(receipt_store):
    store = receipt_store
    app = _CheckedApplication(store)
    invocation, attempt = _ids()
    original_args = _args()
    original = app.invoke(
        "work.run.register-correlated-v1",
        original_args,
        _correlated_context(invocation, attempt, principal_epoch=7),
    )
    fresh_attempt = _ids()[1]

    with pytest.raises(ApplicationRejection) as raised:
        app.invoke(
            "work.run.register-correlated-v1",
            original_args,
            _correlated_context(
                invocation,
                fresh_attempt,
                principal_epoch=8,
            ),
        )
    assert (raised.value.code, raised.value.http_status) == (
        "invocation-correlation-conflict",
        409,
    )
    assert app.invoke(
        "work.run.invocation-receipt-get-v1",
        {
            "run_id": original["run"]["run_id"],
            "invocation_id": invocation,
            "attempt_id": attempt,
        },
        _correlated_context(
            invocation,
            attempt,
            principal_epoch=7,
            authorities={"work:read"},
        ),
    ) == original["receipt"]
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM run WHERE repo_id=%s AND idempotency_key=%s",
            (store.repo_id, original_args["idempotency_key"]),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_idempotency_ledger "
            "WHERE repo_id=%s AND idempotency_key=%s",
            (store.repo_id, original_args["idempotency_key"]),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_binding "
            "WHERE invocation_id=%s",
            (invocation,),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_attempt "
            "WHERE attempt_id=%s",
            (fresh_attempt,),
        )
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM work_run_invocation_attempt "
            "WHERE invocation_id=%s",
            (invocation,),
        )
        assert cur.fetchone()["n"] == 1
    store.conn.rollback()


def test_exact_owner_tuple_with_changed_run_projection_remains_integrity_unknown(
    receipt_store,
):
    store = receipt_store
    app = _CheckedApplication(store)
    invocation, attempt = _ids()
    args = _args()
    bound = app.invoke(
        "work.run.register-correlated-v1",
        args,
        _correlated_context(invocation, attempt),
    )
    run_id = bound["run"]["run_id"]
    with store.conn.cursor() as cur:
        cur.execute(
            "UPDATE run SET harness_build=%s WHERE repo_id=%s AND run_id=%s",
            ("tampered-after-receipt", store.repo_id, run_id),
        )
        assert cur.rowcount == 1
    store.conn.commit()

    with pytest.raises(ApplicationRejection) as read_error:
        app.invoke(
            "work.run.invocation-receipt-get-v1",
            {"run_id": run_id, "invocation_id": invocation, "attempt_id": attempt},
            _correlated_context(
                invocation, attempt, authorities={"work:read"}
            ),
        )
    assert (read_error.value.code, read_error.value.http_status) == (
        "run-invocation-integrity-unknown",
        503,
    )
    store.conn.rollback()

    with pytest.raises(ApplicationRejection) as replay_error:
        app.invoke(
            "work.run.register-correlated-v1",
            args,
            _correlated_context(invocation, attempt),
        )
    assert (replay_error.value.code, replay_error.value.http_status) == (
        "run-invocation-integrity-unknown",
        503,
    )
    store.conn.rollback()


def test_digest_conflict_never_appends_an_invocation_attempt(receipt_store):
    store = receipt_store
    app = _CheckedApplication(store)
    invocation, attempt = _ids()
    args = _args()
    app.invoke("work.run.register-correlated-v1", args,
               _correlated_context(invocation, attempt))
    changed = {**args, "harness_build": "different-build"}
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.register-correlated-v1", changed,
                   _correlated_context(invocation, "att_" + uuid.uuid4().hex[:26].upper()))
    assert raised.value.code == "idempotency-conflict"
    with store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM work_run_invocation_attempt WHERE invocation_id=%s",
                    (invocation,))
        assert cur.fetchone()["n"] == 1
    store.conn.rollback()


def test_receipt_read_requires_current_owner_and_exact_authority(receipt_store):
    app = _CheckedApplication(receipt_store)
    invocation, attempt = _ids()
    result = app.invoke("work.run.register-correlated-v1", _args(),
                        _correlated_context(invocation, attempt))
    query = {"run_id": result["run"]["run_id"], "invocation_id": invocation,
             "attempt_id": attempt}
    for foreign in (
        _correlated_context(invocation, attempt, authorities={"work:read"}, principal_id="foreign:subject:0"),
        _correlated_context(invocation, attempt, authorities={"work:read"}, workspace_id="foreign-workspace"),
        _correlated_context(invocation, attempt, authorities={"work:read"}, client_id="foreign-client"),
        _correlated_context(invocation, attempt, authorities={"work:read"}, grant_id="foreign-grant"),
        _correlated_context(invocation, attempt, authorities={"work:read"}, principal_epoch=1),
    ):
        with pytest.raises(ApplicationRejection) as raised:
            app.invoke("work.run.invocation-receipt-get-v1", query, foreign)
        assert (raised.value.code, raised.value.http_status) == ("run-not-found", 404)
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.invocation-receipt-get-v1", query,
                   _correlated_context(invocation, attempt, authorities={"work:evidence"}))
    assert raised.value.code == "authority-required"

    no_receipt = {**query, "attempt_id": "att_3123456789ABCDEFGHJKMNPQRS"}
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.invocation-receipt-get-v1", no_receipt,
                   _correlated_context(invocation, attempt, authorities={"work:read"}))
    assert (raised.value.code, raised.value.http_status) == ("correlation-absent", 404)
    unknown = {**query, "run_id": "run_1123456789ABCDEFGHJKMNPQRS"}
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.invocation-receipt-get-v1", unknown,
                   _correlated_context(invocation, attempt, authorities={"work:read"}))
    assert (raised.value.code, raised.value.http_status) == ("run-not-found", 404)


def test_correlation_failure_rolls_back_run_ledger_and_receipts(receipt_store, monkeypatch):
    store = receipt_store
    app = _CheckedApplication(store)
    args = _args()

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic storage outage")

    monkeypatch.setattr(pg, "record_run_invocation_attempt", fail)
    with pytest.raises(ApplicationRejection) as raised:
        app.invoke("work.run.register-correlated-v1", args, _correlated_context())
    assert (raised.value.code, raised.value.http_status) == ("run-invocation-write-unknown", 503)
    with store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM run WHERE repo_id=%s AND idempotency_key=%s",
                    (store.repo_id, args["idempotency_key"]))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM work_idempotency_ledger WHERE repo_id=%s AND idempotency_key=%s",
                    (store.repo_id, args["idempotency_key"]))
        assert cur.fetchone()["n"] == 0
    store.conn.rollback()


def test_schema23_catalog_is_exact_and_receipts_are_immutable(receipt_store):
    store = receipt_store
    assert store.remote_schema_version == 27
    handshake = pg_migrations.compatibility_handshake(store)
    capability = handshake["capabilities"]["run_invocation_storage"]
    assert handshake["compatible"] is True
    assert capability["available"] is True
    assert capability["catalog_fingerprint"] == capability["expected_catalog_fingerprint"]

    app = _CheckedApplication(store)
    invocation, attempt = _ids()
    result = app.invoke("work.run.register-correlated-v1", _args(),
                        _correlated_context(invocation, attempt))
    with pytest.raises(psycopg.errors.CheckViolation), store.conn.cursor() as cur:
        cur.execute("UPDATE work_run_invocation_binding SET principal_epoch=99 WHERE invocation_id=%s",
                    (invocation,))
    store.conn.rollback()
    assert app.invoke("work.run.invocation-receipt-get-v1", {
        "run_id": result["run"]["run_id"], "invocation_id": invocation,
        "attempt_id": attempt,
    }, _correlated_context(invocation, attempt, authorities={"work:read"})) == result["receipt"]


def test_schema23_catalog_rejects_same_name_index_with_wrong_columns(receipt_store):
    store = receipt_store
    assert pg_migrations.compatibility_handshake(store)["compatible"] is True
    with store.conn.cursor() as cur:
        cur.execute("DROP INDEX idx_work_run_invocation_attempt_owner")
        cur.execute(
            "CREATE INDEX idx_work_run_invocation_attempt_owner "
            "ON work_run_invocation_attempt(attempt_id)"
        )
        catalog = pg_migrations.compatibility_handshake(store)["capabilities"][
            "run_invocation_storage"
        ]
        assert catalog["available"] is False
        assert catalog["complete"] is False
    store.conn.rollback()
    assert pg_migrations.compatibility_handshake(store)["compatible"] is True


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE work_run_invocation_binding SET principal_epoch=99",
        "DELETE FROM work_run_invocation_attempt",
        "TRUNCATE TABLE work_run_invocation_binding, work_run_invocation_attempt",
    ],
)
def test_binding_and_attempt_rows_reject_update_delete_and_truncate(receipt_store, statement):
    app = _CheckedApplication(receipt_store)
    invocation, attempt = _ids()
    result = app.invoke("work.run.register-correlated-v1", _args(),
                        _correlated_context(invocation, attempt))
    with pytest.raises(psycopg.errors.CheckViolation), receipt_store.conn.cursor() as cur:
        if statement.startswith("UPDATE"):
            cur.execute(statement + " WHERE invocation_id=%s", (invocation,))
        else:
            cur.execute(statement)
    receipt_store.conn.rollback()
    assert app.invoke("work.run.invocation-receipt-get-v1", {
        "run_id": result["run"]["run_id"], "invocation_id": invocation,
        "attempt_id": attempt,
    }, _correlated_context(invocation, attempt, authorities={"work:read"})) == result["receipt"]


def test_v22_owner_refuses_new_receipt_capability_without_failing_legacy(receipt_store):
    store = receipt_store
    app = _CheckedApplication(store)
    actual = store.remote_schema_version
    store.remote_schema_version = 22
    try:
        with pytest.raises(ApplicationRejection) as raised:
            app.invoke("work.run.register-correlated-v1", _args(), _correlated_context())
        assert raised.value.code == "owner-capability-unavailable"
        legacy = app.invoke("work.run.register-v1", _args(), _context())
        assert legacy["run"]["run_id"].startswith("run_")
    finally:
        store.remote_schema_version = actual
