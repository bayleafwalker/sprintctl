"""Schema 21 upgrade/rollback oracle in disposable, isolated schemas."""
import uuid

import pytest

from sprintctl import pg, pg_migrations
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg
from tests.pg.test_protected_artifact_acceptance import setup
from tests.pg.test_effect_intent import _accept, _get

pytestmark = PG_MARKS


@pytest.fixture
def migration_store(pg_test_scope, store):
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    schema = "artifact_upgrade_" + uuid.uuid4().hex
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        store = pg.PgStore(conn, pg_test_scope("artifact-upgrade"))
        pg_migrations.migrate_schema(store)
        yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


def make_schema20(store):
    # The v19 guard and all v20 tables remain intact. Remove only v21's
    # additive metadata, then set the ledger to that exact historical shape.
    with store.conn.cursor() as cur:
        cur.execute("DROP TRIGGER sprintctl_effect_verification_guard ON work_effect_intent")
        cur.execute("DROP FUNCTION sprintctl_effect_verification_guard()")
        cur.execute("ALTER TABLE work_effect_intent DROP COLUMN release_digest, DROP COLUMN verification_binding")
        cur.execute("UPDATE schema_version SET version=20")
    store.conn.commit()


def test_upgrade_preserves_legacy_acceptance_and_is_idempotent(migration_store):
    store = migration_store
    _, _, intent = setup(store, required=False)
    accepted = _accept(store, intent)
    accepted.pop("release_digest")
    make_schema20(store)
    with pytest.raises(pg_migrations.RemoteSchemaCompatibilityError):
        pg_migrations.require_compatible_schema(store)
    store.conn.rollback()
    with store.conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name='work_effect_intent' AND column_name IN ('release_digest','verification_binding')")
        assert cur.fetchall() == []  # read-only startup has not migrated
    store.conn.rollback()
    result = pg_migrations.migrate_schema(store)
    assert result["applied_versions"] == [21]
    assert _get(store, intent["intent_id"]) == accepted
    assert pg_migrations.migrate_schema(store)["applied_versions"] == []
    assert _get(store, intent["intent_id"]) == accepted
    # The old schema-20 admission range cannot admit the upgraded database.
    actual = result["compatibility"]["remote_schema"]["actual"]
    assert actual == 21 and not 20 <= actual <= 20


@pytest.mark.parametrize("column, sql_type", [
    ("release_digest", "integer"), ("release_digest", "text"),
    ("verification_binding", "text"), ("verification_binding", "jsonb"),
])
def test_foreign_binding_columns_are_refused_atomically(migration_store, column, sql_type):
    store = migration_store
    make_schema20(store)
    with store.conn.cursor() as cur:
        cur.execute(f"ALTER TABLE work_effect_intent ADD COLUMN {column} {sql_type}")
    store.conn.commit()
    with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="binding column"):
        pg_migrations.migrate_schema(store)
    with store.conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        assert cur.fetchone()["version"] == 20
        cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='work_effect_intent' "
                    "AND column_name IN ('release_digest','verification_binding')")
        assert cur.fetchall() == [{"column_name": column, "data_type": sql_type}]
    store.conn.rollback()
