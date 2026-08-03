"""Compatibility and migration coverage for the shared PostgreSQL schema."""

from __future__ import annotations

import pytest

from sprintctl import pg, pg_migrations


class _SchemaCursor:
    def __init__(self, conn):
        self._conn = conn
        self._query = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self._query = query
        self._conn.calls.append((query, params))
        if self._conn.fail_on and self._conn.fail_on in query:
            raise RuntimeError("migration failed")
        if query == pg.PG_DDL and self._conn.version is None:
            self._conn.version = 1
            self._conn.version_rows = 1
        if query.startswith("UPDATE schema_version SET version"):
            self._conn.version = int(params[0])
        if "CREATE TABLE IF NOT EXISTS maintenance_capability (" in query:
            self._conn.maintenance_relations = max(self._conn.maintenance_relations, 3)
        if "CREATE TABLE IF NOT EXISTS sprintctl_schema_capability" in query:
            self._conn.maintenance_relations = 4
        if "INSERT INTO sprintctl_schema_capability" in query:
            self._conn.marker_version = 1
        if "CREATE TRIGGER maintenance_capability_recovery_immutable" in query:
            self._conn.maintenance_triggers = 2
        return self

    def fetchone(self):
        if "AS catalog_fingerprint" in self._query:
            if self._conn.maintenance_relations == 4 and self._conn.maintenance_triggers == 2:
                fingerprint = pg_migrations.MAINTENANCE_CATALOG_FINGERPRINT
            elif self._conn.maintenance_relations == 3 and self._conn.maintenance_triggers == 2:
                fingerprint = pg_migrations.LEGACY_SCHEMA6_MAINTENANCE_CATALOG_FINGERPRINT
            else:
                fingerprint = "malformed"
            return {
                "relation_count": self._conn.maintenance_relations,
                "catalog_fingerprint": fingerprint,
            }
        if "AS function_body FROM pg_proc" in self._query:
            body = (
                "BEGIN RAISE EXCEPTION 'maintenance capability receipt/recovery "
                "records are immutable'; END;"
                if self._conn.function_valid else "BEGIN RETURN NEW; END;"
            )
            return {
                "expected_schema": "public",
                "function_schema": self._conn.function_schema,
                "function_body": body,
            }
        if "SELECT version FROM sprintctl_schema_capability" in self._query:
            return {"version": self._conn.marker_version}
        if "to_regclass" in self._query:
            relation = "schema_version" if self._conn.version is not None else None
            return {"relation": relation}
        if "COUNT(*) AS row_count" in self._query:
            return {
                "row_count": self._conn.version_rows,
                "minimum_version": self._conn.version,
                "maximum_version": self._conn.version,
            }
        if "pg_get_serial_sequence" in self._query:
            return {"seq": None}
        if "column_name = 'ingest_id'" in self._query:
            return {"ingest_id_exists": False}
        raise AssertionError(f"unexpected fetch for query: {self._query}")


class _SchemaConnection:
    def __init__(self, version=4, *, version_rows=1, fail_on=None, maintenance_relations=None, maintenance_triggers=None, function_schema="public", function_valid=True):
        self.calls = []
        self.version = version
        self.version_rows = version_rows if version is not None else 0
        self.fail_on = fail_on
        self.maintenance_relations = (4 if version == 6 else 0) if maintenance_relations is None else maintenance_relations
        self.maintenance_triggers = (2 if self.maintenance_relations == 4 else 0) if maintenance_triggers is None else maintenance_triggers
        self.marker_version = 1 if self.maintenance_relations == 4 else None
        self.function_schema = function_schema
        self.function_valid = function_valid
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _SchemaCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _store(version=4, **kwargs):
    conn = _SchemaConnection(version, **kwargs)
    return pg.PgStore(conn=conn, repo_id="test-repo"), conn


def test_snapshot_uses_store_connection_factory_instead_of_redacted_dsn(monkeypatch):
    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def execute(self, statement):
            assert statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

    class Connection:
        closed = False

        def transaction(self):
            return Context()

        def cursor(self):
            return Context()

        def close(self):
            self.closed = True

    sibling = Connection()
    store = pg.PgStore(
        conn=object(), repo_id="agentops", connection_factory=lambda: sibling
    )
    monkeypatch.setattr(pg, "_PSYCOPG_AVAILABLE", True)

    with pg.repeatable_read_snapshot(store) as snapshot:
        assert snapshot.conn is sibling
        assert snapshot.connection_factory is store.connection_factory

    assert sibling.closed is True


