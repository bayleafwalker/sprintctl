"""Disposable PostgreSQL schema 32 and private consent transactions."""
import threading
import uuid
from dataclasses import replace

import pytest

from sprintctl import pg, pg_migrations, recipient_binding_schema as schema
from sprintctl import recipient_binding_pg as binding, operator_request_pg as requests
from sprintctl.operator_request import RequestRefused
from sprintctl.agent_inbox import InboxRefused
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row, assert_disposable_connection
from tests.test_operator_request_pg import _publish
from tests.test_recipient_provenance import provenance

pytestmark = PG_MARKS


@pytest.fixture
def case():
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    name = 'recipient_' + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'SET search_path TO "{name}"')
    conn.commit()
    store = pg.PgStore(conn, 'recipient-repo')
    pg.init_db(store)
    sprint = pg.create_sprint(store, 'test', status='active')
    track = pg.get_or_create_track(store, sprint, 'test')
    item = pg.create_work_item(store, sprint, track, 'test')
    request = 'req_' + uuid.uuid4().hex[:26].upper()
    _publish(store, item, request)
    instance = 'ins_' + 'A' * 26
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())', (store.repo_id, 'workspace', instance, 'principal'))
    caller = replace(provenance(), workspace_id='workspace-a')
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())', (store.repo_id, 'workspace-a', instance, 'principal'))
    yield store, name, request, item, instance, caller
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SET search_path TO public')
        cur.execute(f'DROP SCHEMA "{name}" CASCADE')
    conn.commit()
    conn.close()


def do_bind(case, caller=None, **kwargs):
    store, _, request, item, instance, default = case
    args = dict(request_id=request, work_item_id=item, instance_id=instance) | kwargs
    with store.conn.transaction(), store.conn.cursor() as cur:
        return binding.bind(cur, store, caller or default, **args)


def test_migration_fingerprint_and_rollback(case, monkeypatch):
    store = case[0]
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert schema.inspect_catalog(cur)['available']
        cur.execute('DROP TABLE operator_request_recipient_binding')
        cur.execute('UPDATE schema_version SET version=31')
    original = schema.install
    def fail(cur):
        original(cur)
        raise RuntimeError('failure after DDL')
    monkeypatch.setattr(schema, 'install', fail)
    with pytest.raises(RuntimeError):
        pg_migrations.migrate_schema(store)
    with store.conn.cursor() as cur:
        cur.execute("SELECT version, to_regclass('operator_request_recipient_binding') AS relation FROM schema_version")
        assert cur.fetchone() == {'version': 31, 'relation': None}
    store.conn.rollback()
    monkeypatch.setattr(schema, 'install', original)
    assert pg_migrations.migrate_schema(store)['applied_versions'] == [32]
    assert pg_migrations.migrate_schema(store)['applied_versions'] == []
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('ALTER TABLE operator_request_recipient_binding ADD COLUMN drift integer')
        assert not schema.inspect_catalog(cur)['complete']


def test_bind_withdraw_and_foreign_refusals(case):
    store, _, request, item, instance, caller = case
    result = do_bind(case)
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT * FROM operator_request_recipient_binding WHERE binding_id=%s', (result['binding_id'],))
        row = cur.fetchone()
        assert row['resource'] == caller.resource and set(row['token_scopes']) == caller.token_scopes
        assert row['effective_authority'] == ['work:read']
    for change in ({'workspace_id': 'elsewhere'}, {'principal_id': 'foreign'}):
        with pytest.raises((RequestRefused, InboxRefused)):
            do_bind(case, replace(caller, **change))
    with pytest.raises(RequestRefused):
        do_bind(case, work_item_id=item + 99999)
    with pytest.raises(RequestRefused):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, replace(caller, principal_id='foreign'), request_id=request, binding_id=result['binding_id'], expected_revision=1)
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert binding.withdraw(cur, store, caller, request_id=request, binding_id=result['binding_id'], expected_revision=1)['revision'] == 2
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, caller, request_id=request, binding_id=result['binding_id'], expected_revision=1)
    assert exc.value.code == 'revision-conflict'
    assert do_bind(case)['binding_id'] != result['binding_id']


def test_concurrent_withdraw_cas(case):
    store, name, request, _, _, caller = case
    identifier = do_bind(case)['binding_id']
    barrier = threading.Barrier(2)
    outcomes = []
    def run():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    barrier.wait(10)
                    binding.withdraw(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request, binding_id=identifier, expected_revision=1)
                conn.commit()
            outcomes.append('committed')
        except RequestRefused as exc:
            outcomes.append(exc.code)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert all(not t.is_alive() for t in threads)
    assert sorted(outcomes) == ['committed', 'revision-conflict']


def test_instance_retirement_refuses(case):
    store, _, request, item, instance, caller = case
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('UPDATE agent_instance SET retired_at=now(), retire_reason=%s WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s', ('test', store.repo_id, caller.workspace_id, instance))
    with pytest.raises(RequestRefused) as exc:
        do_bind(case)
    assert exc.value.code == "instance-retired"


def test_catalog_is_visible_without_table_privileges(case):
    import os
    url = os.environ.get('SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL')
    if not url:
        pytest.skip('requires disposable unprivileged catalog probe role')
    store, name, *_ = case
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute(f'GRANT USAGE ON SCHEMA "{name}" TO PUBLIC')
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{name}"')
            assert schema.inspect_catalog(cur)['available']
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute('SELECT * FROM operator_request_recipient_binding')


def test_bind_holds_instance_lock_until_commit(case):
    store, name, request, item, instance, caller = case
    locked, release, retired = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def bind_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request, work_item_id=item, instance_id=instance)
                    locked.set()
                    assert release.wait(10)
        except Exception as exc:
            errors.append(exc)
    def retire_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    cur.execute('UPDATE agent_instance SET retired_at=now(), retire_reason=%s WHERE repo_id=%s AND workspace_id=%s AND instance_id=%s', ('test', store.repo_id, caller.workspace_id, instance))
            retired.set()
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=bind_worker); first.start()
    assert locked.wait(10)
    second = threading.Thread(target=retire_worker); second.start()
    try:
        assert not retired.wait(.2)
    finally:
        release.set()
        first.join(10); second.join(10)
    assert not first.is_alive() and not second.is_alive() and not errors and retired.is_set()
    with pytest.raises(RequestRefused) as exc:
        do_bind(case)
    assert exc.value.code == 'instance-retired'
