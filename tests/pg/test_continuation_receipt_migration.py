"""Schema 29 to 30 cutover and rollback on a disposable owner."""
import json
import uuid
import pytest
from sprintctl import pg, pg_migrations, continuation_receipt_schema as schema
from sprintctl import operator_request as contract, operator_request_pg as storage
from tests.test_operator_request_pg import _packet
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row, assert_disposable_connection

pytestmark = PG_MARKS


@pytest.fixture
def isolated():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    name = 'receipt_migration_' + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'SET search_path TO "{name}"')
    conn.commit()
    store = pg.PgStore(conn, 'itest-receipt-migration')
    pg.init_db(store)
    try:
        yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SET search_path TO public')
            cur.execute(f'DROP SCHEMA "{name}" CASCADE')
        conn.commit(); conn.close()


def reconstruct29(store):
    with store.conn.cursor() as cur:
        cur.execute('DROP TABLE operator_continuation_receipt')
        cur.execute('DROP FUNCTION sprintctl_continuation_receipt_append_only()')
        cur.execute('UPDATE schema_version SET version=29')
    store.conn.commit()


def test_upgrade_preserves_request_and_is_repeatable(isolated):
    store = isolated
    sprint = pg.create_sprint(store, 'preserve', status='active')
    track = pg.get_or_create_track(store, sprint, 'preserve')
    item = pg.create_work_item(store, sprint, track, 'preserve')
    identity = 'req_' + uuid.uuid4().hex[:26].upper()
    args = contract.publish_arguments({'request_id': identity, 'packet': _packet(),
        'dependants': [{'work_item_id': item, 'session_id': 'session-a'}], 'idempotency_key': str(uuid.uuid4())})
    with store.conn.transaction(), store.conn.cursor() as cur:
        storage.publish(cur, store, 'principal-a', 'workspace-a', args)
    with store.conn.cursor() as cur:
        before = storage.get(cur, store, 'workspace-a', identity)
    store.conn.commit()
    reconstruct29(store)
    assert pg_migrations.compatibility_handshake(store)['reason'] == 'schema-too-old'
    assert pg_migrations.migrate_schema(store)['applied_versions'] == [30]
    assert pg_migrations.migrate_schema(store)['applied_versions'] == []
    with store.conn.cursor() as cur:
        after = storage.get(cur, store, 'workspace-a', identity)
        assert schema.inspect_catalog(cur)['available'] is True
    store.conn.rollback()
    assert after == before


def test_preexisting_collision_refuses_repeatedly_without_repair(isolated):
    reconstruct29(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute('CREATE TABLE operator_continuation_receipt (foreign_value integer)')
        cur.execute('INSERT INTO operator_continuation_receipt VALUES (42)')
    isolated.conn.commit()
    for _ in range(2):
        with pytest.raises(pg_migrations.RemoteSchemaMigrationError):
            pg_migrations.migrate_schema(isolated)
        with isolated.conn.cursor() as cur:
            cur.execute('SELECT version FROM schema_version'); assert cur.fetchone()['version'] == 29
            cur.execute('SELECT foreign_value FROM operator_continuation_receipt'); assert cur.fetchone()['foreign_value'] == 42
        isolated.conn.rollback()


def test_failed_migration_rolls_back_schema_and_version(isolated, monkeypatch):
    reconstruct29(isolated)
    original = schema.install
    def fail_after_ddl(cur):
        original(cur)
        raise RuntimeError('injected failure after receipt DDL')
    monkeypatch.setattr(schema, 'install', fail_after_ddl)
    with pytest.raises(RuntimeError, match='injected failure'):
        pg_migrations.migrate_schema(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute('SELECT version FROM schema_version'); assert cur.fetchone()['version'] == 29
        cur.execute("SELECT to_regclass('operator_continuation_receipt') AS name"); assert cur.fetchone()['name'] is None
    isolated.conn.rollback()