def test_runtime_compatibility_probe_is_read_only_and_publishes_work_api():
    store, conn = _store(6)

    handshake = pg.require_compatible_schema(store)

    assert handshake == {
        "schema_version": "sprintctl-work-compatibility/v1",
        "work_api_version": "sprintctl-work/v1",
        "remote_schema": {"actual": 6, "minimum": 5, "maximum": 6},
        "compatible": True,
        "reason": None,
        "capabilities": {
            "maintenance_storage": {
                "schema_version": "sprintctl-maintenance-storage/v1",
                "available": True,
                "complete": True,
                "relation_count": 4,
                "catalog_fingerprint": pg_migrations.MAINTENANCE_CATALOG_FINGERPRINT,
                "expected_catalog_fingerprint": pg_migrations.MAINTENANCE_CATALOG_FINGERPRINT,
                "trigger_function_schema": "public",
                "expected_schema": "public",
                "trigger_function_fingerprint": pg_migrations.MAINTENANCE_TRIGGER_FUNCTION_FINGERPRINT,
                "expected_trigger_function_fingerprint": pg_migrations.MAINTENANCE_TRIGGER_FUNCTION_FINGERPRINT,
                "marker_version": 1,
            },
            "repository_ingest_cursor": {
                "schema_version": "sprintctl-repository-ingest-cursor/v1",
                "scope": "repository",
                "contiguous": True,
            }
        },
    }
    queries = [query for query, _ in conn.calls]
    assert all(query.lstrip().startswith(("SELECT", "WITH")) for query in queries)
    assert any("information_schema.columns" in query for query in queries)


def test_schema5_with_complete_staged_maintenance_storage_is_compatible():
    store, _conn = _store(5, maintenance_relations=4)
    handshake = pg.require_compatible_schema(store)
    assert handshake["compatible"] is True
    assert handshake["remote_schema"]["actual"] == 5
    assert handshake["capabilities"]["maintenance_storage"]["available"] is True


def test_schema5_without_bridge_and_every_partial_bridge_fail_closed():
    for relations, reason in ((0, "maintenance-storage-missing"), (1, "maintenance-storage-partial"), (2, "maintenance-storage-partial"), (3, "maintenance-storage-partial")):
        store, _conn = _store(5, maintenance_relations=relations)
        with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match=reason):
            pg.require_compatible_schema(store)


def test_schema5_bridge_with_missing_immutability_trigger_fails_closed():
    store, _conn = _store(5, maintenance_relations=4, maintenance_triggers=1)
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match="maintenance-storage-partial"):
        pg.require_compatible_schema(store)


@pytest.mark.parametrize(
    "kwargs",
    ({"function_schema": "decoy"}, {"function_valid": False}),
)
def test_schema5_bridge_rejects_wrong_or_mutated_trigger_function(kwargs):
    store, _conn = _store(5, maintenance_relations=4, **kwargs)
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match="maintenance-storage-partial"):
        pg.require_compatible_schema(store)


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        (None, "schema-version-table-missing"),
        (1, "schema-too-old"),
        (2, "schema-too-old"),
        (7, "schema-too-new"),
    ],
)
def test_runtime_startup_fails_closed_for_missing_old_and_new_schema(version, reason):
    store, _conn = _store(version)

    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match=reason):
        pg.require_compatible_schema(store)


def test_runtime_startup_rejects_ambiguous_schema_ledger():
    store, _conn = _store(3, version_rows=2)

    with pytest.raises(
        pg_migrations.RemoteSchemaMigrationError,
        match="exactly one unambiguous",
    ):
        pg.require_compatible_schema(store)


def test_migration_serializes_and_advances_legacy_schema_once():
    store, conn = _store(1)

    result = pg.migrate_schema(store)

    assert conn.calls[0] == (
        "SELECT pg_advisory_xact_lock(%s, %s)",
        pg_migrations.SCHEMA_MIGRATION_LOCK_KEYS,
    )
    assert sum(query == pg.PG_DDL for query, _ in conn.calls) == 1
    assert ("UPDATE schema_version SET version = %s", (2,)) in conn.calls
    assert ("UPDATE schema_version SET version = %s", (3,)) in conn.calls
    assert ("UPDATE schema_version SET version = %s", (4,)) in conn.calls
    assert ("UPDATE schema_version SET version = %s", (5,)) in conn.calls
    assert ("UPDATE schema_version SET version = %s", (6,)) in conn.calls
    assert conn.version == 6
    assert conn.commits == 1
    assert conn.rollbacks == 1  # release the post-migration read transaction
    assert result["from_version"] == 1
    assert result["to_version"] == 6
    assert result["applied_versions"] == [2, 3, 4, 5, 6]


