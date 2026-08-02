"""
PostgreSQL integration tests for sprintctl.pg.

Requires a disposable PostgreSQL database owned by a dedicated, unprivileged
test role. See docs/guides/postgres-integration-tests.md for the contract.
Set SPRINTCTL_TEST_PG_URL to run:

    SPRINTCTL_TEST_PG_URL=postgresql://localhost/testdb pytest tests/test_pg_integration.py -v

All tests are automatically skipped when the variable is unset or psycopg is unavailable.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import threading
import uuid

import pytest

_PG_URL: str | None = os.environ.get("SPRINTCTL_TEST_PG_URL")

try:
    import psycopg
    from psycopg.rows import dict_row
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False

_SKIP = not _PG_URL or not _PSYCOPG_AVAILABLE
_SKIP_REASON = (
    "SPRINTCTL_TEST_PG_URL not set"
    if not _PG_URL
    else "psycopg not installed — run: pip install 'sprintctl[remote]'"
)

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(_SKIP, reason=_SKIP_REASON),
]

# Safe unconditional imports: pg.py handles missing psycopg gracefully.
from sprintctl import authority, contracts, db, maintain, observations, pg, projection, sync
from sprintctl import outbox
from sprintctl.cli import cli
from sprintctl.db import ClaimConflict, InvalidTransition
from sprintctl.pg_testing import (
    assert_disposable_connection,
    cleanup_test_repositories,
    new_test_repo_id,
    new_test_repo_uuid,
    write_cleanup_report,
)
from sprintctl.maintenance_capability import (
    MaintenanceCapabilityError,
    PostgresMaintenanceCapabilityStore,
)
from tests.test_maintenance_capability import CAPABILITY_ID, AT, envelope


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_test_scope():
    """Register test scopes, then clean and report them from one finalizer."""
    if _SKIP:
        pytest.skip(_SKIP_REASON)
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_ids: set[str] = set()

    def register(label: str = "scope", *, canonical_uuid: bool = False) -> str:
        repo_id = new_test_repo_uuid() if canonical_uuid else new_test_repo_id(label)
        repo_ids.add(repo_id)
        return repo_id

    try:
        yield register
    finally:
        report_path = os.environ.get("SPRINTCTL_TEST_PG_CLEANUP_REPORT")
        try:
            report = cleanup_test_repositories(conn, repo_ids)
        except Exception as exc:
            if report_path:
                write_cleanup_report(
                    report_path,
                    {
                        "schema_version": "sprintctl-pg-cleanup/v1",
                        "cleanup_completed": False,
                        "error_type": type(exc).__name__,
                        "repo_ids": sorted(repo_ids),
                    },
                )
            raise
        else:
            if report_path:
                write_cleanup_report(report_path, report)
        finally:
            conn.close()


@pytest.fixture(scope="module")
def store(pg_test_scope):
    """Create the module store only after the disposable-target preflight."""
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = pg_test_scope("module")
    s = pg.PgStore(
        conn=conn,
        repo_id=repo_id,
        authority_repo_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{repo_id}")),
    )
    pg.init_db(s)
    try:
        yield s
    finally:
        conn.close()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _authority_repo_uuid(store) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{store.repo_id}"))


def _append_authority_command(
    conn,
    store,
    *,
    record_type,
    aggregate_type,
    basis_revision,
    payload,
    aggregate_uuid=None,
    claim_id=None,
    actor="authority-test",
):
    refs = {
        "repo_id": _authority_repo_uuid(store),
        "aggregate_type": aggregate_type,
    }
    if aggregate_uuid is not None:
        refs["aggregate_uuid"] = aggregate_uuid
    if claim_id is not None:
        refs["claim_id"] = claim_id
    command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()),
        record_type=record_type,
        schema_version="1",
        actor=actor,
        authored_at="2026-07-14T18:00:00Z",
        refs=refs,
        payload=payload,
        basis_revision=basis_revision,
        correlation_id=str(uuid.uuid4()),
    )
    return outbox.append_authority_command(conn, command)


def _receipt_bytes(store, sprint_id, boundary_event_id, **overrides):
    receipt_id = f"{store.repo_id}.2026-07-13.boundary"
    receipt = {
        "schema_version": "capability-receipt/v1",
        "id": receipt_id,
        "project": store.repo_id,
        "status": "draft",
        "publication": "private",
        "boundary": {
            "kind": "sprint-close",
            "ref": {
                "kind": "sprint-event",
                "source": f"sprintctl:{store.repo_id}:sprint:{sprint_id}",
                "revision": f"event:{boundary_event_id}",
            },
        },
    }
    receipt.update(overrides)
    return json.dumps(receipt, sort_keys=True).encode()


def _receipt_payload(store, receipt_bytes):
    receipt_id = f"{store.repo_id}.2026-07-13.boundary"
    return {
        "project": store.repo_id,
        "receipt_id": receipt_id,
        "receipt_path": (
            f"/projects/dev/_artifacts/{store.repo_id}/capability/receipts/"
            f"{receipt_id}.json"
        ),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }


@pytest.fixture
def sprint_id(store):
    return pg.create_sprint(store, f"S-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")


@pytest.fixture
def track_id(store, sprint_id):
    return pg.get_or_create_track(store, sprint_id, "eng")


@pytest.fixture
def work_item_id(store, sprint_id, track_id):
    return pg.create_work_item(store, sprint_id, track_id, f"Item-{_uid()}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestMaintenanceCapabilityLifecycle:
    def test_postgres_matches_exact_plan_lifecycle_and_replay(self, store, pg_test_scope):
        repo_id = pg_test_scope("maintenance-lifecycle")
        lifecycle = PostgresMaintenanceCapabilityStore(
            pg.PgStore(conn=store.conn, repo_id=repo_id)
        )
        capability_id = f"mcap:{uuid.uuid4()}"
        prepare_id = str(uuid.uuid4())
        prepared = lifecycle.prepare(capability_id=capability_id, request_id=prepare_id, envelope=envelope(), actor="operator", at=AT)
        assert lifecycle.prepare(capability_id=capability_id, request_id=prepare_id, envelope=envelope(), actor="operator", at=AT)["duplicate"] is True
        attested = lifecycle.transition(capability_id=capability_id, request_id=str(uuid.uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT, effect_ref="sha256:" + "0" * 64)
        active = lifecycle.transition(capability_id=capability_id, request_id=str(uuid.uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)
        assert active["state"] == "active"
        recovery = lifecycle.append_recovery_record(capability_id=capability_id, record_id=str(uuid.uuid4()), kind="requested-command", payload_ref="artifact:sha256:" + "e" * 64, actor="recovery", at=AT)
        assert recovery["authority"] == "none"
        assert lifecycle.get(capability_id)["state"] == "active"

    def test_claim_activation_race_has_exactly_one_authority_winner(
        self, store, pg_test_scope
    ):
        repo_id = pg_test_scope("maintenance-race")
        setup_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(setup_store, f"Maintenance Race-{_uid()}", status="active")
        track_id = pg.get_or_create_track(setup_store, sprint_id, "authority")
        item_id = pg.create_work_item(setup_store, sprint_id, track_id, "Race target")

        capability_id = f"mcap:{uuid.uuid4()}"
        lifecycle = PostgresMaintenanceCapabilityStore(setup_store)
        prepared = lifecycle.prepare(
            capability_id=capability_id,
            request_id=str(uuid.uuid4()),
            envelope=envelope(),
            actor="operator",
            at=AT,
        )
        attested = lifecycle.transition(
            capability_id=capability_id,
            request_id=str(uuid.uuid4()),
            action="attest",
            expected_revision=prepared["revision"],
            actor="operator",
            at=AT,
            effect_ref="sha256:" + "0" * 64,
        )

        barrier = threading.Barrier(3)
        outcomes: dict[str, str] = {}

        def activate() -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            try:
                actor_store = pg.PgStore(conn=conn, repo_id=repo_id)
                barrier.wait()
                PostgresMaintenanceCapabilityStore(actor_store).transition(
                    capability_id=capability_id,
                    request_id=str(uuid.uuid4()),
                    action="activate",
                    expected_revision=attested["revision"],
                    actor="operator",
                    at=AT,
                    step_id="attest-backup",
                    command_id="verify-backup",
                    command_ref="sha256:" + "c" * 64,
                    effect_ref="sha256:" + "d" * 64,
                )
                outcomes["activation"] = "accepted"
            except MaintenanceCapabilityError as exc:
                outcomes["activation"] = f"rejected:{exc}"
            finally:
                conn.close()

        def claim() -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            try:
                actor_store = pg.PgStore(conn=conn, repo_id=repo_id)
                barrier.wait()
                pg.create_claim(actor_store, item_id, "ordinary-agent")
                outcomes["claim"] = "accepted"
            except ClaimConflict as exc:
                outcomes["claim"] = f"rejected:{exc}"
            finally:
                conn.close()

        workers = [threading.Thread(target=activate), threading.Thread(target=claim)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)
            assert not worker.is_alive(), "shared claim/capability arbitration deadlocked"

        assert sorted(value.split(":", 1)[0] for value in outcomes.values()) == [
            "accepted",
            "rejected",
        ]
        with setup_store.conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM maintenance_capability WHERE repo_id=%s AND capability_id=%s",
                (repo_id, capability_id),
            )
            capability_active = cur.fetchone()["state"] == "active"
            cur.execute(
                "SELECT count(*) AS count FROM claim WHERE repo_id=%s AND status='active' AND expires_at > now()",
                (repo_id,),
            )
            live_claims = int(cur.fetchone()["count"])
        assert not (capability_active and live_claims), outcomes

    def test_rejected_claim_rolls_back_repo_arbitration_on_retained_connection(
        self, store, pg_test_scope
    ):
        repo_id = pg_test_scope("maintenance-conflict-rollback")
        retained = psycopg.connect(_PG_URL, row_factory=dict_row)
        contender = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            retained_store = pg.PgStore(conn=retained, repo_id=repo_id)
            sprint_id = pg.create_sprint(retained_store, f"Conflict rollback-{_uid()}", status="active")
            track_id = pg.get_or_create_track(retained_store, sprint_id, "authority")
            item_id = pg.create_work_item(retained_store, sprint_id, track_id, "Rejected claim")
            lifecycle = PostgresMaintenanceCapabilityStore(retained_store)
            prepared = lifecycle.prepare(capability_id=f"mcap:{uuid.uuid4()}", request_id=str(uuid.uuid4()), envelope=envelope(), actor="operator", at=AT)
            attested = lifecycle.transition(capability_id=prepared["capability_id"], request_id=str(uuid.uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT, effect_ref="sha256:" + "0" * 64)
            lifecycle.transition(capability_id=prepared["capability_id"], request_id=str(uuid.uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)

            with pytest.raises(ClaimConflict):
                pg.create_claim(retained_store, item_id, "ordinary-agent")
            assert retained.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            with contender.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired",
                    (repo_id,),
                )
                assert cur.fetchone()["acquired"] is True
            contender.rollback()
        finally:
            retained.close()
            contender.close()

    def test_postgres_receipt_and_recovery_evidence_reject_mutation(
        self, store, pg_test_scope
    ):
        repo_id = pg_test_scope("maintenance-evidence-immutable")
        scoped_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        lifecycle = PostgresMaintenanceCapabilityStore(scoped_store)
        capability_id = f"mcap:{uuid.uuid4()}"
        lifecycle.prepare(capability_id=capability_id, request_id=str(uuid.uuid4()), envelope=envelope(), actor="operator", at=AT)
        lifecycle.append_recovery_record(capability_id=capability_id, record_id=str(uuid.uuid4()), kind="observation", payload_ref="artifact:sha256:" + "3" * 64, actor="observer", at=AT)
        for statement in (
            "UPDATE maintenance_capability_receipt SET actor='tampered' WHERE repo_id=%s",
            "DELETE FROM maintenance_capability_receipt WHERE repo_id=%s",
            "UPDATE maintenance_capability_recovery SET actor='tampered' WHERE repo_id=%s",
            "DELETE FROM maintenance_capability_recovery WHERE repo_id=%s",
        ):
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                with scoped_store.conn.cursor() as cur:
                    cur.execute(statement, (repo_id,))
            scoped_store.conn.rollback()

class TestRemoteSafety:
    def test_superseded_marker_is_read_from_disposable_temp_table(self, store):
        """The marker probe is read-only; the temporary fixture disappears with this connection."""
        with store.conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE superseded_marker (message text NOT NULL)")
            cur.execute(
                "INSERT INTO superseded_marker(message) VALUES (%s)",
                ("this database was superseded; use served mode",),
            )
        try:
            assert pg.superseded_marker_message(store) == "this database was superseded; use served mode"
        finally:
            with store.conn.cursor() as cur:
                cur.execute("DROP TABLE superseded_marker")


class TestInitDb:
    def test_idempotent(self, store):
        pg.init_db(store)
        pg.init_db(store)
        sprint_id = pg.create_sprint(store, f"Init-{_uid()}", "G", status="active")
        assert pg.get_sprint(store, sprint_id) is not None

    def test_concurrent_migration_serializes_on_disposable_postgres(self, pg_test_scope, store):
        """Two migration jobs admit one v1 -> v3 transition without deadlock."""
        schema = "migration_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'CREATE TABLE "{schema}".schema_version (version integer NOT NULL)')
            cur.execute(f'INSERT INTO "{schema}".schema_version VALUES (1)')
        store.conn.commit()
        connections = [psycopg.connect(_PG_URL, row_factory=dict_row) for _ in range(2)]
        for conn in connections:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()

        barrier = threading.Barrier(3)
        errors = []
        results = []

        def migrate(conn, repo_id):
            try:
                barrier.wait(timeout=15)
                results.append(pg.migrate_schema(pg.PgStore(conn=conn, repo_id=repo_id)))
            except BaseException as exc:  # Preserve a worker failure for assertion.
                errors.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=migrate, args=(conn, pg_test_scope("migration")))
            for conn in connections
        ]
        try:
            for thread in threads:
                thread.start()
            barrier.wait(timeout=15)
            for thread in threads:
                thread.join(timeout=60)

            assert not any(thread.is_alive() for thread in threads)
            assert not errors
            assert sorted(result["applied_versions"] for result in results) == [[], [2, 3, 4, 5, 6]]
            with store.conn.cursor() as cur:
                cur.execute(f'SELECT version FROM "{schema}".schema_version')
                assert cur.fetchone()["version"] == 6
            store.conn.rollback()
        finally:
            with store.conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    def test_failed_migration_releases_lock_for_concurrent_retry(self, store):
        schema = "migration_fault_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
        store.conn.commit()
        connections = [psycopg.connect(_PG_URL, row_factory=dict_row) for _ in range(2)]
        for conn in connections:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
        lock_acquired = threading.Event()
        retry_invoked = threading.Event()
        failures = []
        results = []

        def fail_after_schema_work():
            conn = connections[0]
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        pg._SCHEMA_BOOTSTRAP_LOCK_KEYS,
                    )
                    lock_acquired.set()
                    assert retry_invoked.wait(timeout=15)
                    pg._apply_schema_version_3(cur)
                    raise RuntimeError("injected failure before ledger advance")
            except RuntimeError as exc:
                failures.append(exc)
                conn.rollback()
            finally:
                conn.close()

        def retry_migration():
            conn = connections[1]
            retry_invoked.set()
            try:
                results.append(pg.migrate_schema(pg.PgStore(conn, "migration-retry")))
            finally:
                conn.close()

        failing = threading.Thread(target=fail_after_schema_work)
        retrying = threading.Thread(target=retry_migration)
        try:
            failing.start()
            assert lock_acquired.wait(timeout=15)
            retrying.start()
            failing.join(timeout=30)
            retrying.join(timeout=30)

            assert not failing.is_alive()
            assert not retrying.is_alive()
            assert [str(exc) for exc in failures] == [
                "injected failure before ledger advance"
            ]
            assert [result["applied_versions"] for result in results] == [[3, 4, 5, 6]]
            with store.conn.cursor() as cur:
                cur.execute(f'SELECT version FROM "{schema}".schema_version')
                assert cur.fetchone()["version"] == 6
            store.conn.rollback()
        finally:
            for conn in connections:
                if not conn.closed:
                    conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    def test_production_like_role_is_rejected_server_side(self, store):
        guard_url = os.environ.get("SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL")
        if not guard_url:
            pytest.skip("SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL not set")

        probe_role = "sprintctl_production_probe"
        with store.conn.cursor() as cur:
            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {probe_role}")
            cur.execute(f"GRANT SELECT ON schema_version TO {probe_role}")
            cur.execute(f"GRANT INSERT ON ingest_stream TO {probe_role}")
        store.conn.commit()
        try:
            probe = psycopg.connect(guard_url, row_factory=dict_row)
            try:
                handshake = pg.require_compatible_schema(
                    pg.PgStore(conn=probe, repo_id="runtime-probe")
                )
                assert handshake["compatible"] is True
                probe.rollback()
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with probe.cursor() as cur:
                        cur.execute("CREATE TABLE runtime_role_must_not_create_tables (id integer)")
                probe.rollback()
                with pytest.raises(
                    psycopg.errors.InsufficientPrivilege,
                    match="dedicated disposable sprintctl test role and database",
                ):
                    with probe.cursor() as cur:
                        cur.execute(
                            "INSERT INTO ingest_stream "
                            "(repo_id, origin_stream_id) VALUES (%s, %s)",
                            (new_test_repo_id("production-guard"), uuid.uuid4().hex),
                        )
                probe.rollback()
            finally:
                probe.close()
        finally:
            with store.conn.cursor() as cur:
                cur.execute(f"REVOKE INSERT ON ingest_stream FROM {probe_role}")
                cur.execute(f"REVOKE SELECT ON schema_version FROM {probe_role}")
                cur.execute(f"REVOKE USAGE ON SCHEMA public FROM {probe_role}")
            store.conn.commit()

    def test_phase26_ingest_schema_upgrades_before_authority_foreign_keys(
        self, pg_test_scope
    ):
        schema = "phase26_" + uuid.uuid4().hex
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
                cur.execute(
                    """
                    CREATE TABLE ingest_stream (
                        repo_id text NOT NULL,
                        origin_stream_id text NOT NULL,
                        highest_origin_seq bigint NOT NULL DEFAULT 0,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (repo_id, origin_stream_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE ingest_record (
                        ingest_offset bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
                        repo_id text NOT NULL,
                        origin_stream_id text NOT NULL,
                        origin_seq bigint NOT NULL CHECK (origin_seq > 0),
                        event_id text NOT NULL,
                        schema_version integer NOT NULL CHECK (schema_version > 0),
                        record_class text NOT NULL CHECK (record_class = 'observation'),
                        event_type text NOT NULL,
                        actor text NOT NULL,
                        runtime_session_id text,
                        occurred_at timestamptz NOT NULL,
                        basis_revision text,
                        correlation_id text,
                        causation_id text,
                        payload jsonb NOT NULL,
                        payload_sha256 text NOT NULL,
                        record_sha256 text NOT NULL,
                        ingested_at timestamptz NOT NULL DEFAULT now(),
                        UNIQUE (repo_id, origin_stream_id, origin_seq),
                        UNIQUE (repo_id, event_id),
                        FOREIGN KEY (repo_id, origin_stream_id)
                            REFERENCES ingest_stream(repo_id, origin_stream_id)
                            ON DELETE CASCADE
                    )
                    """
                )
            conn.commit()

            upgraded = pg.PgStore(
                conn=conn,
                repo_id=pg_test_scope("phase26-upgrade"),
                authority_repo_uuid=str(uuid.uuid4()),
            )
            pg.init_db(upgraded)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conname IN ('authority_decision_request_fk', "
                    "'authority_decision_record_fk') "
                    "AND conrelid = 'authority_decision'::regclass ORDER BY conname"
                )
                assert [row["conname"] for row in cur.fetchall()] == [
                    "authority_decision_record_fk",
                    "authority_decision_request_fk",
                ]
                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = %s "
                    "AND indexname = 'idx_ingest_record_repo_offset_unique'",
                    (schema,),
                )
                assert cur.fetchone() is not None
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()

    @pytest.mark.parametrize(
        ("legacy_version", "applied_versions"),
        [(1, [2, 3, 4, 5, 6]), (2, [3, 4, 5, 6])],
    )
    def test_interleaved_legacy_offsets_backfill_per_repository_and_translate_fk(
        self, pg_test_scope, legacy_version, applied_versions
    ):
        schema = "cursor_backfill_" + uuid.uuid4().hex
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        repo_a = pg_test_scope(f"cursor-backfill-v{legacy_version}-a")
        repo_b = pg_test_scope(f"cursor-backfill-v{legacy_version}-b")
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
                cur.execute("CREATE TABLE schema_version (version integer NOT NULL)")
                cur.execute("INSERT INTO schema_version VALUES (%s)", (legacy_version,))
                cur.execute(
                    """
                    CREATE TABLE ingest_stream (
                        repo_id text NOT NULL,
                        origin_stream_id text NOT NULL,
                        highest_origin_seq bigint NOT NULL DEFAULT 0,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (repo_id, origin_stream_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE ingest_record (
                        repo_id text NOT NULL,
                        ingest_offset bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        origin_stream_id text NOT NULL,
                        origin_seq bigint NOT NULL,
                        event_id text NOT NULL,
                        schema_version integer NOT NULL DEFAULT 1,
                        record_class text NOT NULL DEFAULT 'observation',
                        event_type text NOT NULL DEFAULT 'note.recorded',
                        actor text NOT NULL DEFAULT 'legacy',
                        runtime_session_id text,
                        occurred_at timestamptz NOT NULL DEFAULT now(),
                        basis_revision text,
                        correlation_id text,
                        causation_id text,
                        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                        payload_sha256 text NOT NULL DEFAULT 'payload',
                        record_sha256 text NOT NULL,
                        producer_created_at timestamptz NOT NULL DEFAULT now(),
                        ingested_at timestamptz NOT NULL DEFAULT now(),
                        UNIQUE (repo_id, origin_stream_id, origin_seq),
                        UNIQUE (repo_id, event_id),
                        UNIQUE (repo_id, ingest_offset),
                        FOREIGN KEY (repo_id, origin_stream_id)
                            REFERENCES ingest_stream(repo_id, origin_stream_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE authority_decision (
                        repo_id text NOT NULL,
                        request_event_id text NOT NULL,
                        request_record_sha256 text NOT NULL,
                        decision_event_id text NOT NULL,
                        decision_ingest_offset bigint NOT NULL,
                        outcome text NOT NULL,
                        reason_code text,
                        reason_detail text,
                        effect jsonb NOT NULL DEFAULT '{}'::jsonb,
                        decided_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (repo_id, request_event_id),
                        UNIQUE (repo_id, decision_event_id),
                        UNIQUE (repo_id, decision_ingest_offset)
                    )
                    """
                )
                # legacy_version=1 replays the full canonical PG_DDL, which
                # creates this table with CREATE TABLE IF NOT EXISTS; for
                # legacy_version=2 that replay is skipped, so this stub
                # stands in for what a real version-2+ deployment already
                # has. Schema version 4 (_apply_schema_version_4) only
                # widens its ref_type CHECK constraint.
                cur.execute(
                    """
                    CREATE TABLE ref (
                        repo_id      text        NOT NULL,
                        id           bigint      GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                        work_item_id bigint      NOT NULL,
                        ref_type     text        NOT NULL DEFAULT 'other'
                                                  CHECK (ref_type IN (
                                                      'pr', 'issue', 'doc', 'other',
                                                      'file', 'glob', 'manifest'
                                                  )),
                        url          text        NOT NULL DEFAULT '',
                        label        text        NOT NULL DEFAULT '',
                        created_at   timestamptz NOT NULL DEFAULT now(),
                        UNIQUE (repo_id, id)
                    )
                    """
                )
                for repo_id in (repo_a, repo_b):
                    cur.execute(
                        "INSERT INTO ingest_stream "
                        "(repo_id, origin_stream_id, highest_origin_seq) "
                        "VALUES (%s, %s, 2)",
                        (repo_id, f"stream:{repo_id}"),
                    )
                for repo_id, sequence in (
                    (repo_a, 1),
                    (repo_b, 1),
                    (repo_a, 2),
                    (repo_b, 2),
                ):
                    cur.execute(
                        "INSERT INTO ingest_record "
                        "(repo_id, origin_stream_id, origin_seq, event_id, record_sha256) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            repo_id,
                            f"stream:{repo_id}",
                            sequence,
                            f"event:{repo_id}:{sequence}",
                            f"digest:{repo_id}:{sequence}",
                        ),
                    )
                cur.execute(
                    "INSERT INTO authority_decision "
                    "(repo_id, request_event_id, request_record_sha256, "
                    "decision_event_id, decision_ingest_offset, outcome) "
                    "VALUES (%s, %s, 'request-digest', %s, 4, 'accepted')",
                    (repo_b, f"event:{repo_b}:1", f"event:{repo_b}:2"),
                )
            conn.commit()

            migrated = pg.migrate_schema(pg.PgStore(conn, repo_a))

            assert migrated["applied_versions"] == applied_versions
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT repo_id, ingest_id, ingest_offset FROM ingest_record "
                    "ORDER BY ingest_id"
                )
                rows = cur.fetchall()
                assert [int(row["ingest_id"]) for row in rows] == [1, 2, 3, 4]
                assert [
                    int(row["ingest_offset"])
                    for row in rows
                    if row["repo_id"] == repo_a
                ] == [1, 2]
                assert [
                    int(row["ingest_offset"])
                    for row in rows
                    if row["repo_id"] == repo_b
                ] == [1, 2]
                cur.execute(
                    "SELECT decision_ingest_offset FROM authority_decision "
                    "WHERE repo_id = %s",
                    (repo_b,),
                )
                assert int(cur.fetchone()["decision_ingest_offset"]) == 2
                cur.execute(
                    "SELECT highest_offset FROM ingest_repo_cursor "
                    "WHERE repo_id = %s",
                    (repo_a,),
                )
                assert int(cur.fetchone()["highest_offset"]) == 2
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE authority_decision SET decision_ingest_offset = 3 "
                        "WHERE repo_id = %s",
                        (repo_b,),
                    )
            conn.rollback()
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()


# ---------------------------------------------------------------------------
# Producer outbox ingestion
# ---------------------------------------------------------------------------

class TestProducerOutboxIngestion:
    def _records(self, tmp_path, count=2):
        conn = outbox.open_outbox(tmp_path / "producer-outbox.db")
        try:
            return [
                outbox.append_observation(
                    conn,
                    event_type="work.completed",
                    actor="producer-a",
                    payload={"index": index},
                    event_id=f"ingest-{uuid.uuid4().hex}-{index}",
                    occurred_at="2026-07-14T12:00:00Z",
                )
                for index in range(count)
            ]
        finally:
            conn.close()

    def test_retry_returns_original_offset_and_cursor_orders_new_records(self, store, tmp_path):
        first, second = self._records(tmp_path)

        admitted = pg.ingest_records(store, [first, second])
        retried = pg.ingest_records(store, [first, second])
        ordered = pg.list_ingested_records(store)

        assert [result.duplicate for result in admitted] == [False, False]
        assert [result.duplicate for result in retried] == [True, True]
        assert [result.ingest_offset for result in retried] == [result.ingest_offset for result in admitted]
        assert [result.ingest_offset for result in ordered] == [result.ingest_offset for result in admitted]
        assert [result.record.created_at for result in ordered] == [first.created_at, second.created_at]
        assert ordered[1].ingest_offset > ordered[0].ingest_offset

    def test_interleaved_repositories_each_publish_contiguous_offsets(
        self, pg_test_scope, store, tmp_path
    ):
        repo_a = pg.PgStore(store.conn, pg_test_scope("cursor-interleaved-a"))
        repo_b = pg.PgStore(store.conn, pg_test_scope("cursor-interleaved-b"))
        first_a, second_a = self._records(tmp_path / "repo-a")
        first_b, second_b = self._records(tmp_path / "repo-b")

        outcomes = [
            pg.ingest_records(repo_a, [first_a])[0],
            pg.ingest_records(repo_b, [first_b])[0],
            pg.ingest_records(repo_a, [second_a])[0],
            pg.ingest_records(repo_b, [second_b])[0],
        ]

        assert [outcomes[index].ingest_offset for index in (0, 2)] == [1, 2]
        assert [outcomes[index].ingest_offset for index in (1, 3)] == [1, 2]
        assert pg.get_ingest_high_water(repo_a) == 2
        assert pg.get_ingest_high_water(repo_b) == 2

    def test_concurrent_streams_in_one_repository_allocate_offsets_one_and_two(
        self, pg_test_scope, store, tmp_path
    ):
        repo_id = pg_test_scope("cursor-concurrent-same-repo")
        records = [
            self._records(tmp_path / f"same-repo-{index}", count=1)[0]
            for index in range(2)
        ]
        barrier = threading.Barrier(3)
        outcomes = []
        failures = []

        def worker(record):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent = pg.PgStore(conn, repo_id)
            try:
                barrier.wait(timeout=15)
                outcomes.append(pg.ingest_records(independent, [record])[0])
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(record,)) for record in records]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=15)
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        assert {outcome.ingest_offset for outcome in outcomes} == {1, 2}

    def test_concurrent_first_appends_use_public_one_and_distinct_internal_ids(
        self, pg_test_scope, store, tmp_path
    ):
        repo_ids = [
            pg_test_scope("cursor-concurrent-repo-a"),
            pg_test_scope("cursor-concurrent-repo-b"),
        ]
        records = [
            self._records(tmp_path / f"first-repo-{index}", count=1)[0]
            for index in range(2)
        ]
        barrier = threading.Barrier(3)
        outcomes = []
        failures = []

        def worker(repo_id, record):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent = pg.PgStore(conn, repo_id)
            try:
                barrier.wait(timeout=15)
                outcome = pg.ingest_records(independent, [record])[0]
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT ingest_id FROM ingest_record "
                        "WHERE repo_id = %s AND event_id = %s",
                        (repo_id, record.event_id),
                    )
                    ingest_id = int(cur.fetchone()["ingest_id"])
                outcomes.append((repo_id, outcome.ingest_offset, ingest_id))
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=pair)
            for pair in zip(repo_ids, records, strict=True)
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=15)
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        assert [offset for _repo, offset, _ingest_id in outcomes] == [1, 1]
        assert len({ingest_id for _repo, _offset, ingest_id in outcomes}) == 2

    def test_lost_response_retry_does_not_advance_repository_cursor(
        self, pg_test_scope, store, tmp_path
    ):
        isolated = pg.PgStore(store.conn, pg_test_scope("cursor-lost-response"))
        first, second = self._records(tmp_path / "lost-response")

        admitted = pg.ingest_records(isolated, [first])[0]
        retried = pg.ingest_records(isolated, [first])[0]
        following = pg.ingest_records(isolated, [second])[0]

        assert (admitted.ingest_offset, retried.ingest_offset) == (1, 1)
        assert retried.duplicate is True
        assert following.ingest_offset == 2
        assert pg.get_ingest_high_water(isolated) == 2

    def test_failure_after_offset_allocation_rolls_back_cursor_and_record(
        self, pg_test_scope, store, tmp_path
    ):
        repo_id = pg_test_scope("cursor-rollback")
        isolated = pg.PgStore(store.conn, repo_id)
        first = self._records(tmp_path / "cursor-rollback", count=1)[0]
        suffix = uuid.uuid4().hex
        function_name = f"reject_cursor_record_{suffix}"
        trigger_name = f"reject_cursor_record_{suffix}"
        with store.conn.cursor() as cur:
            cur.execute(
                f"CREATE FUNCTION {function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
                "'injected post-allocation failure'; END; $$"
            )
            cur.execute(
                f"CREATE TRIGGER {trigger_name} AFTER INSERT ON ingest_record "
                f"FOR EACH ROW WHEN (NEW.repo_id = '{repo_id}') "
                f"EXECUTE FUNCTION {function_name}()"
            )
        store.conn.commit()
        try:
            with pytest.raises(Exception, match="injected post-allocation failure"):
                pg.ingest_records(isolated, [first])
            assert pg.get_ingest_high_water(isolated) == 0
            assert pg.list_ingested_records(isolated) == []
        finally:
            with store.conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON ingest_record")
                cur.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
            store.conn.commit()

        admitted = pg.ingest_records(isolated, [first])[0]
        assert admitted.ingest_offset == 1

    def test_item_evidence_ingest_deduplicates_stale_basis_without_status_mutation(
        self, store, tmp_path
    ):
        sprint_id = pg.create_sprint(store, f"Evidence-ingest-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Evidence-item-{_uid()}")
        pg.set_work_item_status(store, item_id, "active")
        item_before = pg.get_work_item(store, item_id)

        producer = outbox.open_outbox(tmp_path / "item-evidence.db")
        try:
            record = observations.append_item_evidence_observation(
                producer,
                event_type=observations.WORK_COMPLETED,
                actor="session-wrapper",
                repo_id=store.repo_id,
                sprint_id=sprint_id,
                work_item_id=item_id,
                runtime_session_id="session-pg-evidence",
                summary="Work completed while offline",
                evidence_refs=[
                    {
                        "kind": "git-commit",
                        "source": f"repo:{store.repo_id}",
                        "revision": "a" * 40,
                    }
                ],
                basis_revision=authority.item_revision(item_before),
                authored_at="2026-07-19T12:00:00Z",
            )
        finally:
            producer.close()

        pg.set_work_item_status(store, item_id, "done")
        current = pg.get_work_item(store, item_id)
        admitted = pg.ingest_records(store, [record])
        retried = pg.ingest_records(store, [record])
        projected = observations.project_item_evidence(
            retried[0].record,
            current_revision=authority.item_revision(current),
        )

        assert admitted[0].duplicate is False
        assert retried[0].duplicate is True
        assert retried[0].ingest_offset == admitted[0].ingest_offset
        assert projected.basis.classification is observations.BasisClassification.ANACHRONISTIC
        assert projected.runtime_session_id == "session-pg-evidence"
        assert pg.get_work_item(store, item_id)["status"] == "done"

    def test_changed_producer_timestamp_conflicts_with_existing_origin_tuple(self, store, tmp_path):
        first = self._records(tmp_path, count=1)[0]
        pg.ingest_records(store, [first])

        with pytest.raises(pg.IngestConflictError, match="different record"):
            pg.ingest_records(
                store,
                [replace(first, created_at="2026-07-14T12:00:01Z")],
            )

    def test_gap_rejects_whole_batch_without_advancing_stream(self, store, tmp_path):
        first, second = self._records(tmp_path)
        before_offset = max(
            (result.ingest_offset for result in pg.list_ingested_records(store)), default=0
        )

        with pytest.raises(pg.IngestGapError, match="expected sequence 1, received 2"):
            pg.ingest_records(store, [second])
        assert pg.list_ingested_records(store, after_offset=before_offset) == []

        admitted = pg.ingest_records(store, [first, second])
        assert [result.record.origin_seq for result in admitted] == [1, 2]

    def test_seed_ingest_stream_lets_a_stranded_producer_resume(self, store, tmp_path):
        """A producer whose remote ledger history was lost (e.g. by a
        ledger-introducing/resetting schema migration -- see sprintctl-work
        schema v4's repository_ingest_cursor rollout, 2026-07-24) can never
        admit another record: the remote permanently expects sequence 1
        while the producer permanently sends whatever it already has queued
        locally. ``seed_ingest_stream`` is the explicit, operator-invoked
        recovery for exactly that, and does not weaken ordinary gap
        detection for anyone else.
        """
        stranded_stream_id = str(uuid.uuid4())
        first, second = self._records(tmp_path)
        stranded_second = replace(second, origin_stream_id=stranded_stream_id, origin_seq=7)

        with pytest.raises(pg.IngestGapError, match="expected sequence 1, received 7"):
            pg.ingest_records(store, [stranded_second])

        pg.seed_ingest_stream(store, stranded_stream_id, 6)

        admitted = pg.ingest_records(store, [stranded_second])
        assert admitted[0].record.origin_seq == 7
        assert admitted[0].duplicate is False

        with pytest.raises(pg.IngestConflictError, match="already has ingest history"):
            pg.seed_ingest_stream(store, stranded_stream_id, 100)

    def test_concurrent_same_stream_retry_admits_once(self, store, tmp_path):
        record = self._records(tmp_path, count=1)[0]
        before_offset = max(
            (result.ingest_offset for result in pg.list_ingested_records(store)), default=0
        )
        barrier = threading.Barrier(3)
        outcomes = []
        failures = []

        def worker():
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                barrier.wait()
                outcomes.append(pg.ingest_records(independent_store, [record])[0])
            except Exception as exc:  # pragma: no cover - asserted through failures
                failures.append(exc)
            finally:
                conn.close()

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        barrier.wait()
        for worker_thread in workers:
            worker_thread.join()

        assert failures == []
        assert len(outcomes) == 2
        assert {outcome.ingest_offset for outcome in outcomes} == {outcomes[0].ingest_offset}
        assert sorted(outcome.duplicate for outcome in outcomes) == [False, True]
        assert len(pg.list_ingested_records(store, after_offset=before_offset)) == 1

    def test_disposable_remote_history_rebuilds_projection(
        self, pg_test_scope, tmp_path
    ):
        repo_id = pg_test_scope("projection-rebuild")
        rebuild_store = pg.PgStore(
            conn=psycopg.connect(_PG_URL, row_factory=dict_row),
            repo_id=repo_id,
        )
        pg.init_db(rebuild_store)
        try:
            records = self._records(tmp_path, count=4)
            admitted = pg.ingest_records(rebuild_store, records)
            remote_history = pg.list_ingested_records(rebuild_store)
            cache = projection.open_cached_projection(tmp_path / "remote-rebuild.db")
            try:
                projection.apply_ingested_records(
                    cache,
                    [sync._cached_record(record) for record in remote_history],
                )

                assert projection.get_watermark(cache).ingest_offset == admitted[-1].ingest_offset
                assert [
                    record.record["event_id"]
                    for record in projection.list_cached_records(cache)
                ] == [record.event_id for record in records]
            finally:
                cache.close()
        finally:
            rebuild_store.conn.close()


# ---------------------------------------------------------------------------
# Sprint
# ---------------------------------------------------------------------------

class TestSprint:
    def test_create_and_get(self, store):
        sid = pg.create_sprint(store, "SprintA", "G", "2026-01-01", "2026-12-31", "active")
        row = pg.get_sprint(store, sid)
        assert row is not None
        assert row["name"] == "SprintA"
        assert row["repo_id"] == store.repo_id

    def test_get_missing_returns_none(self, store):
        assert pg.get_sprint(store, 9_999_999) is None

    def test_get_active_sprint(self, store):
        pg.create_sprint(store, f"Active-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        result = pg.get_active_sprint(store)
        assert result is not None

    def test_list_active_sprints(self, store):
        pg.create_sprint(store, f"La-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        rows = pg.list_active_sprints(store)
        assert isinstance(rows, list)
        assert all(r["repo_id"] == store.repo_id for r in rows)

    def test_list_sprints(self, store):
        rows = pg.list_sprints(store)
        assert isinstance(rows, list)

    def test_set_sprint_status_active_to_closed(self, store):
        sid = pg.create_sprint(store, f"St-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        pg.set_sprint_status(store, sid, "closed")
        assert pg.get_sprint(store, sid)["status"] == "closed"

    def test_explicit_close_appends_boundary_event_atomically(self, store):
        sid = pg.create_sprint(
            store,
            f"Cb-{_uid()}",
            "G",
            "2026-01-01",
            "2026-12-31",
            "active",
        )

        event_id = pg.close_sprint_with_boundary_event(store, sid, "operator")

        assert pg.get_sprint(store, sid)["status"] == "closed"
        event = next(
            event for event in pg.list_events(store, sid) if event["id"] == event_id
        )
        assert event["event_type"] == "sprint-close-boundary"
        assert event["actor"] == "operator"
        assert json.loads(event["payload"]) == {
            "previous_status": "active",
            "status": "closed",
        }

    def test_explicit_close_rolls_back_when_boundary_insert_fails(self, store, monkeypatch):
        sid = pg.create_sprint(
            store,
            f"Cr-{_uid()}",
            "G",
            "2026-01-01",
            "2026-12-31",
            "active",
        )

        def fail_insert(*args, **kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(pg, "_insert_event", fail_insert)
        with pytest.raises(RuntimeError, match="injected event failure"):
            pg.close_sprint_with_boundary_event(store, sid, "operator")

        assert pg.get_sprint(store, sid)["status"] == "active"
        assert pg.list_events(store, sid) == []

    def test_set_sprint_kind(self, store):
        sid = pg.create_sprint(store, f"Sk-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        pg.set_sprint_kind(store, sid, "backlog")
        assert pg.get_sprint(store, sid)["kind"] == "backlog"

    def test_timestamps_are_iso_strings(self, store):
        sid = pg.create_sprint(store, f"Ts-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        row = pg.get_sprint(store, sid)
        assert isinstance(row["created_at"], str)
        assert "T" in row["created_at"]


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------

class TestTrack:
    def test_get_or_create_idempotent(self, store, sprint_id):
        tid = pg.get_or_create_track(store, sprint_id, "eng")
        assert pg.get_or_create_track(store, sprint_id, "eng") == tid

    def test_list_tracks(self, store, sprint_id):
        pg.get_or_create_track(store, sprint_id, f"trk-{_uid()}")
        tracks = pg.list_tracks(store, sprint_id)
        assert len(tracks) >= 1

    def test_get_track(self, store, sprint_id):
        tid = pg.get_or_create_track(store, sprint_id, f"gt-{_uid()}")
        track = pg.get_track(store, tid)
        assert track is not None
        assert track["sprint_id"] == sprint_id


# ---------------------------------------------------------------------------
# WorkItem
# ---------------------------------------------------------------------------

class TestWorkItem:
    def test_create_and_get(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "WI title", "desc")
        item = pg.get_work_item(store, iid)
        assert item is not None
        assert item["title"] == "WI title"
        assert item["description"] == "desc"
        assert item["status"] == "pending"

    def test_update_description(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "Editable", "Old scope")
        current = pg.get_work_item_with_edit_revision(store, iid)
        assert current is not None

        edited = pg.update_work_item_description(
            store,
            iid,
            "New shaped scope",
            expected_revision=current[1],
            actor="pg-editor",
        )

        assert pg.get_work_item(store, iid)["description"] == "New shaped scope"
        assert edited["previous_revision"] == current[1]
        assert edited["revision"] != current[1]
        with pytest.raises(db.EditConflict, match="revision mismatch"):
            pg.update_work_item_description(
                store,
                iid,
                "Stale scope",
                expected_revision=current[1],
                actor="stale-editor",
            )
        events = pg.list_events(store, sprint_id)
        audit = next(event for event in events if event["id"] == edited["event_id"])
        assert audit["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
        assert audit["actor"] == "pg-editor"
        assert set(json.loads(audit["payload"])) == {
            "summary",
            "field",
            "previous_description",
            "description",
            "previous_revision",
            "revision",
        }

    def test_edit_rolls_back_when_audit_insert_fails(
        self, store, sprint_id, track_id, monkeypatch
    ):
        iid = pg.create_work_item(
            store, sprint_id, track_id, "Rollback edit", "Original scope"
        )
        current = pg.get_work_item_with_edit_revision(store, iid)
        assert current is not None

        def fail_event_insert(*args, **kwargs):
            raise RuntimeError("injected pg audit insert failure")

        monkeypatch.setattr(pg, "_insert_event", fail_event_insert)
        with pytest.raises(RuntimeError, match="injected pg audit insert failure"):
            pg.update_work_item_description(
                store,
                iid,
                "Must roll back",
                expected_revision=current[1],
                actor="pg-editor",
            )

        assert pg.get_work_item(store, iid)["description"] == "Original scope"
        assert not [
            event
            for event in pg.list_events(store, sprint_id)
            if event["work_item_id"] == iid
        ]

    def test_overlapping_edit_writers_accept_exactly_one_revision(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(
            store, sprint_id, track_id, "Concurrent edit", "Original scope"
        )
        current = pg.get_work_item_with_edit_revision(store, iid)
        assert current is not None
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []

        def edit(label: str) -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            assert_disposable_connection(conn)
            independent = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                barrier.wait(timeout=10)
                try:
                    pg.update_work_item_description(
                        independent,
                        iid,
                        f"{label} scope",
                        expected_revision=current[1],
                        actor=label,
                    )
                except db.EditConflict:
                    outcomes.append((label, "conflict"))
                else:
                    outcomes.append((label, "accepted"))
            finally:
                conn.close()

        threads = [
            threading.Thread(target=edit, args=("writer-a",)),
            threading.Thread(target=edit, args=("writer-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcome for _label, outcome in outcomes) == [
            "accepted",
            "conflict",
        ]

        item = pg.get_work_item(store, iid)
        assert item is not None
        accepted_actor = next(
            label for label, outcome in outcomes if outcome == "accepted"
        )
        assert item["description"] == f"{accepted_actor} scope"
        edits = [
            event
            for event in pg.list_events(store, sprint_id)
            if event["work_item_id"] == iid
            and event["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
        ]
        assert len(edits) == 1
        assert edits[0]["actor"] == accepted_actor

    def test_update_description_validation_and_missing_item(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, "Validated", "Keep scope")

        with pytest.raises(ValueError, match="non-whitespace"):
            pg.update_work_item_description(store, iid, "  \n  ")
        with pytest.raises(ValueError, match="NUL"):
            pg.update_work_item_description(store, iid, "invalid\x00scope")
        with pytest.raises(ValueError, match="Item #999999999 not found"):
            pg.update_work_item_description(store, 999_999_999, "Valid scope")
        assert pg.get_work_item(store, iid)["description"] == "Keep scope"

    def test_get_missing_returns_none(self, store):
        assert pg.get_work_item(store, 9_999_999) is None

    def test_list_by_sprint(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Li-{_uid()}")
        items = pg.list_work_items(store, sprint_id=sprint_id)
        assert any(i["id"] == iid for i in items)

    def test_list_by_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ls-{_uid()}")
        pending = pg.list_work_items(store, sprint_id=sprint_id, status="pending")
        assert any(i["id"] == iid for i in pending)

    def test_set_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ss-{_uid()}")
        pg.set_work_item_status(store, iid, "active")
        assert pg.get_work_item(store, iid)["status"] == "active"

    def test_set_status_invalid_transition_raises(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Inv-{_uid()}")
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, iid, "done")  # pending → done not allowed

    def test_claimed_item_requires_proof_for_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cp-{_uid()}")
        claim_id = pg.create_claim(store, iid, "ag", ttl_seconds=300)
        claim = pg.get_claim(store, claim_id, include_secret=True)
        token = claim["claim_token"]
        pg.set_work_item_status(store, iid, "active", claim_id=claim_id, claim_token=token)
        with pytest.raises(ClaimConflict):
            pg.set_work_item_status(store, iid, "done")


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class TestEvent:
    def test_generic_api_cannot_forge_close_boundary(self, store, sprint_id):
        with pytest.raises(ValueError, match="reserved; use the atomic sprint close"):
            pg.create_event(
                store,
                sprint_id,
                "forger",
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                payload={"previous_status": "active", "status": "closed"},
            )

    def test_capability_pointer_requires_closed_sprint_and_local_boundary(
        self,
        store,
        sprint_id,
    ):
        receipt_bytes = _receipt_bytes(store, sprint_id, 1)
        with pytest.raises(ValueError, match="requires a closed sprint"):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=_receipt_payload(store, receipt_bytes),
            )

    def test_create_and_list(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "hi"})
        events = pg.list_events(store, sprint_id)
        assert any(e["event_type"] == "note" for e in events)

    def test_payload_is_string(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "str-check"})
        events = pg.list_events(store, sprint_id)
        note = next(e for e in reversed(events) if e["event_type"] == "note")
        assert isinstance(note["payload"], str)
        assert json.loads(note["payload"])["summary"] is not None

    def test_capability_receipt_pointer_matches_sqlite_contract(
        self,
        store,
        sprint_id,
        monkeypatch,
    ):
        boundary_event_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_event_id)
        payload = _receipt_payload(store, receipt_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: receipt_bytes,
        )

        event_id = pg.create_event(
            store,
            sprint_id,
            "drafting-agent",
            "capability-receipt-drafted",
            payload=payload,
        )

        event = next(
            event
            for event in pg.list_events(store, sprint_id)
            if event["id"] == event_id
        )
        assert json.loads(event["payload"]) == payload

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda payload: payload.update(receipt_id="other.receipt"), "must start"),
            (lambda payload: payload.update(receipt_path="/tmp/receipt.json"), "must be exactly"),
            (lambda payload: payload.update(receipt_sha256="A" * 64), "lowercase hexadecimal"),
            (lambda payload: payload.update(receipt_body={"private": True}), "unknown fields"),
            (
                lambda payload: payload.update(
                    project="other",
                    receipt_id="other.receipt",
                    receipt_path=(
                        "/projects/dev/_artifacts/other/capability/receipts/"
                        "other.receipt.json"
                    ),
                ),
                "owning repository",
            ),
        ],
    )
    def test_malformed_capability_pointer_matches_sqlite_rejection(
        self,
        store,
        sprint_id,
        mutate,
        message,
    ):
        receipt_bytes = _receipt_bytes(store, sprint_id, 1)
        payload = _receipt_payload(store, receipt_bytes)
        mutate(payload)

        with pytest.raises(ValueError, match=message):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )

    def test_capability_pointer_file_and_boundary_are_verified_before_insert(
        self,
        store,
        sprint_id,
        monkeypatch,
    ):
        boundary_event_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        wrong_revision_bytes = _receipt_bytes(
            store,
            sprint_id,
            boundary_event_id,
            boundary={
                "kind": "sprint-close",
                "ref": {
                    "kind": "sprint-event",
                    "source": f"sprintctl:{store.repo_id}:sprint:{sprint_id}",
                    "revision": f"event:{boundary_event_id + 1}",
                },
            },
        )
        payload = _receipt_payload(store, wrong_revision_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: wrong_revision_bytes,
        )

        with pytest.raises(ValueError, match="boundary.ref.revision"):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )
        assert not any(
            event["event_type"] == contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
            for event in pg.list_events(store, sprint_id)
        )

    def test_list_events_limited(self, store, sprint_id):
        for _ in range(4):
            pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                            payload={"summary": "x"})
        limited = pg.list_events_limited(store, sprint_id, limit=2)
        assert len(limited) == 2

    def test_list_knowledge_candidates(self, store, sprint_id, work_item_id):
        pg.create_event(store, sprint_id, "ag", "decision", source_type="actor",
                        work_item_id=work_item_id, payload={"summary": "chose X"})
        candidates = pg.list_knowledge_candidates(store, sprint_id)
        assert any(c["event_type"] == "decision" for c in candidates)
        assert all(isinstance(c["payload"], dict) for c in candidates)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

class TestClaim:
    def test_create_and_get(self, store, work_item_id):
        cid = pg.create_claim(store, work_item_id, "ag-A", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        assert claim is not None
        assert claim["agent"] == "ag-A"
        assert claim["claim_token"] is not None

    def test_get_missing_returns_none(self, store):
        assert pg.get_claim(store, 9_999_999) is None

    def test_token_not_exposed_by_default(self, store, work_item_id):
        cid = pg.create_claim(store, work_item_id, "ag-hidden", ttl_seconds=300)
        claim = pg.get_claim(store, cid)
        assert "claim_token" not in claim or claim.get("claim_token_redacted") is True

    def test_heartbeat_extends_ttl(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Hb-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-hb", ttl_seconds=60)
        claim = pg.get_claim(store, cid, include_secret=True)
        pg.heartbeat_claim(store, cid, claim["claim_token"], ttl_seconds=600)
        updated = pg.get_claim(store, cid)
        assert updated is not None

    def test_release_deletes_claim(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Rel-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-rel", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        pg.release_claim(store, cid, claim["claim_token"])
        assert pg.get_claim(store, cid) is None

    def test_handoff_rotates_token_and_changes_agent(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ho-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-from", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        old_token = claim["claim_token"]
        new_claim = pg.handoff_claim(store, cid, old_token, actor="ag-to")
        assert new_claim["agent"] == "ag-to"
        assert new_claim["claim_token"] != old_token

    def test_explicit_lost_proof_adoption_rotates_remote_claim_token(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Adopt-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-from", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)

        with pytest.raises(ValueError, match="Invalid claim_token"):
            pg.handoff_claim(
                store, cid, "not-the-token", actor="ag-to", allow_legacy_adopt=True
            )

        adopted = pg.handoff_claim(
            store, cid, None, actor="ag-to", allow_legacy_adopt=True, mode="rotate"
        )
        assert adopted["claim_token"] != claim["claim_token"]

        events = pg.list_events(store, sprint_id)
        handoff = [event for event in events if event["event_type"] == "claim-handoff"][-1]
        payload = json.loads(handoff["payload"])
        assert payload["lost_proof_adopted"] is True

    def test_find_claim_by_instance_id(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Fi-{_uid()}")
        inst = f"inst-{_uid()}"
        pg.create_claim(store, iid, "ag-fi", ttl_seconds=300, instance_id=inst)
        found = pg.find_claim_by_identity(store, instance_id=inst)
        assert len(found) == 1
        assert found[0]["instance_id"] == inst

    def test_list_claims_by_sprint(self, store, sprint_id, work_item_id):
        pg.create_claim(store, work_item_id, "ag-ls", ttl_seconds=300)
        claims = pg.list_claims_by_sprint(store, sprint_id)
        assert any(c["work_item_id"] == work_item_id for c in claims)

    def test_list_claims(self, store, work_item_id):
        pg.create_claim(store, work_item_id, "ag-lc", ttl_seconds=300)
        claims = pg.list_claims(store, work_item_id)
        assert isinstance(claims, list)

    def test_conflict_on_double_exclusive_claim(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cc-{_uid()}")
        pg.create_claim(store, iid, "ag-1", ttl_seconds=300)
        with pytest.raises(ClaimConflict):
            pg.create_claim(store, iid, "ag-2", ttl_seconds=300)

    def test_concurrent_exclusive_claims_serialize_on_repo_authority_lock(
        self, store, sprint_id, track_id, monkeypatch
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cr-{_uid()}")
        first_locked = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        second_locked = threading.Event()
        original_lock = pg._lock_repo_claim_capability_arbitration

        def instrumented_lock(cur, repo_id):
            if threading.current_thread().name == "claim-worker-b":
                second_attempted.set()
            original_lock(cur, repo_id)
            if threading.current_thread().name == "claim-worker-a":
                first_locked.set()
                assert release_first.wait(timeout=5)
            else:
                second_locked.set()

        monkeypatch.setattr(pg, "_lock_repo_claim_capability_arbitration", instrumented_lock)
        outcomes = []
        outcomes_lock = threading.Lock()

        def worker(actor):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                claim_id = pg.create_claim(independent_store, iid, actor, ttl_seconds=300)
                outcome = {"actor": actor, "result": "accepted", "claim_id": claim_id}
            except ClaimConflict as exc:
                outcome = {"actor": actor, "result": "rejected", "error": str(exc)}
            finally:
                conn.close()
            with outcomes_lock:
                outcomes.append(outcome)

        first = threading.Thread(target=worker, args=("ag-a",), name="claim-worker-a")
        second = threading.Thread(target=worker, args=("ag-b",), name="claim-worker-b")
        first.start()
        assert first_locked.wait(timeout=5)
        second.start()
        assert second_attempted.wait(timeout=5)
        assert not second_locked.wait(timeout=0.1), "second claim bypassed the arbitration lock"
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()

        assert sorted(outcome["result"] for outcome in outcomes) == ["accepted", "rejected"]
        with store.conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS count FROM claim
                WHERE repo_id = %s AND work_item_id = %s AND exclusive = true
                  AND expires_at > now()
                """,
                (store.repo_id, iid),
            )
            assert cur.fetchone()["count"] == 1


# ---------------------------------------------------------------------------
# Ref
# ---------------------------------------------------------------------------

class TestRef:
    def test_add_list_remove(self, store, work_item_id):
        rid = pg.add_ref(store, work_item_id, "pr",
                         "https://github.com/org/repo/pull/1")
        refs = pg.list_refs(store, work_item_id)
        assert any(r["id"] == rid for r in refs)
        pg.remove_ref(store, rid, work_item_id)
        assert not any(r["id"] == rid for r in pg.list_refs(store, work_item_id))

    def test_invalid_ref_type_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_ref(store, work_item_id, "tweet", "https://example.com")

    def test_scope_ref_round_trip(self, store, work_item_id):
        rid = pg.add_ref(store, work_item_id, "glob", "src/**/*.py", "Python sources")
        ref = next(ref for ref in pg.list_refs(store, work_item_id) if ref["id"] == rid)
        assert ref["url"] == "src/**/*.py"
        assert ref["scope"] == {"kind": "glob", "value": "src/**/*.py"}


# ---------------------------------------------------------------------------
# Dep
# ---------------------------------------------------------------------------

class TestDep:
    def test_add_and_list(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Da-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Db-{_uid()}")
        pg.add_dep(store, a, b)
        assert any(d["item_id"] == a for d in pg.list_deps_blocking(store, b))
        assert any(d["blocked_item_id"] == b for d in pg.list_deps_blocked_by(store, a))

    def test_remove_dep(self, store, sprint_id, track_id):
        x = pg.create_work_item(store, sprint_id, track_id, f"Dx-{_uid()}")
        y = pg.create_work_item(store, sprint_id, track_id, f"Dy-{_uid()}")
        dep_id = pg.add_dep(store, x, y)
        pg.remove_dep(store, dep_id, x)
        assert pg.list_deps_blocking(store, y) == []

    def test_get_ready_items(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Ra-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Rb-{_uid()}")
        pg.add_dep(store, a, b)
        ready_ids = {i["id"] for i in pg.get_ready_items(store, sprint_id)}
        assert a in ready_ids
        assert b not in ready_ids

    def test_self_dep_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_dep(store, work_item_id, work_item_id)


# ---------------------------------------------------------------------------
# Recovery snapshot (Postgres → recovery SQLite, ID-preserving)
# ---------------------------------------------------------------------------

class TestRecoverFromRemote:
    def test_snapshot_then_write_preserves_ids_and_passes_integrity(
        self, tmp_path, store, sprint_id, track_id, work_item_id,
    ):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        work_item_id=work_item_id,
                        payload={"summary": "recovery source event"})
        pg.create_claim(store, work_item_id, "ag", ttl_seconds=300)
        pg.add_ref(store, work_item_id, "doc", "docs/plans/x.md")
        other_item = pg.create_work_item(store, sprint_id, track_id, f"Dep-{_uid()}")
        pg.add_dep(store, work_item_id, other_item)

        snapshot = pg.recover_repo_snapshot(store)
        assert any(row["id"] == sprint_id for row in snapshot["sprint"])
        assert any(row["id"] == work_item_id for row in snapshot["work_item"])
        assert snapshot["claim"] and snapshot["ref"] and snapshot["dep"]

        dest = tmp_path / "recovery.db"
        conn = db.get_connection(dest)
        try:
            db.init_db(conn)
            counts = db.write_recovery_snapshot(
                conn,
                snapshot,
                provenance={"recovered_at": "2026-07-24T00:00:00Z",
                            "source_repo_id": store.repo_id},
            )
            assert counts["sprint"] == len(snapshot["sprint"])
            assert counts["work_item"] == len(snapshot["work_item"])

            assert db.get_sprint(conn, sprint_id) is not None
            assert db.get_work_item(conn, work_item_id)["id"] == work_item_id
            assert db.get_work_item(conn, other_item)["id"] == other_item

            report = db.check_integrity(conn)
            assert report["ok"] is True, report

            claim_row = conn.execute(
                "SELECT exclusive, status, claim_token FROM claim WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            assert claim_row["exclusive"] == 1
            # ownership is never restored: the live pg claim comes back closed,
            # with its bearer token stripped
            assert claim_row["status"] == "expired"
            assert claim_row["claim_token"] is None

            provenance_rows = conn.execute(
                "SELECT sprint_id FROM event WHERE event_type = 'recovery.completed'"
            ).fetchall()
            assert len(provenance_rows) == len(snapshot["sprint"])

            event_row = conn.execute(
                "SELECT payload FROM event WHERE work_item_id = ? AND event_type = 'note'",
                (work_item_id,),
            ).fetchone()
            assert json.loads(event_row["payload"])["summary"] == "recovery source event"

            # A subsequent synthetic recovery.completed event must not collide
            # with any restored event id.
            max_restored_event_id = max(row["id"] for row in snapshot["event"])
            new_event_id = db.create_event(
                conn, sprint_id, "sprintctl", "recovery.completed", source_type="system",
            )
            assert new_event_id > max_restored_event_id
        finally:
            conn.close()

    def test_refuses_to_overwrite_existing_output(self, tmp_path, runner, monkeypatch):
        (tmp_path / ".git").mkdir()
        dest = tmp_path / "existing.db"
        dest.write_text("not a real db")
        monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
        monkeypatch.setenv("SPRINTCTL_URL", _PG_URL)
        result = runner.invoke(
            cli,
            [
                "--repo-id", tmp_path.name,
                "--allow-markerless-nonlocal",
                "db", "recover-from-remote",
                "--output", str(dest),
            ],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output


# ---------------------------------------------------------------------------
# NDJSON round-trip (export sqlite → import pg)
# ---------------------------------------------------------------------------

class TestNdjsonRoundTrip:
    def test_trusted_state_transfer_preserves_typed_event_ids_exactly(self, store):
        sprint_id = 2_000_000_000 + int(uuid.uuid4().hex[:6], 16)
        boundary_event_id = sprint_id + 1
        receipt_event_id = sprint_id + 2
        receipt_id = f"{store.repo_id}.2026-07-13.migration"
        receipt_payload = {
            "project": store.repo_id,
            "receipt_id": receipt_id,
            "receipt_path": (
                f"/projects/dev/_artifacts/{store.repo_id}/capability/receipts/"
                f"{receipt_id}.json"
            ),
            "receipt_sha256": "c" * 64,
        }
        records = [
            {
                "table": "sprint",
                "repo_id": store.repo_id,
                "data": {
                    "id": sprint_id,
                    "name": f"Trusted-{_uid()}",
                    "goal": "G",
                    "status": "closed",
                },
            },
            {
                "table": "event",
                "repo_id": store.repo_id,
                "data": {
                    "id": boundary_event_id,
                    "sprint_id": sprint_id,
                    "work_item_id": None,
                    "source_type": "actor",
                    "actor": "operator",
                    "event_type": contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                    "payload": {"previous_status": "active", "status": "closed"},
                },
            },
            {
                "table": "event",
                "repo_id": store.repo_id,
                "data": {
                    "id": receipt_event_id,
                    "sprint_id": sprint_id,
                    "work_item_id": None,
                    "source_type": "actor",
                    "actor": "drafting-agent",
                    "event_type": contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                    "payload": receipt_payload,
                },
            },
        ]

        pg.import_ndjson(store, records, trusted_state_transfer=True)

        events = {event["id"]: event for event in pg.list_events(store, sprint_id)}
        assert events[boundary_event_id]["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
        assert events[receipt_event_id]["event_type"] == contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
        assert json.loads(events[receipt_event_id]["payload"]) == receipt_payload
        with pytest.raises(ValueError, match="cannot remap IDs"):
            pg.import_ndjson(
                store,
                records,
                remap_ids=True,
                trusted_state_transfer=True,
            )

    def test_sqlite_to_pg_full_round_trip(self, store, pg_test_scope, tmp_path):
        """Export from a fresh sqlite db, import into pg, verify data survives."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rt.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        rt_repo_id = pg_test_scope("round-trip")
        sid = _db.create_sprint(conn, "RT Sprint", "G", "2026-01-01", "2026-12-31", "active")
        tid = _db.get_or_create_track(conn, sid, "eng")
        iid = _db.create_work_item(conn, sid, tid, "RT Item")
        _db.create_event(conn, sid, actor="a", event_type="note", source_type="actor",
                         work_item_id=iid, payload={"summary": "round-trip"})
        _db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/99")
        conn.commit()

        buf = io.StringIO()
        export_counts = pg.export_ndjson(conn, rt_repo_id, buf)
        conn.close()

        rt_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row),
                              repo_id=rt_repo_id)
        pg.init_db(rt_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            import_counts = pg.import_ndjson(rt_store, records, remap_ids=True)

            assert import_counts["sprint"] == export_counts["sprint"] == 1
            assert import_counts["track"] == export_counts["track"] == 1
            assert import_counts["work_item"] == export_counts["work_item"] == 1
            assert import_counts["event"] == export_counts["event"] == 1
            assert import_counts["ref"] == export_counts["ref"] == 1

            sprints = pg.list_sprints(rt_store)
            assert any(s["name"] == "RT Sprint" for s in sprints)
            items = pg.list_work_items(rt_store)
            assert any(i["title"] == "RT Item" for i in items)
        finally:
            rt_store.conn.close()

    def test_import_with_replace_clears_existing_data(self, store, pg_test_scope, tmp_path):
        """import_ndjson with replace=True should delete existing rows first."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rep.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        repl_repo_id = pg_test_scope("replace")
        sid = _db.create_sprint(conn, "Repl Sprint", "G", "2026-01-01", "2026-12-31", "active")
        conn.commit()

        buf = io.StringIO()
        pg.export_ndjson(conn, repl_repo_id, buf)
        conn.close()

        repl_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row),
                                repo_id=repl_repo_id)
        pg.init_db(repl_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            pg.import_ndjson(repl_store, records, remap_ids=True)
            # Re-importing with replace should succeed without unique-violation errors
            pg.import_ndjson(repl_store, records, replace=True, remap_ids=True)
            assert len(pg.list_sprints(repl_store)) == 1
        finally:
            repl_store.conn.close()

    def test_concurrent_imports_serialize_identity_sequence_advance(
        self, store, pg_test_scope, monkeypatch
    ):
        """Regression test for sprintctl#1250.

        _advance_identity_sequences reads MAX(id) then setval()s the shared,
        cross-repo identity sequence to it. Without IDENTITY_SEQUENCE_LOCK_KEYS,
        two concurrent import_ndjson calls can each read a stale MAX(id) under
        READ COMMITTED (neither sees the other's still-uncommitted rows), and
        because sequence state is non-transactional, whichever call's setval()
        physically executes last wins -- even if it reflects fewer rows. That
        can regress the sequence below rows the *other* caller already
        committed, so a naive test asserting only "my own rows are covered"
        would not catch it; this asserts the sequence covers the max id across
        *both* callers.

        Proves two things: (1) the second call's advisory-lock acquisition
        genuinely blocks until the first call's transaction commits (not just
        that results happen to come out right), and (2) the resulting sequence
        value is >= the true combined max of both callers' rows.
        """
        repo_a = pg_test_scope("seq-lock-a")
        repo_b = pg_test_scope("seq-lock-b")
        base = 2_000_000_000 + int(uuid.uuid4().hex[:6], 16)
        ids_a = [base, base + 1, base + 2]
        ids_b = [base + 1000, base + 1001, base + 1002]

        def sprint_records(repo_id, ids):
            return [
                {
                    "table": "sprint",
                    "repo_id": repo_id,
                    "data": {"id": i, "name": f"SeqLock-{i}", "goal": "G", "status": "active"},
                }
                for i in ids
            ]

        first_paused = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        second_completed = threading.Event()
        original_advance = pg._advance_identity_sequences

        def instrumented_advance(cur, tables):
            name = threading.current_thread().name
            if name == "import-worker-b":
                second_attempted.set()
            original_advance(cur, tables)
            if name == "import-worker-a":
                first_paused.set()
                assert release_first.wait(timeout=5)
            else:
                second_completed.set()

        monkeypatch.setattr(pg, "_advance_identity_sequences", instrumented_advance)
        failures = []

        def worker(repo_id, ids):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=repo_id)
            try:
                pg.import_ndjson(independent_store, sprint_records(repo_id, ids))
            except BaseException as exc:  # Preserve a worker failure for assertion.
                failures.append(exc)
            finally:
                conn.close()

        first = threading.Thread(
            target=worker, args=(repo_a, ids_a), name="import-worker-a"
        )
        second = threading.Thread(
            target=worker, args=(repo_b, ids_b), name="import-worker-b"
        )
        try:
            first.start()
            assert first_paused.wait(timeout=5), "first import never reached the critical section"
            second.start()
            assert not second_attempted.wait(timeout=0.2), (
                "second import bypassed the identity-sequence advisory lock while "
                "the first import's transaction was still open"
            )
            release_first.set()
            first.join(timeout=15)
            second.join(timeout=15)
            assert not first.is_alive() and not second.is_alive()
            assert second_completed.wait(timeout=5)
        finally:
            release_first.set()  # Never leave the first worker parked on failure.

        assert failures == []

        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_get_serial_sequence('sprint', 'id') AS seq")
            seq_name = cur.fetchone()["seq"]
            cur.execute(f"SELECT last_value FROM {seq_name}")  # noqa: S608 - identifier from pg catalog, not user input
            last_value = cur.fetchone()["last_value"]

        true_max = max(ids_a + ids_b)
        assert last_value >= true_max, (
            f"sequence last_value={last_value} regressed below the combined "
            f"max id={true_max} across both concurrent importers"
        )

        # Defense in depth: the next ordinary (non-explicit-id) insert must not
        # collide with either caller's explicitly-imported rows.
        next_id = pg.create_sprint(
            pg.PgStore(conn=store.conn, repo_id=repo_a), f"After-{uuid.uuid4().hex[:6]}", "G",
            status="active",
        )
        assert next_id > true_max


class TestRemoteBackfill:
    """export_from_postgres + import_ndjson(remap_ids=True): the PostgreSQL-to-
    PostgreSQL path `sprintctl remote-backfill` uses to copy a repository's
    history out of a separate, already-deployed authority. Both connections
    point at the same disposable database in these tests (there is only one
    to test against), but the functions themselves are connection-agnostic --
    export_from_postgres only ever reads, import_ndjson only ever writes to
    the store it's given."""

    def test_export_from_postgres_matches_backfill_row_counts(self, store, pg_test_scope):
        repo_id = pg_test_scope("backfill-export")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(source_store, "Backfill Sprint", "G", status="active")
        track_id = pg.get_or_create_track(source_store, sprint_id, "eng")
        item_id = pg.create_work_item(source_store, sprint_id, track_id, "Backfill Item")
        pg.add_ref(source_store, item_id, "pr", "https://github.com/org/repo/pull/1")
        pg.create_event(
            source_store, sprint_id, actor="a", event_type="note",
            source_type="actor", work_item_id=item_id, payload={"summary": "x"},
        )

        counts = pg.backfill_repo_row_counts(store.conn, repo_id)
        assert counts["sprint"] == 1
        assert counts["track"] == 1
        assert counts["work_item"] == 1
        assert counts["ref"] == 1
        assert counts["event"] == 1
        assert counts["claim"] == 0
        assert counts["dep"] == 0

        records = pg.export_from_postgres(store.conn, repo_id)
        assert len(records) == sum(counts.values())
        by_table = {}
        for record in records:
            by_table.setdefault(record["table"], []).append(record)
            assert record["repo_id"] == repo_id
            assert "repo_id" not in record["data"]
        assert len(by_table["sprint"]) == 1
        assert by_table["sprint"][0]["data"]["name"] == "Backfill Sprint"
        assert by_table["ref"][0]["data"]["url"] == "https://github.com/org/repo/pull/1"

    def test_backfill_round_trip_remaps_ids_and_preserves_parity(self, store, pg_test_scope):
        repo_id = pg_test_scope("backfill-roundtrip")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(source_store, "RT Sprint", "G", status="active")
        track_id = pg.get_or_create_track(source_store, sprint_id, "eng")
        item_id = pg.create_work_item(source_store, sprint_id, track_id, "RT Item")
        pg.add_ref(source_store, item_id, "pr", "https://github.com/org/repo/pull/2")
        pg.create_event(
            source_store, sprint_id, actor="a", event_type="note",
            source_type="actor", work_item_id=item_id, payload={"summary": "rt"},
        )

        source_counts = pg.backfill_repo_row_counts(store.conn, repo_id)
        records = pg.export_from_postgres(store.conn, repo_id)

        dest_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row), repo_id=repo_id)
        try:
            # replace=True simulates copying into a genuinely separate,
            # already-populated-by-source destination: delete the captured
            # rows (records already holds their data in memory), then
            # reinsert with fresh remapped IDs -- exactly what
            # remote_backfill_cmd does against two real databases.
            imported = pg.import_ndjson(dest_store, records, replace=True, remap_ids=True)
            dest_counts = pg.backfill_repo_row_counts(dest_store.conn, repo_id)
            assert dest_counts == source_counts
            assert imported["sprint"] == 1
            assert imported["work_item"] == 1

            new_item = pg.get_work_item(dest_store, pg.list_work_items(dest_store, sprint_id=None)[0]["id"])
            assert new_item["id"] != item_id, "remap_ids=True must not preserve the source's literal id"
            assert new_item["title"] == "RT Item"
        finally:
            dest_store.conn.close()

    def test_remote_backfill_row_counts_ignore_other_repos(self, store, pg_test_scope):
        repo_a = pg_test_scope("backfill-scope-a")
        repo_b = pg_test_scope("backfill-scope-b")
        store_a = pg.PgStore(conn=store.conn, repo_id=repo_a)
        store_b = pg.PgStore(conn=store.conn, repo_id=repo_b)
        pg.create_sprint(store_a, "A Sprint", "G", status="active")
        pg.create_sprint(store_b, "B Sprint 1", "G", status="active")
        pg.create_sprint(store_b, "B Sprint 2", "G", status="active")

        assert pg.backfill_repo_row_counts(store.conn, repo_a)["sprint"] == 1
        assert pg.backfill_repo_row_counts(store.conn, repo_b)["sprint"] == 2
        records_a = pg.export_from_postgres(store.conn, repo_a)
        assert len([r for r in records_a if r["table"] == "sprint"]) == 1


class TestRemoteBackfillCli:
    """CLI wiring for `sprintctl remote-backfill`. --source-url and --url both
    point at the same disposable Postgres in these tests -- the command
    itself does not require them to differ, and in production they name two
    genuinely separate deployments."""

    def test_dry_run_reports_source_counts_without_writing(self, store, pg_test_scope, runner):
        repo_id = pg_test_scope("backfill-cli-dry")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        pg.create_sprint(source_store, "CLI Dry Sprint", "G", status="active")

        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--dry-run",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["source_counts"]["sprint"] == 1

    def test_missing_repo_data_at_source_is_an_error(self, pg_test_scope, runner):
        repo_id = pg_test_scope("backfill-cli-missing")
        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--dry-run",
        ])
        assert result.exit_code != 0
        assert "no rows found" in result.output.lower()

    def test_existing_destination_data_requires_replace(self, store, pg_test_scope, runner):
        # Since --source-url and --url are the same disposable database in
        # this test, any pre-existing row for repo_id already satisfies the
        # "destination has data" guard -- no separate backfill run needed to
        # set it up.
        repo_id = pg_test_scope("backfill-cli-guard")
        guard_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        pg.create_sprint(guard_store, "Guard Sprint", "G", status="active")

        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--yes",
        ])
        assert result.exit_code != 0
        assert "already has data" in result.output.lower()

    def test_full_backfill_with_replace_reports_parity(self, store, pg_test_scope, runner):
        repo_id = pg_test_scope("backfill-cli-full")
        source_store = pg.PgStore(conn=store.conn, repo_id=repo_id)
        sprint_id = pg.create_sprint(source_store, "CLI Full Sprint", "G", status="active")
        track_id = pg.get_or_create_track(source_store, sprint_id, "eng")
        pg.create_work_item(source_store, sprint_id, track_id, "CLI Full Item")

        # --replace lets the (trivially true, same-database-in-this-test)
        # existing-destination-data guard through; source_counts is computed
        # from the CLI's own pre-write read, before import_ndjson's replace
        # delete+reinsert -- so parity still means what it means in
        # production, where source and destination are genuinely separate.
        result = runner.invoke(cli, [
            "remote-backfill",
            "--source-url", _PG_URL,
            "--url", _PG_URL,
            "--repo-id", repo_id,
            "--replace",
            "--yes",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["parity_ok"] is True
        assert payload["parity"]["sprint"] == {"source": 1, "destination": 1}
        assert payload["parity"]["track"] == {"source": 1, "destination": 1}
        assert payload["parity"]["work_item"] == {"source": 1, "destination": 1}


# ---------------------------------------------------------------------------
# Maintain
# ---------------------------------------------------------------------------

class TestMaintain:
    def test_purge_expired_claims_marks_and_retains_history(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Pu-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-pu", ttl_seconds=300)
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE claim SET expires_at = now() - interval '1 second'"
                " WHERE repo_id = %s AND id = %s",
                (store.repo_id, cid),
            )
        store.conn.commit()
        expired = pg.purge_expired_claims(store, sprint_id)
        assert expired >= 1
        claim = pg.get_claim(store, cid)
        assert claim is not None
        assert claim["status"] == "expired"

    def test_expiry_reacquire_retains_both_rows_and_increments_epoch(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Epoch-{_uid()}")
        old_id = pg.create_claim(store, iid, "old-owner")
        old = pg.get_claim(store, old_id, include_secret=True)
        assert old["lease_epoch"] == 1

        rotated = pg.handoff_claim(
            store,
            old_id,
            old["claim_token"],
            actor="rotated-owner",
            mode="rotate",
        )
        assert rotated["lease_epoch"] == 2
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE claim SET expires_at = now() - interval '1 second' "
                "WHERE repo_id = %s AND id = %s",
                (store.repo_id, old_id),
            )
        store.conn.commit()

        new_id = pg.create_claim(store, iid, "new-owner")
        history = pg.list_claims(store, iid, active_only=False)
        assert [claim["claim_id"] for claim in history] == [old_id, new_id]
        assert [claim["status"] for claim in history] == ["expired", "active"]
        assert [claim["lease_epoch"] for claim in history] == [2, 3]

    def test_truth_findings_match_remote_backend(self, store, sprint_id, track_id):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Drift-{_uid()}")
        pg.set_work_item_status(store, item_id, "active")
        event_id = pg.create_event(
            store,
            sprint_id,
            actor="wrapper",
            event_type="session.ended",
            source_type="daemon",
            payload={"git": {"commits": ["abc123"]}},
        )

        report = maintain.check(
            store,
            sprint_id,
            datetime.now(timezone.utc),
            _m=pg,
        )

        findings = {finding["reason_code"]: finding for finding in report["findings"]}
        assert findings["active-item-without-live-claim"]["item_ids"] == [item_id]
        assert findings["code-evidence-without-item-link"]["event_ids"] == [event_id]


# ---------------------------------------------------------------------------
# Authority fault histories
# ---------------------------------------------------------------------------

class TestAuthorityFaultHistories:
    def _independent_store(self, store):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        return pg.PgStore(conn=conn, repo_id=store.repo_id)

    def test_partition_expiry_reassignment_then_stale_heartbeat_is_rejected(
        self,
        store,
    ):
        sprint_id = pg.create_sprint(store, f"Partition-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Lease-{_uid()}")
        old_claim_id = pg.create_claim(store, item_id, "partitioned-owner")
        old_claim = pg.get_claim(store, old_claim_id, include_secret=True)
        replacement = self._independent_store(store)
        try:
            with replacement.conn.cursor() as cur:
                cur.execute(
                    "UPDATE claim SET expires_at = now() - interval '1 second' "
                    "WHERE repo_id = %s AND id = %s",
                    (store.repo_id, old_claim_id),
                )
            replacement.conn.commit()
            assert pg.list_claims(store, item_id, active_only=True) == []

            new_claim_id = pg.create_claim(replacement, item_id, "replacement-owner")
            with pytest.raises(ValueError, match="expired and is no longer active"):
                pg.heartbeat_claim(
                    store,
                    old_claim_id,
                    old_claim["claim_token"],
                    actor="partitioned-owner",
                )

            active_ids = {
                claim["claim_id"] for claim in pg.list_claims(replacement, item_id, active_only=True)
            }
            assert active_ids == {new_claim_id}
            history = pg.list_claims(replacement, item_id, active_only=False)
            assert [claim["claim_id"] for claim in history] == [old_claim_id, new_claim_id]
            assert [claim["status"] for claim in history] == ["expired", "active"]
        finally:
            replacement.conn.close()

    def test_stale_item_and_sprint_commands_reject_without_second_mutation(self, store):
        sprint_id = pg.create_sprint(store, f"Stale-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Stale-item-{_uid()}")
        pg.set_work_item_status(store, item_id, "active")
        actor_b = self._independent_store(store)
        try:
            assert pg.get_work_item(actor_b, item_id)["status"] == "active"
            assert pg.get_sprint(actor_b, sprint_id)["status"] == "active"

            pg.set_work_item_status(store, item_id, "done")
            with pytest.raises(InvalidTransition, match="done -> done"):
                pg.set_work_item_status(actor_b, item_id, "done")
            assert pg.get_work_item(actor_b, item_id)["status"] == "done"

            boundary_id = pg.close_sprint_with_boundary_event(store, sprint_id, "actor-a")
            with pytest.raises(InvalidTransition, match="sprint closed -> closed"):
                pg.close_sprint_with_boundary_event(actor_b, sprint_id, "actor-b")
            boundaries = [
                event
                for event in pg.list_events(actor_b, sprint_id)
                if event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
            ]
            assert [event["id"] for event in boundaries] == [boundary_id]
            assert pg.get_sprint(actor_b, sprint_id)["status"] == "closed"
        finally:
            actor_b.conn.close()


# ---------------------------------------------------------------------------
# Durable authority command arbitration
# ---------------------------------------------------------------------------

class TestAuthorityCommandArbitration:
    def _independent_store(self, store):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        return pg.PgStore(conn=conn, repo_id=store.repo_id)

    def test_request_and_decision_receive_consecutive_repository_offsets(
        self, pg_test_scope, store, tmp_path
    ):
        isolated = pg.PgStore(
            conn=store.conn,
            repo_id=pg_test_scope("cursor-command-pair"),
        )
        isolated.authority_repo_uuid = _authority_repo_uuid(isolated)
        sprint_id = pg.create_sprint(isolated, f"Cursor-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(isolated, sprint_id, "authority")
        item_id = pg.create_work_item(isolated, sprint_id, track_id, "Cursor command")
        item = pg.get_work_item(isolated, item_id)
        producer = outbox.open_outbox(tmp_path / "cursor-command-pair.db")
        try:
            command = _append_authority_command(
                producer,
                isolated,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            decision = authority.arbitrate_command(isolated, command)
        finally:
            producer.close()

        history = pg.list_ingested_records(isolated)
        assert [entry.record.event_id for entry in history] == [
            command.event_id,
            decision.decision_event_id,
        ]
        assert [entry.ingest_offset for entry in history] == [1, 2]
        assert decision.decision_ingest_offset == 2
        assert pg.get_ingest_high_water(isolated) == 2

    def test_authenticated_actor_mismatch_is_durably_rejected_and_consumes_sequence(
        self, store, tmp_path
    ):
        sprint_id = pg.create_sprint(store, f"Actor-mismatch-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, "Actor mismatch command")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "actor-mismatch.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
                actor="stale-actor",
            )
            rejected = authority.arbitrate_command(
                store, command, authenticated_actor="served-actor"
            )
            retried = authority.arbitrate_command(
                store, command, authenticated_actor="served-actor"
            )
        finally:
            producer.close()

        assert rejected.outcome == "rejected"
        assert rejected.reason_code == "actor-mismatch"
        assert retried.to_dict() == {**rejected.to_dict(), "duplicate": True}
        assert pg.get_work_item(store, item_id)["status"] == "pending"

    def test_item_transition_retry_and_stale_rejection_are_durable(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Command-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-item.db")
        try:
            first = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            accepted = authority.arbitrate_command(store, first)
            retried = authority.arbitrate_command(store, first)

            assert accepted.accepted is True
            assert accepted.decision_type == "item.transitioned"
            assert retried.to_dict() == {**accepted.to_dict(), "duplicate": True}
            assert pg.get_work_item(store, item_id)["status"] == "active"

            stale = _append_authority_command(
                producer,
                store,
                record_type="item.done",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "done"},
            )
            rejected = authority.arbitrate_command(store, stale)

            assert rejected.accepted is False
            assert rejected.decision_type == "command.rejected"
            assert rejected.reason_code == "stale-basis"
            assert pg.get_work_item(store, item_id)["status"] == "active"
            assert [decision.outcome for decision in authority.list_authority_decisions(store)][-2:] == [
                "accepted",
                "rejected",
            ]

            current = pg.get_work_item(store, item_id)
            done = _append_authority_command(
                producer,
                store,
                record_type="item.done",
                aggregate_type="item",
                aggregate_uuid=current["aggregate_uuid"],
                basis_revision=authority.item_revision(current),
                payload={"to_status": "done"},
            )
            completed = authority.arbitrate_command(store, done)
            assert completed.accepted is True
            assert completed.effect["status"] == "done"
            assert pg.get_work_item(store, item_id)["status"] == "done"
        finally:
            producer.close()

    def test_repository_uuid_mismatch_is_durably_rejected(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Repo-mismatch-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Repo-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-repo-mismatch.db")
        try:
            command = contracts.AuthorityCommand(
                event_id=str(uuid.uuid4()),
                record_type="item.transition",
                schema_version="1",
                actor="wrong-repository",
                authored_at="2026-07-14T18:00:00Z",
                refs={
                    "repo_id": str(uuid.uuid4()),
                    "aggregate_type": "item",
                    "aggregate_uuid": item["aggregate_uuid"],
                },
                payload={"to_status": "active"},
                basis_revision=authority.item_revision(item),
            )
            durable = outbox.append_authority_command(producer, command)
            decision = authority.arbitrate_command(store, durable)
            assert decision.accepted is False
            assert decision.reason_code == "repository-mismatch"
            assert pg.get_work_item(store, item_id)["status"] == "pending"
        finally:
            producer.close()

    def test_arbitrate_command_succeeds_without_a_committed_repository_uuid(
        self, pg_test_scope, store, tmp_path
    ):
        """Regression test for the served (Vuoro work-adapter) composition
        path, which never sets ``authority_repo_uuid`` -- there is no
        server-side repo-UUID registry to populate it from, because served
        callers are already tenant-isolated by identity before
        WorkApplication.invoke ever runs (see vuoro_service.composition).

        Before this fix, ``_apply_command`` treated an unset
        authority_repo_uuid as a hard `AuthorityProtocolError`, which meant
        every served item/sprint/claim authority command failed
        unconditionally -- discovered 2026-07-24 while reconciling sprintctl
        #1220/#1221, which had been silently blocked by this since the
        served work.lifecycle.arbitrate route shipped in #1195. Every other
        test in this class supplies a matching authority_repo_uuid on both
        sides and would not have caught this.
        """
        isolated = pg.PgStore(
            conn=store.conn,
            repo_id=pg_test_scope("served-no-committed-uuid"),
        )
        assert isolated.authority_repo_uuid is None
        sprint_id = pg.create_sprint(isolated, f"Served-uuid-{_uid()}", status="active")
        track_id = pg.get_or_create_track(isolated, sprint_id, "authority")
        item_id = pg.create_work_item(isolated, sprint_id, track_id, "Served item")
        item = pg.get_work_item(isolated, item_id)
        producer = outbox.open_outbox(tmp_path / "served-no-committed-uuid.db")
        try:
            command = contracts.AuthorityCommand(
                event_id=str(uuid.uuid4()),
                record_type="item.transition",
                schema_version="1",
                actor="served-client",
                authored_at="2026-07-24T18:00:00Z",
                refs={
                    # A served client has no committed authority UUID of its
                    # own either; any UUID-shaped value is accepted here
                    # since the mismatch check is skipped when the store has
                    # nothing to check it against (see isolated above).
                    "repo_id": str(uuid.uuid4()),
                    "aggregate_type": "item",
                    "aggregate_uuid": item["aggregate_uuid"],
                },
                payload={"to_status": "active"},
                basis_revision=authority.item_revision(item),
            )
            durable = outbox.append_authority_command(producer, command)
            decision = authority.arbitrate_command(isolated, durable)
        finally:
            producer.close()

        assert decision.accepted is True
        assert pg.get_work_item(isolated, item_id)["status"] == "active"

    def test_malformed_embedded_command_is_durably_rejected(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Malformed-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Malformed-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-malformed.db")
        try:
            valid = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            malformed_payload = {"malformed": True}
            encoded = json.dumps(
                malformed_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            malformed = replace(
                valid,
                payload=malformed_payload,
                payload_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
            )

            decision = authority.arbitrate_command(store, malformed)
            assert decision.accepted is False
            assert decision.reason_code == "invalid-command"
            assert pg.get_work_item(store, item_id)["status"] == "pending"
            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM ingest_record "
                    "WHERE repo_id = %s AND event_id IN (%s, %s)",
                    (store.repo_id, malformed.event_id, decision.decision_event_id),
                )
                assert cur.fetchone()["count"] == 2
        finally:
            producer.close()

    def test_sync_stops_at_pending_command_then_resumes_stream_in_order(
        self, store, tmp_path
    ):
        sprint_id = pg.create_sprint(store, f"Pending-sync-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Pending-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-pending-sync.db")
        cache = projection.open_cached_projection(tmp_path / "authority-pending-cache.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            observation = outbox.append_observation(
                producer,
                event_type="work.completed",
                actor="producer-after-command",
                payload={"item_id": item_id},
                occurred_at="2026-07-14T18:00:01Z",
            )

            pending = sync.synchronize_outbox(
                producer,
                store,
                cache,
                apply_ingest_projection=False,
            )
            assert pending.pending_command_event_ids == (command.event_id,)
            assert pending.uploaded == ()
            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM ingest_record "
                    "WHERE repo_id = %s AND event_id IN (%s, %s)",
                    (store.repo_id, command.event_id, observation.event_id),
                )
                assert cur.fetchone()["count"] == 0

            resumed = sync.synchronize_outbox(
                producer,
                store,
                cache,
                credential_resolver=lambda _record: {},
                apply_ingest_projection=False,
            )
            assert [decision.request_event_id for decision in resumed.command_decisions] == [
                command.event_id
            ]
            assert [result.record.event_id for result in resumed.uploaded] == [
                observation.event_id
            ]
            assert resumed.pending_command_event_ids == ()
            assert pg.get_work_item(store, item_id)["status"] == "active"
        finally:
            producer.close()
            cache.close()

    def test_expired_claim_cannot_be_revived_after_reassignment(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Claim-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Claim-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-claim.db")
        old_token = "old-" + uuid.uuid4().hex
        new_token = "new-" + uuid.uuid4().hex
        try:
            first = _append_authority_command(
                producer,
                store,
                record_type="claim.acquire",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "agent": "old-owner",
                    "claim_type": "execute",
                    "exclusive": True,
                    "ttl_seconds": 300,
                    "credential_ref": authority.credential_ref(old_token),
                    "metadata": {},
                },
            )
            granted = authority.arbitrate_command(
                store,
                first,
                credentials={authority.credential_ref(old_token): old_token},
            )
            old_claim_id = granted.effect["claim_id"]
            with store.conn.cursor() as cur:
                cur.execute(
                    "UPDATE claim SET expires_at = now() - interval '1 second' "
                    "WHERE repo_id = %s AND id = %s",
                    (store.repo_id, old_claim_id),
                )
            store.conn.commit()
            expired = pg.get_claim(store, old_claim_id, include_secret=True)

            replacement = _append_authority_command(
                producer,
                store,
                record_type="claim.acquire",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "agent": "new-owner",
                    "claim_type": "execute",
                    "exclusive": True,
                    "ttl_seconds": 300,
                    "credential_ref": authority.credential_ref(new_token),
                    "metadata": {},
                },
            )
            replacement_grant = authority.arbitrate_command(
                store,
                replacement,
                credentials={authority.credential_ref(new_token): new_token},
            )
            assert replacement_grant.accepted is True

            stale_renew = _append_authority_command(
                producer,
                store,
                record_type="claim.renew",
                aggregate_type="claim",
                claim_id=old_claim_id,
                basis_revision=authority.claim_revision(expired),
                payload={
                    "claim_id": old_claim_id,
                    "ttl_seconds": 300,
                    "credential_ref": authority.credential_ref(old_token),
                },
            )
            rejected = authority.arbitrate_command(
                store,
                stale_renew,
                credentials={authority.credential_ref(old_token): old_token},
            )

            assert rejected.accepted is False
            assert rejected.reason_code == "expired-grant"
            active_claims = pg.list_claims(store, item_id, active_only=True)
            assert [claim["claim_id"] for claim in active_claims] == [
                replacement_grant.effect["claim_id"]
            ]
        finally:
            producer.close()

    def test_claim_lifecycle_decisions_are_secret_safe(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Claim-lifecycle-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Claim-life-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-claim-lifecycle.db")
        first_token = "first-" + uuid.uuid4().hex
        rotated_token = "rotated-" + uuid.uuid4().hex
        first_ref = authority.credential_ref(first_token)
        rotated_ref = authority.credential_ref(rotated_token)
        try:
            acquire = _append_authority_command(
                producer,
                store,
                record_type="claim.acquire",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "agent": "owner-a",
                    "claim_type": "execute",
                    "exclusive": True,
                    "ttl_seconds": 300,
                    "credential_ref": first_ref,
                    "metadata": {"runtime_session_id": "session-a"},
                },
            )
            granted = authority.arbitrate_command(
                store, acquire, credentials={first_ref: first_token}
            )
            claim_id = granted.effect["claim_id"]

            claim = pg.get_claim(store, claim_id, include_secret=True)
            renew = _append_authority_command(
                producer,
                store,
                record_type="claim.renew",
                aggregate_type="claim",
                claim_id=claim_id,
                basis_revision=authority.claim_revision(claim),
                payload={
                    "claim_id": claim_id,
                    "ttl_seconds": 600,
                    "credential_ref": first_ref,
                },
            )
            renewed = authority.arbitrate_command(
                store, renew, credentials={first_ref: first_token}
            )
            assert renewed.decision_type == "claim.renewed"

            claim = pg.get_claim(store, claim_id, include_secret=True)
            handoff = _append_authority_command(
                producer,
                store,
                record_type="claim.handoff",
                aggregate_type="claim",
                claim_id=claim_id,
                basis_revision=authority.claim_revision(claim),
                payload={
                    "claim_id": claim_id,
                    "to_actor": "owner-b",
                    "mode": "rotate",
                    "ttl_seconds": 600,
                    "credential_ref": first_ref,
                    "proposed_credential_ref": rotated_ref,
                    "metadata": {"runtime_session_id": "session-b"},
                },
            )
            handed_off = authority.arbitrate_command(
                store,
                handoff,
                credentials={first_ref: first_token, rotated_ref: rotated_token},
            )
            assert handed_off.decision_type == "claim.handed-off"
            assert handed_off.effect["actor"] == "owner-b"

            claim = pg.get_claim(store, claim_id, include_secret=True)
            release = _append_authority_command(
                producer,
                store,
                record_type="claim.release",
                aggregate_type="claim",
                claim_id=claim_id,
                basis_revision=authority.claim_revision(claim),
                payload={"claim_id": claim_id, "credential_ref": rotated_ref},
            )
            released = authority.arbitrate_command(
                store, release, credentials={rotated_ref: rotated_token}
            )
            assert released.decision_type == "claim.released"
            assert released.effect["released"] is True
            assert pg.get_claim(store, claim_id, include_secret=True) is None

            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT payload::text FROM ingest_record WHERE repo_id = %s",
                    (store.repo_id,),
                )
                durable_text = "\n".join(row["payload"] for row in cur.fetchall())
                cur.execute(
                    "SELECT effect::text || coalesce(reason_detail, '') AS text "
                    "FROM authority_decision WHERE repo_id = %s",
                    (store.repo_id,),
                )
                durable_text += "\n" + "\n".join(row["text"] for row in cur.fetchall())
            assert first_token not in durable_text
            assert rotated_token not in durable_text
        finally:
            producer.close()

    def test_sprint_activate_is_remotely_arbitrated(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Activate-command-{_uid()}", status="planned")
        sprint = pg.get_sprint(store, sprint_id)
        producer = outbox.open_outbox(tmp_path / "authority-activate.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="sprint.activate",
                aggregate_type="sprint",
                aggregate_uuid=sprint["aggregate_uuid"],
                basis_revision=authority.sprint_revision(sprint),
                payload={},
            )
            decision = authority.arbitrate_command(store, command)
            assert decision.accepted is True
            assert decision.decision_type == "sprint-activated"
            assert pg.get_sprint(store, sprint_id)["status"] == "active"
        finally:
            producer.close()

    def test_decision_insert_failure_rolls_back_request_and_effect(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Atomic-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Atomic-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-atomic.db")
        suffix = uuid.uuid4().hex
        function_name = f"reject_authority_decision_{suffix}"
        trigger_name = f"reject_authority_decision_{suffix}"
        command = _append_authority_command(
            producer,
            store,
            record_type="item.transition",
            aggregate_type="item",
            aggregate_uuid=item["aggregate_uuid"],
            basis_revision=authority.item_revision(item),
            payload={"to_status": "active"},
        )
        try:
            with store.conn.cursor() as cur:
                cur.execute(
                    f"CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'injected decision failure'; END $$"
                )
                cur.execute(
                    f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON authority_decision "
                    f"FOR EACH ROW WHEN (NEW.repo_id = '{store.repo_id}') "
                    f"EXECUTE FUNCTION {function_name}()"
                )
            store.conn.commit()

            with pytest.raises(psycopg.Error, match="injected decision failure"):
                authority.arbitrate_command(store, command)

            assert pg.get_work_item(store, item_id)["status"] == "pending"
            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM ingest_record "
                    "WHERE repo_id = %s AND event_id = %s",
                    (store.repo_id, command.event_id),
                )
                assert cur.fetchone()["count"] == 0
                cur.execute(
                    "SELECT count(*) AS count FROM authority_decision "
                    "WHERE repo_id = %s AND request_event_id = %s",
                    (store.repo_id, command.event_id),
                )
                assert cur.fetchone()["count"] == 0
        finally:
            store.conn.rollback()
            with store.conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON authority_decision")
                cur.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
            store.conn.commit()
            producer.close()

    def test_sprint_close_boundary_and_decision_commit_atomically(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Close-command-{_uid()}", status="active")
        sprint = pg.get_sprint(store, sprint_id)
        producer = outbox.open_outbox(tmp_path / "authority-close.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="sprint.close",
                aggregate_type="sprint",
                aggregate_uuid=sprint["aggregate_uuid"],
                basis_revision=authority.sprint_revision(sprint),
                payload={},
            )
            decision = authority.arbitrate_command(store, command)
            retried = authority.arbitrate_command(store, command)

            assert decision.accepted is True
            assert decision.decision_type == "sprint-closed"
            assert retried.duplicate is True
            assert pg.get_sprint(store, sprint_id)["status"] == "closed"
            boundaries = [
                event
                for event in pg.list_events(store, sprint_id)
                if event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
            ]
            assert [event["id"] for event in boundaries] == [decision.effect["boundary_event_id"]]
        finally:
            producer.close()

    def test_capability_receipt_acceptance_is_a_remote_decision(
        self, store, tmp_path, monkeypatch
    ):
        sprint_id = pg.create_sprint(store, f"Receipt-command-{_uid()}", status="active")
        boundary_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        sprint = pg.get_sprint(store, sprint_id)
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_id)
        pointer = _receipt_payload(store, receipt_bytes)
        producer = outbox.open_outbox(tmp_path / "authority-receipt.db")
        monkeypatch.setattr(
            contracts,
            "verify_capability_receipt_draft_pointer",
            lambda payload, *, sprint_id, boundary_event_id: payload,
        )
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="capability-receipt.accept",
                aggregate_type="sprint",
                aggregate_uuid=sprint["aggregate_uuid"],
                basis_revision=f"event:{boundary_id}",
                payload={"pointer": pointer},
            )
            decision = authority.arbitrate_command(store, command)
            assert decision.accepted is True
            assert decision.decision_type == "capability-receipt.accepted"
            assert decision.effect["boundary_revision"] == f"event:{boundary_id}"
            event_types = [event["event_type"] for event in pg.list_events(store, sprint_id)]
            assert event_types == [
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
            ]
        finally:
            producer.close()

    def test_unavailable_capability_artifact_rejects_pointer_without_event(
        self,
        store,
        monkeypatch,
    ):
        sprint_id = pg.create_sprint(store, f"Artifact-{_uid()}", status="active")
        boundary_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_id)
        payload = _receipt_payload(store, receipt_bytes)

        def missing(receipt_path):
            raise ValueError(f"capability receipt file does not exist: {receipt_path}")

        monkeypatch.setattr(contracts, "_read_capability_receipt_bytes", missing)
        drafting_agent = self._independent_store(store)
        try:
            with pytest.raises(ValueError, match="file does not exist"):
                pg.create_event(
                    drafting_agent,
                    sprint_id,
                    "drafting-agent",
                    contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                    payload=payload,
                )
            event_types = [event["event_type"] for event in pg.list_events(store, sprint_id)]
            assert event_types == [contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE]
        finally:
            drafting_agent.conn.close()


# ---------------------------------------------------------------------------
# Takeup
# ---------------------------------------------------------------------------

class TestTakeup:
    def test_list_takeup_history_structure(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "sprint-taken-up", source_type="actor",
                        payload={"summary": "up", "detail": None, "actor_kind": "agent",
                                 "hostname": "h", "pid": 1, "instance_id": "i1",
                                 "runtime_session_id": None})
        history = pg.list_takeup_history(store, sprint_id)
        assert "active_takeups" in history
        assert isinstance(history["active_takeups"], list)

    def test_list_active_takeups(self, store, sprint_id):
        result = pg.list_active_takeups(store, sprint_id)
        assert isinstance(result, list)
