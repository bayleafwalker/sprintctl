"""Disposable PostgreSQL checks for schema-29 lease basis binding."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from sprintctl import pg, pg_migrations
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.lease_basis_schema import (
    _ACQUIRE_GUARD_FUNCTION,
    _ACQUIRE_GUARD_TRIGGER,
    _GUARD_FUNCTION,
    _GUARD_TRIGGER,
    inspect_catalog,
)
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg

pytestmark = PG_MARKS


@pytest.fixture
def lease_basis_store():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    schema = "s29_lease_basis_" + uuid.uuid4().hex
    repo_id = "itest-lease-basis-" + uuid.uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()
    store = pg.PgStore(conn=conn, repo_id=repo_id)
    pg_migrations.migrate_schema(store)
    try:
        yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


def _context():
    identity = SimpleNamespace(
        actor="lease-basis-test",
        environment="vuoro-pg-test",
        authorities=frozenset({"work:evidence", "work:claim", "work:read"}),
        principal_id="lease-basis-principal",
        workspace_id="lease-basis-workspace",
        client_id=None,
        grant_id=None,
    )
    return SimpleNamespace(
        identity=identity,
        request_id="lease-basis-test",
        basis_revision=None,
        catalog_revision="lease-basis-catalog",
        idempotency_requirement="not-allowed",
        idempotency_key=None,
    )


def _item_and_lease(store):
    context = _context()
    app = WorkApplication.postgres(store)
    sprint_id = pg.create_sprint(
        store, "Schema 29 lease basis", "Test migration", "2026-01-01", "2026-12-31", "active"
    )
    track_id = pg.get_or_create_track(store, sprint_id, "lease-basis")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Legacy lease")
    run = app.invoke("work.run.register-v1", {
        "harness_id": "fixture-harness",
        "harness_build": "1",
        "model_id": "fixture-model",
        "recipe_id": "lease-basis-migration",
        "observed_profile": {
            "instruction_digest": "sha256:" + "a" * 64,
            "skill_digests": [],
        },
        "idempotency_key": "lease-basis-register-run",
    }, context)["run"]["run_id"]
    lease = app.invoke("work.lease.acquire-v1", {
        "item_id": item_id,
        "run_id": run,
        "idempotency_key": "lease-basis-acquire-lease",
    }, context)["lease"]
    return app, context, item_id, run, lease


def test_schema_28_lease_rows_remain_basis_unknown_and_cannot_settle(lease_basis_store):
    store = lease_basis_store
    app, context, item_id, run_id, lease = _item_and_lease(store)
    assert lease["basis_item_revision"]
    assert lease["basis_release_digest"] is None

    # Reconstruct the schema-28 shape around a live lease. The acquisition
    # revision is deliberately lost, as it would be for a real legacy row.
    with store.conn.cursor() as cur:
        from tests.pg._shared import drop_schema30
        drop_schema30(cur)
        cur.execute("DROP TABLE operator_continuation_receipt")
        cur.execute("DROP FUNCTION sprintctl_continuation_receipt_append_only()")
        cur.execute(f"DROP TRIGGER {_ACQUIRE_GUARD_TRIGGER} ON work_lease")
        cur.execute(f"DROP TRIGGER {_GUARD_TRIGGER} ON work_decision")
        cur.execute(f"DROP FUNCTION {_ACQUIRE_GUARD_FUNCTION}()")
        cur.execute(f"DROP FUNCTION {_GUARD_FUNCTION}()")
        cur.execute(
            "ALTER TABLE work_lease DROP COLUMN basis_item_revision, "
            "DROP COLUMN basis_release_digest"
        )
        cur.execute("UPDATE schema_version SET version=28")
    store.conn.commit()

    handshake = pg_migrations.compatibility_handshake(store)
    assert handshake["compatible"] is False and handshake["reason"] == "schema-too-old"
    with store.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 28
        before = inspect_catalog(cur)
    store.conn.rollback()
    assert before["complete"] is True and before["available"] is False

    migrated = pg_migrations.migrate_schema(store)
    assert migrated["applied_versions"] == [29, 30, 31]
    assert migrated["compatibility"]["compatible"] is True
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT basis_item_revision, basis_release_digest FROM work_lease "
            "WHERE repo_id=%s AND lease_id=%s",
            (store.repo_id, lease["lease_id"]),
        )
        assert cur.fetchone() == {
            "basis_item_revision": None,
            "basis_release_digest": None,
        }
    store.conn.rollback()

    with pytest.raises(ApplicationRejection) as refused:
        app.invoke("work.lease.report-outcome-v1", {
            "lease_id": lease["lease_id"],
            "run_id": run_id,
            "outcome": "succeeded",
            "summary": "legacy report",
            "payload": {"result": "ok"},
            "checks": [{"name": "tests", "status": "passed"}],
            "idempotency_key": "legacy-lease-report-key",
        }, context)
    assert refused.value.code == "lease-basis-unavailable"
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT disposition, reason_code, decision_id FROM work_outcome_report "
            "WHERE repo_id=%s AND lease_id=%s",
            (store.repo_id, lease["lease_id"]),
        )
        assert cur.fetchone() == {
            "disposition": "rejected",
            "reason_code": "lease-basis-unavailable",
            "decision_id": None,
        }
        cur.execute(
            "SELECT state, end_reason FROM work_lease WHERE repo_id=%s AND lease_id=%s",
            (store.repo_id, lease["lease_id"]),
        )
        assert cur.fetchone() == {"state": "released", "end_reason": "lease-basis-unavailable"}
    store.conn.rollback()
    assert pg.list_decisions(store, item_id) == []


def test_database_guards_reject_schema_28_unbound_writes(lease_basis_store):
    store = lease_basis_store
    app, _context_value, item_id, run_id, _lease = _item_and_lease(store)

    with pytest.raises(psycopg.errors.CheckViolation, match="new work leases require"):
        with store.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_lease (repo_id, lease_id, work_item_id, run_id, "
                "principal_id, workspace_id, claim_key, state, ttl_seconds, acquired_at, "
                "heartbeat_at, verification_profile, required_checks) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', 600, now(), now(), 'checked', '[]'::jsonb)",
                (
                    store.repo_id,
                    "lease_" + "0" * 25 + "1",
                    item_id,
                    run_id,
                    "legacy-runtime-principal",
                    "lease-basis-workspace",
                    "legacy-runtime-claim-key",
                ),
            )
    store.conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation, match="schema 29 basis check"):
        pg.record_decision(
            store,
            item_id,
            "accept",
            actor=pg.SETTLEMENT_ACTOR,
            rationale="stale schema-28 settlement attempt",
            evidence_digests=["a" * 64],
        )
    store.conn.rollback()
    assert pg.list_decisions(store, item_id) == []


def test_schema_29_partial_guard_is_read_only_incompatible_and_not_repaired(lease_basis_store):
    store = lease_basis_store
    with store.conn.cursor() as cur:
        cur.execute(f"DROP TRIGGER {_ACQUIRE_GUARD_TRIGGER} ON work_lease")
    store.conn.commit()

    handshake = pg_migrations.compatibility_handshake(store)
    assert handshake["compatible"] is False
    assert handshake["reason"] == "work-lease-basis-storage-partial"
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError):
        pg_migrations.require_compatible_schema(store)
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND t.tgname=%s AND NOT t.tgisinternal",
            (_ACQUIRE_GUARD_TRIGGER,),
        )
        assert cur.fetchone() is None
    store.conn.rollback()