def test_migration_bootstraps_a_missing_schema_before_advancing():
    store, conn = _store(None)

    result = pg.migrate_schema(store)

    assert sum(query == pg.PG_DDL for query, _ in conn.calls) == 2
    assert result["from_version"] is None
    assert result["applied_versions"] == [2, 3, 4, 5, 6]
    assert conn.version == 6


def test_migration_is_idempotent_at_current_schema():
    store, conn = _store(6)

    first = pg.migrate_schema(store)
    second = pg.migrate_schema(store)

    assert first["applied_versions"] == []
    assert second["applied_versions"] == []
    assert not any(query == pg.PG_DDL for query, _ in conn.calls)
    assert conn.commits == 2


def test_migration_marks_only_exact_legacy_schema6_layout():
    store, conn = _store(6, maintenance_relations=3, maintenance_triggers=2)
    result = pg.migrate_schema(store)
    assert result["applied_versions"] == []
    assert conn.maintenance_relations == 4
    assert conn.marker_version == 1
    assert result["compatibility"]["compatible"] is True


def test_migration_rejects_unrecognized_partial_schema6_layout():
    store, conn = _store(6, maintenance_relations=2, maintenance_triggers=0)
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="partial"):
        pg.migrate_schema(store)
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_stage_schema5_bridge_is_additive_and_does_not_advance_ledger():
    store, conn = _store(5, maintenance_relations=0)
    result = pg_migrations.stage_schema5_maintenance_bridge(store)
    assert conn.version == 5
    assert conn.commits == 1
    assert result["installed"] is True
    assert result["compatibility"]["compatible"] is True


def test_stage_schema5_bridge_rejects_partial_without_repair():
    store, conn = _store(5, maintenance_relations=2)
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="partial"):
        pg_migrations.stage_schema5_maintenance_bridge(store)
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_stage_schema5_bridge_rolls_back_if_marker_write_fails():
    store, conn = _store(
        5,
        maintenance_relations=0,
        fail_on="INSERT INTO sprintctl_schema_capability",
    )
    with pytest.raises(RuntimeError, match="migration failed"):
        pg_migrations.stage_schema5_maintenance_bridge(store)
    assert conn.version == 5
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_migration_rolls_back_if_upgrade_fails_after_lock_acquisition():
    store, conn = _store(1, fail_on="ALTER TABLE ingest_record")

    with pytest.raises(RuntimeError, match="migration failed"):
        pg.migrate_schema(store)

    assert conn.calls[0] == (
        "SELECT pg_advisory_xact_lock(%s, %s)",
        pg_migrations.SCHEMA_MIGRATION_LOCK_KEYS,
    )
    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert conn.version == 1


def test_version_2_migration_rolls_back_without_advancing_cursor_schema():
    store, conn = _store(2, fail_on="RENAME COLUMN ingest_offset")

    with pytest.raises(RuntimeError, match="migration failed"):
        pg.migrate_schema(store)

    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert conn.version == 2


def test_normal_startup_mode_never_enters_migration(monkeypatch):
    store, conn = _store(6)
    monkeypatch.setattr(
        pg_migrations,
        "migrate_schema",
        lambda _store: pytest.fail("normal startup attempted migration"),
    )

    handshake = pg_migrations.startup_schema_handshake(store, {})

    assert handshake["compatible"] is True
    assert conn.rollbacks == 1


def test_operator_compatibility_mode_is_explicit(monkeypatch):
    store, conn = _store(6)
    calls = []
    monkeypatch.setattr(pg_migrations, "migrate_schema", lambda value: calls.append(value))

    pg_migrations.startup_schema_handshake(
        store,
        {pg_migrations.STARTUP_MODE_ENV: pg_migrations.OPERATOR_MIGRATE_STARTUP_MODE},
    )

    assert calls == [store]
    assert conn.rollbacks == 1


def test_unknown_startup_mode_fails_before_any_database_query():
    store, conn = _store(4)

    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match="invalid"):
        pg_migrations.startup_schema_handshake(
            store,
            {pg_migrations.STARTUP_MODE_ENV: "auto"},
        )

    assert conn.calls == []
