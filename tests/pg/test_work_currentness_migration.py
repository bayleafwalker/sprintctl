"""Exact schema30→31 single-CHECK migration, fail-closed and rollback histories."""
import uuid

import pytest

from sprintctl import pg,pg_migrations,delegation_schema,work_currentness_schema,delegation_pg
from sprintctl.application import ApplicationRejection
from sprintctl.vuoro_adapter import catalog_operation_specs
from tests.pg._shared import PG_MARKS,_PG_URL,psycopg,dict_row,assert_disposable_connection,drop_schema30
from tests.pg.test_work_currentness import setup,delegated_scope,invoke,OP
from tests.pg.test_work_lease import A,_invoke

pytestmark=PG_MARKS


@pytest.fixture
def isolated():
    name='currentness_migration_'+uuid.uuid4().hex
    conn=psycopg.connect(_PG_URL,row_factory=dict_row)
    assert_disposable_connection(conn)
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"');cur.execute(f'SET search_path TO "{name}"')
    conn.commit()
    def connect(): return psycopg.connect(_PG_URL,row_factory=dict_row,options=f'-c search_path={name}')
    store=pg.PgStore(conn,'itest-currentness-migration',connection_factory=connect)
    pg.init_db(store)
    try: yield store
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SET search_path TO public');cur.execute(f'DROP SCHEMA "{name}" CASCADE')
        conn.commit();conn.close()


def reconstruct30(store):
    with store.conn.cursor() as cur:
        drop_schema30(cur)
        cur.execute('UPDATE schema_version SET version=30')
    store.conn.commit();store.remote_schema_version=30


def raw_delegate(store,identity):
    with store.conn.cursor() as cur:
        cur.execute('SELECT * FROM work_run_delegation WHERE repo_id=%s AND delegation_id=%s',(store.repo_id,identity))
        value=dict(cur.fetchone())
    store.conn.rollback();return value


def test_upgrade_preserves_existing_delegation_and_enables_only_new_read(isolated):
    store=isolated
    _,body=setup(store)
    minted,old=delegated_scope(store,body,operations=['work.run.resolve-v1'])
    before=raw_delegate(store,minted['delegation_id'])
    reconstruct30(store)
    assert OP not in {o['name'] for o in catalog_operation_specs(resource_schema_available=True,remote_schema_version=30)}
    assert pg_migrations.compatibility_handshake(store)['reason']=='schema-too-old'
    with pytest.raises(ApplicationRejection): invoke(store,body)
    with pytest.raises(ApplicationRejection) as unavailable:
        _invoke(store,'work.run.delegate-v1',{'run_id':body['run_id'],
            'creator_instance_id':before['creator_instance_id'],'operations':[OP],
            'ttl_seconds':300,'idempotency_key':uuid.uuid4().hex},A)
    assert unavailable.value.code=='owner-capability-unavailable'
    assert raw_delegate(store,minted['delegation_id'])==before
    result=pg_migrations.migrate_schema(store)
    assert result['applied_versions']==[31] and result['to_version']==31
    assert pg_migrations.migrate_schema(store)['applied_versions']==[]
    assert raw_delegate(store,minted['delegation_id'])==before
    assert delegation_pg.resolve_token(store,minted['token']).delegation.operations==('work.run.resolve-v1',)
    with pytest.raises(ApplicationRejection) as denied: invoke(store,body,old)
    assert denied.value.code=='delegation-operation-not-allowed'
    _invoke(store,'work.run.delegation-revoke-v1',{'delegation_id':minted['delegation_id'],'reason':'fixture-replaced',
        'idempotency_key':uuid.uuid4().hex},A)
    _,new=delegated_scope(store,body)
    assert invoke(store,body,new)['authorizes_effect'] is False


def test_failure_after_constraint_ddl_rolls_back_ledger_and_exact_old_policy(isolated,monkeypatch):
    reconstruct30(isolated)
    original=work_currentness_schema.install
    def fail(cur):
        original(cur)
        raise RuntimeError('injected failure after exact CHECK change')
    monkeypatch.setattr(work_currentness_schema,'install',fail)
    with pytest.raises(RuntimeError,match='injected failure'): pg_migrations.migrate_schema(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute('SELECT version FROM schema_version');assert cur.fetchone()['version']==30
        assert delegation_schema.inspect_catalog(cur)['available'] is True
    isolated.conn.rollback()


def test_foreign_delegation_constraint_refuses_without_repair_or_stamp(isolated):
    reconstruct30(isolated)
    with isolated.conn.cursor() as cur:
        cur.execute('ALTER TABLE work_run_delegation DROP CONSTRAINT work_run_delegation_operations_check')
        cur.execute("ALTER TABLE work_run_delegation ADD CONSTRAINT work_run_delegation_operations_check CHECK(jsonb_typeof(operations)='array')")
    isolated.conn.commit()
    for _ in range(2):
        with pytest.raises(pg_migrations.RemoteSchemaMigrationError): pg_migrations.migrate_schema(isolated)
        with isolated.conn.cursor() as cur:
            cur.execute('SELECT version FROM schema_version');assert cur.fetchone()['version']==30
            cur.execute("SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid='work_run_delegation'::regclass AND conname='work_run_delegation_operations_check'")
            assert 'currentness' not in cur.fetchone()['definition']
        isolated.conn.rollback()
