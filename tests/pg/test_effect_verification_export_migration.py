"""Explicit index/schema24 qualification under disposable PostgreSQL16."""
import uuid

import pytest
from sprintctl import pg, pg_migrations
from sprintctl.effect_verification_export_index import INDEX_NAME, inspect_catalog, install
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row, assert_disposable_connection, drop_schema25

pytestmark = PG_MARKS


@pytest.fixture
def isolated():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    schema = "q4_index_" + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()
    store = pg.PgStore(conn, "itest-q4-index")
    pg.init_db(store)
    try:
        yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.commit(); conn.close()


def reconstruct23(store):
    with store.conn.cursor() as cur:
        drop_schema25(cur)
        cur.execute("DROP INDEX " + INDEX_NAME)
        cur.execute("UPDATE schema_version SET version=23")
    store.conn.commit()


def test_exact_index_explicit_upgrade_readonly_admission_and_idempotent_migration(isolated):
    store = isolated
    reconstruct23(store)
    assert pg_migrations.compatibility_handshake(store)["reason"] == "schema-too-old"
    with store.conn.cursor() as cur:
        assert inspect_catalog(cur) == {"available": False, "complete": True}
    store.conn.rollback()
    result = pg_migrations.migrate_schema(store)
    assert result["applied_versions"] == [24, 25] and result["to_version"] == 25
    with store.conn.cursor() as cur:
        assert inspect_catalog(cur) == {"available": True, "complete": True}
    store.conn.rollback()
    assert pg_migrations.migrate_schema(store)["applied_versions"] == []
    assert pg_migrations.compatibility_handshake(store)["compatible"] is True


@pytest.mark.parametrize("ddl", [
    "CREATE INDEX {name} ON evidence_item (digest)",
    "CREATE INDEX {name} ON evidence_item USING gin(claims jsonb_ops) WHERE kind='protected-artifact-verification'",
    "CREATE INDEX {name} ON evidence_item USING gin(claims jsonb_path_ops)",
    "CREATE INDEX {name} ON evidence_item USING gin(provenance jsonb_path_ops) WHERE kind='protected-artifact-verification'",
])
def test_existing_wrong_index_refuses_without_schema_or_index_repair(isolated, ddl):
    store = isolated
    reconstruct23(store)
    with store.conn.cursor() as cur: cur.execute(ddl.format(name=INDEX_NAME))
    store.conn.commit()
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="collision"):
        pg_migrations.migrate_schema(store)
    with store.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 23
        assert inspect_catalog(cur)["complete"] is False
    store.conn.rollback()


def test_missing_index_on_schema24_is_unavailable_without_runtime_ddl(isolated):
    with isolated.conn.cursor() as cur: cur.execute("DROP INDEX " + INDEX_NAME)
    isolated.conn.commit()
    assert pg_migrations.compatibility_handshake(isolated)["reason"] == "verification-export-index-missing"
    with isolated.conn.cursor() as cur:
        assert inspect_catalog(cur)["available"] is False
    isolated.conn.rollback()


def test_same_named_nonindex_relation_refuses_atomically(isolated):
    reconstruct23(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute("CREATE VIEW " + INDEX_NAME + " AS SELECT 42 AS foreign_value")
    isolated.conn.commit()
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="collision"):
        pg_migrations.migrate_schema(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 23
        cur.execute("SELECT foreign_value FROM " + INDEX_NAME)
        assert cur.fetchone()["foreign_value"] == 42
    isolated.conn.rollback()


def test_index_ddl_failure_rolls_back_index_and_ledger(isolated, monkeypatch):
    from sprintctl import effect_verification_export_index as index
    reconstruct23(isolated)
    original = index.DDL
    monkeypatch.setattr(index, "DDL", original + "; SELECT 1/0")
    with pytest.raises(psycopg.errors.DivisionByZero):
        pg_migrations.migrate_schema(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 23
        assert inspect_catalog(cur) == {"available": False, "complete": True}
    isolated.conn.rollback()
    monkeypatch.setattr(index, "DDL", original)
    assert pg_migrations.migrate_schema(isolated)["applied_versions"] == [24, 25]


@pytest.mark.parametrize("flag", ["indisvalid", "indisready"])
def test_catalog_admission_rejects_invalid_or_unready_native_index(isolated, flag):
    # Exercise the exact PostgreSQL catalog result and mutate only the lifecycle
    # flag. No system catalog writes or superuser role are needed by this oracle.
    with isolated.conn.cursor() as cur:
        class LifecycleCursor:
            def execute(self, *args):
                cur.execute(*args)
            def fetchone(self):
                row = cur.fetchone()
                if row is not None and flag in row:
                    row[flag] = False
                return row
        assert inspect_catalog(LifecycleCursor()) == {"available": True, "complete": False}
    isolated.conn.rollback()


@pytest.mark.parametrize("phase", ["unready", "ready"])
def test_actual_interrupted_concurrent_index_is_not_admitted(isolated, phase):
    import threading
    import time
    reconstruct23(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute("SELECT current_schema() AS name")
        namespace = cur.fetchone()["name"]
    isolated.conn.rollback()
    worker = psycopg.connect(_PG_URL, row_factory=dict_row, autocommit=True)
    observer = psycopg.connect(_PG_URL, row_factory=dict_row, autocommit=True)
    blocker = psycopg.connect(_PG_URL, row_factory=dict_row)
    for connection in (worker, observer, blocker):
        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{namespace}"')
    blocker.commit()
    failures = []
    try:
        with blocker.cursor() as cur:
            if phase == "unready":
                cur.execute("LOCK TABLE evidence_item IN ROW EXCLUSIVE MODE")
            else:
                # This old MVCC snapshot makes the final concurrent-index phase
                # wait after building, with ready=true but valid=false.
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("SELECT count(*) FROM evidence_item")
        def build():
            try:
                with worker.cursor() as cur:
                    cur.execute("CREATE INDEX CONCURRENTLY " + INDEX_NAME +
                                " ON evidence_item USING gin(claims pg_catalog.jsonb_path_ops) "
                                "WHERE kind='protected-artifact-verification'")
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=build)
        thread.start()
        expected_ready = phase == "ready"
        observed = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with observer.cursor() as cur:
                cur.execute("SELECT i.indisvalid, i.indisready FROM pg_index i "
                            "JOIN pg_class c ON c.oid=i.indexrelid "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=current_schema() AND c.relname=%s", (INDEX_NAME,))
                observed = cur.fetchone()
            if observed == {"indisvalid": False, "indisready": expected_ready}:
                break
            time.sleep(0.01)
        worker.cancel()
        thread.join(5)
        assert not thread.is_alive()
        assert observed == {"indisvalid": False, "indisready": expected_ready}
        assert len(failures) == 1 and isinstance(failures[0], psycopg.errors.QueryCanceled)
        blocker.rollback()
        with isolated.conn.cursor() as cur:
            assert inspect_catalog(cur) == {"available": True, "complete": False}
        isolated.conn.rollback()
        with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="collision"):
            pg_migrations.migrate_schema(isolated)
    finally:
        worker.cancel()
        blocker.rollback()
        if "thread" in locals():
            thread.join(5)
        worker.close()
        observer.close()
        blocker.close()
