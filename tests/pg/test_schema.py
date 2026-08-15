"""PostgreSQL integration tests: MaintenanceCapabilityLifecycle, RemoteSafety, InitDb.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    pg,
    pg_migrations,
    ReservationConflict,
    assert_disposable_connection,
    new_test_repo_id,
    MaintenanceCapabilityError,
    PostgresMaintenanceCapabilityStore,
    AT,
    envelope,
    _uid,
    _anchor_capability_window_to_db_clock,
    PG_MARKS,
    _PG_URL,
    os,
    threading,
    uuid,
    psycopg,
    dict_row,
)


def _migrations_from(first: int) -> list[int]:
    """The migration versions a store at ``first - 1`` still has to apply.

    Derived from the ledger rather than written out, so adding a migration
    updates these expectations instead of silently invalidating them -- the
    PostgreSQL suite is skipped without SPRINTCTL_TEST_PG_URL, so a stale
    literal here goes unnoticed until a rehearsal runs.
    """

    return list(range(first, pg_migrations.CURRENT_SCHEMA_VERSION + 1))

pytestmark = PG_MARKS


class TestMaintenanceCapabilityLifecycle:
    def test_postgres_matches_exact_plan_lifecycle_and_replay(self, store, pg_test_scope):
        repo_id = pg_test_scope("maintenance-lifecycle")
        lifecycle = PostgresMaintenanceCapabilityStore(
            pg.PgStore(conn=store.conn, repo_id=repo_id)
        )
        capability_id = f"mcap:{uuid.uuid4()}"
        prepare_id = str(uuid.uuid4())
        capability_envelope = envelope()
        prepared = lifecycle.prepare(capability_id=capability_id, request_id=prepare_id, envelope=capability_envelope, actor="operator", at=AT)
        assert lifecycle.prepare(capability_id=capability_id, request_id=prepare_id, envelope=capability_envelope, actor="operator", at=AT)["duplicate"] is True
        attested = lifecycle.transition(capability_id=capability_id, request_id=str(uuid.uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT, effect_ref="sha256:" + "0" * 64)
        active = lifecycle.transition(capability_id=capability_id, request_id=str(uuid.uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)
        assert active["state"] == "active"
        recovery = lifecycle.append_recovery_record(capability_id=capability_id, record_id=str(uuid.uuid4()), kind="requested-command", payload_ref="artifact:sha256:" + "e" * 64, actor="recovery", at=AT)
        assert recovery["authority"] == "none"
        assert lifecycle.get(capability_id)["state"] == "active"

    def test_reservation_activation_race_has_exactly_one_authority_winner(
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
        _anchor_capability_window_to_db_clock(store.conn, repo_id, capability_id)
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

        def reserve() -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            try:
                actor_store = pg.PgStore(conn=conn, repo_id=repo_id)
                barrier.wait()
                pg.reserve(
                    actor_store,
                    item_id,
                    actor="ordinary-agent",
                    session_id="race-session",
                )
                outcomes["reservation"] = "accepted"
            except ReservationConflict as exc:
                outcomes["reservation"] = f"rejected:{exc}"
            finally:
                conn.close()

        workers = [threading.Thread(target=activate), threading.Thread(target=reserve)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=10)
            assert not worker.is_alive(), "shared reservation/capability arbitration deadlocked"

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
                "SELECT count(*) AS count FROM reservation WHERE repo_id=%s AND state='active'",
                (repo_id,),
            )
            live_reservations = int(cur.fetchone()["count"])
        assert not (capability_active and live_reservations), outcomes

    def test_rejected_reservation_rolls_back_repo_arbitration_on_retained_connection(
        self, store, pg_test_scope
    ):
        repo_id = pg_test_scope("maintenance-conflict-rollback")
        retained = psycopg.connect(_PG_URL, row_factory=dict_row)
        contender = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            retained_store = pg.PgStore(conn=retained, repo_id=repo_id)
            sprint_id = pg.create_sprint(retained_store, f"Conflict rollback-{_uid()}", status="active")
            track_id = pg.get_or_create_track(retained_store, sprint_id, "authority")
            item_id = pg.create_work_item(retained_store, sprint_id, track_id, "Rejected reservation")
            lifecycle = PostgresMaintenanceCapabilityStore(retained_store)
            prepared = lifecycle.prepare(capability_id=f"mcap:{uuid.uuid4()}", request_id=str(uuid.uuid4()), envelope=envelope(), actor="operator", at=AT)
            _anchor_capability_window_to_db_clock(retained, repo_id, prepared["capability_id"])
            attested = lifecycle.transition(capability_id=prepared["capability_id"], request_id=str(uuid.uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT, effect_ref="sha256:" + "0" * 64)
            lifecycle.transition(capability_id=prepared["capability_id"], request_id=str(uuid.uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)

            with pytest.raises(ReservationConflict):
                pg.reserve(
                    retained_store,
                    item_id,
                    actor="ordinary-agent",
                    session_id="rollback-session",
                )
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
            assert sorted(result["applied_versions"] for result in results) == [
                [],
                _migrations_from(2),
            ]
            with store.conn.cursor() as cur:
                cur.execute(f'SELECT version FROM "{schema}".schema_version')
                assert cur.fetchone()["version"] == pg_migrations.CURRENT_SCHEMA_VERSION
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
            assert [result["applied_versions"] for result in results] == [_migrations_from(3)]
            with store.conn.cursor() as cur:
                cur.execute(f'SELECT version FROM "{schema}".schema_version')
                assert cur.fetchone()["version"] == pg_migrations.CURRENT_SCHEMA_VERSION
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
            # The schema-6 maintenance bridge reads the capability marker row to
            # distinguish a complete migration from a partial one. Structural
            # verification is privilege-independent, but this row is real data,
            # so a runtime role must be granted SELECT on it at migration time.
            cur.execute(
                f"GRANT SELECT ON sprintctl_schema_capability TO {probe_role}"
            )
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
                cur.execute(
                    f"REVOKE SELECT ON sprintctl_schema_capability FROM {probe_role}"
                )
                cur.execute(f"REVOKE INSERT ON ingest_stream FROM {probe_role}")
                cur.execute(f"REVOKE SELECT ON schema_version FROM {probe_role}")
                cur.execute(f"REVOKE USAGE ON SCHEMA public FROM {probe_role}")
            store.conn.commit()

    def test_maintenance_fingerprint_is_identical_under_least_privilege(self, store):
        """A runtime role holding no privilege on the maintenance tables must
        still fingerprint the contract exactly as its owner does.

        information_schema hides relations the caller holds no privilege on, so
        deriving the catalog fingerprint from it made a least-privilege runtime
        role compute a subset of the contract and fail closed against a
        correctly migrated ledger. Only the capability marker row -- real data,
        not catalog shape -- is granted here, mirroring the production runtime
        role; the other three maintenance relations stay ungranted.
        """
        guard_url = os.environ.get("SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL")
        if not guard_url:
            pytest.skip("SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL not set")

        probe_role = "sprintctl_production_probe"
        with store.conn.cursor() as cur:
            owner_layout = pg_migrations._read_maintenance_bridge(cur)
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {probe_role}")
            cur.execute(
                f"GRANT SELECT ON sprintctl_schema_capability TO {probe_role}"
            )
        store.conn.commit()
        assert owner_layout["relation_count"] == 4
        assert owner_layout["available"] is True

        try:
            probe = psycopg.connect(guard_url, row_factory=dict_row)
            try:
                with probe.cursor() as cur:
                    probe_layout = pg_migrations._read_maintenance_bridge(cur)
            finally:
                probe.close()
        finally:
            with store.conn.cursor() as cur:
                cur.execute(
                    f"REVOKE SELECT ON sprintctl_schema_capability FROM {probe_role}"
                )
                cur.execute(f"REVOKE USAGE ON SCHEMA public FROM {probe_role}")
            store.conn.commit()

        assert probe_layout["available"] is True
        assert probe_layout["marker_version"] == owner_layout["marker_version"] == 1
        assert probe_layout["relation_count"] == owner_layout["relation_count"]
        assert (
            probe_layout["catalog_fingerprint"]
            == owner_layout["catalog_fingerprint"]
            == pg_migrations.MAINTENANCE_CATALOG_FINGERPRINT
        )
        assert (
            probe_layout["trigger_function_fingerprint"]
            == owner_layout["trigger_function_fingerprint"]
            == pg_migrations.MAINTENANCE_TRIGGER_FUNCTION_FINGERPRINT
        )

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
        [(1, _migrations_from(2)), (2, _migrations_from(3))],
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
                # Same rationale as the ``ref`` stub above: schema version 8
                # adds ``reservation`` with a foreign key onto ``work_item``,
                # and version 9 derives ``claim_history`` from ``claim``, so a
                # legacy_version=2 fixture has to stand in for the base tables
                # a real version-2+ deployment already carries. Only the
                # columns those two migrations depend on are reproduced.
                # legacy_version=1 replays the canonical PG_DDL instead, and
                # its CREATE TABLE IF NOT EXISTS would skip a stub, leaving a
                # work_item without the columns the rest of the schema needs.
                if legacy_version >= 2:
                    cur.execute(
                        """
                        CREATE TABLE work_item (
                            repo_id    text        NOT NULL,
                            id         bigint      GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                            title      text        NOT NULL DEFAULT '',
                            status     text        NOT NULL DEFAULT 'pending',
                            created_at timestamptz NOT NULL DEFAULT now(),
                            UNIQUE (repo_id, id)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE claim (
                            repo_id      text        NOT NULL,
                            id           bigint      GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                            work_item_id bigint      NOT NULL,
                            agent        text        NOT NULL,
                            claim_token  text,
                            status       text        NOT NULL DEFAULT 'active',
                            created_at   timestamptz NOT NULL DEFAULT now(),
                            expires_at   timestamptz NOT NULL DEFAULT now(),
                            UNIQUE (repo_id, id),
                            FOREIGN KEY (repo_id, work_item_id)
                                REFERENCES work_item(repo_id, id)
                                ON DELETE CASCADE
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
