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
        return self

    def fetchone(self):
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
        raise AssertionError(f"unexpected fetch for query: {self._query}")


class _SchemaConnection:
    def __init__(self, version=2, *, version_rows=1, fail_on=None):
        self.calls = []
        self.version = version
        self.version_rows = version_rows if version is not None else 0
        self.fail_on = fail_on
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _SchemaCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _store(version=2, **kwargs):
    conn = _SchemaConnection(version, **kwargs)
    return pg.PgStore(conn=conn, repo_id="test-repo"), conn


def test_runtime_compatibility_probe_is_read_only_and_publishes_work_api():
    store, conn = _store(2)

    handshake = pg.require_compatible_schema(store)

    assert handshake == {
        "schema_version": "sprintctl-work-compatibility/v1",
        "work_api_version": "sprintctl-work/v1",
        "remote_schema": {"actual": 2, "minimum": 2, "maximum": 2},
        "compatible": True,
        "reason": None,
    }
    assert [query for query, _ in conn.calls] == [
        "SELECT to_regclass('schema_version') AS relation",
        "SELECT COUNT(*) AS row_count, MIN(version) AS minimum_version, "
        "MAX(version) AS maximum_version FROM schema_version",
    ]


@pytest.mark.parametrize(
    ("version", "reason"),
    [(None, "schema-version-table-missing"), (1, "schema-too-old"), (3, "schema-too-new")],
)
def test_runtime_startup_fails_closed_for_missing_old_and_new_schema(version, reason):
    store, _conn = _store(version)

    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match=reason):
        pg.require_compatible_schema(store)


def test_runtime_startup_rejects_ambiguous_schema_ledger():
    store, _conn = _store(2, version_rows=2)

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
    assert conn.version == 2
    assert conn.commits == 1
    assert conn.rollbacks == 1  # release the post-migration read transaction
    assert result["from_version"] == 1
    assert result["to_version"] == 2
    assert result["applied_versions"] == [2]


def test_migration_bootstraps_a_missing_schema_before_advancing():
    store, conn = _store(None)

    result = pg.migrate_schema(store)

    assert sum(query == pg.PG_DDL for query, _ in conn.calls) == 2
    assert result["from_version"] is None
    assert result["applied_versions"] == [2]
    assert conn.version == 2


def test_migration_is_idempotent_at_current_schema():
    store, conn = _store(2)

    first = pg.migrate_schema(store)
    second = pg.migrate_schema(store)

    assert first["applied_versions"] == []
    assert second["applied_versions"] == []
    assert not any(query == pg.PG_DDL for query, _ in conn.calls)
    assert conn.commits == 2


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


def test_normal_startup_mode_never_enters_migration(monkeypatch):
    store, conn = _store(2)
    monkeypatch.setattr(
        pg_migrations,
        "migrate_schema",
        lambda _store: pytest.fail("normal startup attempted migration"),
    )

    handshake = pg_migrations.startup_schema_handshake(store, {})

    assert handshake["compatible"] is True
    assert conn.rollbacks == 1


def test_operator_compatibility_mode_is_explicit(monkeypatch):
    store, conn = _store(2)
    calls = []
    monkeypatch.setattr(pg_migrations, "migrate_schema", lambda value: calls.append(value))

    pg_migrations.startup_schema_handshake(
        store,
        {pg_migrations.STARTUP_MODE_ENV: pg_migrations.OPERATOR_MIGRATE_STARTUP_MODE},
    )

    assert calls == [store]
    assert conn.rollbacks == 1


def test_unknown_startup_mode_fails_before_any_database_query():
    store, conn = _store(2)

    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError, match="invalid"):
        pg_migrations.startup_schema_handshake(
            store,
            {pg_migrations.STARTUP_MODE_ENV: "auto"},
        )

    assert conn.calls == []
