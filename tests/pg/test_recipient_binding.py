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
from tests.test_recipient_provenance import provenance, owner_identity

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
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())', (store.repo_id, 'workspace', instance, provenance().principal_id))
    caller = replace(provenance(), workspace_id='workspace-a')
    with conn.transaction(), conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())', (store.repo_id, 'workspace-a', instance, caller.principal_id))
    yield store, name, request, item, instance, caller
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SET search_path TO public')
        cur.execute(f'DROP SCHEMA "{name}" CASCADE')
    conn.commit()
    conn.close()


def do_bind(case, caller=None, **kwargs):
    store, _, request, item, instance, default = case
    args = dict(request_id=request, work_item_id=item, instance_id=instance, idempotency_key=str(uuid.uuid4())) | kwargs
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
    for change in ({'workspace_id': 'elsewhere'}, {'principal_id': 'issuer:foreign:1', 'subject': 'foreign'}):
        with pytest.raises((RequestRefused, InboxRefused)):
            do_bind(case, replace(caller, **change))
    with pytest.raises(RequestRefused):
        do_bind(case, work_item_id=item + 99999)
    with pytest.raises(RequestRefused):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, replace(owner_identity(caller), principal_id='issuer:foreign:1', subject='foreign'), request_id=request, binding_id=result['binding_id'], expected_revision=1)
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=result['binding_id'], expected_revision=1)['revision'] == 2
    with pytest.raises(RequestRefused) as exc:
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=result['binding_id'], expected_revision=1)
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
                    binding.withdraw(cur, pg.PgStore(conn, store.repo_id), owner_identity(caller), request_id=request, binding_id=identifier, expected_revision=1)
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
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request, work_item_id=item, instance_id=instance, idempotency_key="lock-test")
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


def test_lost_response_retry_conflict_and_fresh_binding(case):
    store, _, request, item, instance, caller = case
    first = do_bind(case, idempotency_key='retry')  # commit, then discard response
    assert do_bind(case, idempotency_key='retry') == first
    with pytest.raises(RequestRefused) as exc:
        do_bind(case, idempotency_key='retry', work_item_id=item + 1)
    assert exc.value.code == 'idempotency-conflict'
    with pytest.raises(RequestRefused) as exc:
        do_bind(case, replace(caller, effective_scopes=caller.effective_scopes | {'extra'}), idempotency_key='retry')
    assert exc.value.code == 'idempotency-conflict'
    with store.conn.transaction(), store.conn.cursor() as cur:
        binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=first['binding_id'], expected_revision=1)
    assert do_bind(case, idempotency_key='retry') == {
        **first, 'historical_replay': True, 'current_revision': 2, 'current_status': 'withdrawn'}
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 0
    fresh = do_bind(case, idempotency_key='fresh')
    assert fresh['binding_id'] != first['binding_id']
    assert do_bind(case, idempotency_key='retry')['current_status'] == 'withdrawn'


def test_different_keys_one_active_and_renewal(case):
    store, _, request, _, _, caller = case
    first = do_bind(case, idempotency_key='first')
    with pytest.raises(RequestRefused) as exc:
        do_bind(case, idempotency_key='second')
    assert exc.value.code == 'binding-already-active'
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 1
        binding.withdraw(cur, store, owner_identity(caller), request_id=request, binding_id=first['binding_id'], expected_revision=1)
    second = do_bind(case, idempotency_key='second')
    assert second['binding_id'] != first['binding_id']
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 1


def test_distinct_instance_is_distinct_active_identity(case):
    store, _, _, _, _, caller = case
    other = 'ins_' + 'B' * 26
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('INSERT INTO agent_instance(repo_id,workspace_id,instance_id,principal_id,registered_at) VALUES (%s,%s,%s,%s,now())',
                    (store.repo_id, caller.workspace_id, other, caller.principal_id))
    first = do_bind(case, idempotency_key='first')
    second = do_bind(case, idempotency_key='second', instance_id=other)
    assert first['binding_id'] != second['binding_id']
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 2


def test_concurrent_different_keys_one_active(case):
    store, name, request, item, instance, caller = case
    barrier = threading.Barrier(2)
    outcomes, errors = [], []
    def run(key):
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    barrier.wait(10)
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request,
                                 work_item_id=item, instance_id=instance, idempotency_key=key)
            outcomes.append('committed')
        except RequestRefused as exc:
            outcomes.append(exc.code)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(str(i),)) for i in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert all(not thread.is_alive() for thread in threads) and not errors
    assert sorted(outcomes) == ['binding-already-active', 'committed']
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding WHERE withdrawn_at IS NULL')
        assert cur.fetchone()['n'] == 1


def test_concurrent_exact_retries(case):
    store, name, request, item, instance, caller = case
    barrier = threading.Barrier(2)
    results, errors = [], []
    def run():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    barrier.wait(10)
                    result = binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request, work_item_id=item, instance_id=instance, idempotency_key='concurrent')
                conn.commit()
                results.append(result)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(15)
    assert all(not t.is_alive() for t in threads) and not errors
    assert len(results) == 2 and results[0] == results[1]


def test_collision_preserves_foreign_relation(case):
    store = case[0]
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('DROP TABLE operator_request_recipient_binding')
        cur.execute('CREATE TABLE operator_request_recipient_binding (foreign_value integer)')
        cur.execute('INSERT INTO operator_request_recipient_binding VALUES (42)')
        cur.execute('UPDATE schema_version SET version=31')
    for _ in range(2):
        with pytest.raises(pg_migrations.RemoteSchemaMigrationError):
            pg_migrations.migrate_schema(store)
        with store.conn.cursor() as cur:
            cur.execute('SELECT foreign_value FROM operator_request_recipient_binding')
            assert cur.fetchone()['foreign_value'] == 42
            cur.execute('SELECT version FROM schema_version')
            assert cur.fetchone()['version'] == 31
        store.conn.rollback()


def test_handshake_rejects_missing_and_partial_binding_storage(case):
    store = case[0]
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('DROP TABLE operator_request_recipient_binding')
    assert pg_migrations.compatibility_handshake(store)['reason'] == 'recipient-binding-storage-missing'
    store.conn.rollback()
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('CREATE TABLE operator_request_recipient_binding (foreign_value integer)')
    assert pg_migrations.compatibility_handshake(store)['reason'] == 'recipient-binding-storage-partial'
    store.conn.rollback()


def test_runtime_role_can_bind_and_withdraw_without_ddl(case):
    import os
    url = os.environ.get('SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL')
    if not url:
        pytest.skip('requires disposable runtime role')
    store, name, request, item, instance, caller = case
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute(f'GRANT USAGE ON SCHEMA "{name}" TO sprintctl_production_probe')
        cur.execute('GRANT SELECT, UPDATE ON operator_request TO sprintctl_production_probe')
        cur.execute('GRANT SELECT ON operator_request_dependant TO sprintctl_production_probe')
        cur.execute('GRANT SELECT, UPDATE ON agent_instance TO sprintctl_production_probe')
        cur.execute('GRANT SELECT, INSERT, UPDATE ON operator_request_recipient_binding TO sprintctl_production_probe')
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{name}"')
            runtime = pg.PgStore(conn, store.repo_id)
            result = binding.bind(cur, runtime, caller, request_id=request, work_item_id=item, instance_id=instance, idempotency_key='runtime')
            binding.withdraw(cur, runtime, owner_identity(caller), request_id=request, binding_id=result['binding_id'], expected_revision=1)
        conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute('CREATE TABLE unauthorized_ddl (id integer)')
        conn.rollback()


@pytest.mark.parametrize('mutation', ['withdraw', 'progress'])
def test_bind_serializes_with_request_mutations(case, mutation):
    store, name, request, item, instance, caller = case
    locked, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def bind_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    binding.bind(cur, pg.PgStore(conn, store.repo_id), caller, request_id=request,
                                 work_item_id=item, instance_id=instance, idempotency_key='mutation')
                    locked.set()
                    assert release.wait(10)
        except Exception as exc:
            errors.append(exc)
    def mutate_worker():
        try:
            with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{name}"')
                    args = {'request_id': request, 'work_item_id': item, 'session_id': 'session-a', 'expected_revision': 1}
                    if mutation == 'withdraw':
                        requests.withdraw_dependant(cur, pg.PgStore(conn, store.repo_id), 'principal-a', 'workspace-a', args)
                    else:
                        args.update(step_id='check', status='completed', note='checked')
                        requests.progress(cur, pg.PgStore(conn, store.repo_id), 'principal-a', 'workspace-a', args)
            finished.set()
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=bind_worker); first.start()
    assert locked.wait(10)
    second = threading.Thread(target=mutate_worker); second.start()
    try:
        assert not finished.wait(.2)
    finally:
        release.set(); first.join(10); second.join(10)
    assert not first.is_alive() and not second.is_alive() and not errors and finished.is_set()
    if mutation == 'withdraw':
        with pytest.raises(RequestRefused) as exc:
            do_bind(case)
        assert exc.value.code == 'dependant-not-active'


def test_inconsistent_provenance_never_reaches_consent_storage(case):
    store, _, _, _, _, caller = case
    with pytest.raises(ValueError, match="principal_id must match"):
        do_bind(case, replace(caller, subject='different-subject'))
    with store.conn.transaction(), store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_request_recipient_binding')
        assert cur.fetchone()['n'] == 0
    assert do_bind(case)['binding_id']


def test_withdraw_after_scope_loss_and_identity_substitution(case):
    store, _, request, item, instance, caller = case
    result = do_bind(case)
    owner = owner_identity(caller)  # Current authentication carries no consent scopes.
    for substitute in (
        replace(owner, workspace_id='elsewhere'),
        replace(owner, principal_id='issuer:foreign:1', subject='foreign'),
        replace(owner, principal_id='other:subject:1', gateway_issuer='other'),
        replace(owner, principal_id='issuer:subject:2', epoch=2),
    ):
        with pytest.raises(RequestRefused) as exc:
            with store.conn.transaction(), store.conn.cursor() as cur:
                binding.withdraw(cur, store, substitute, request_id=request,
                                 binding_id=result['binding_id'], expected_revision=1)
        assert exc.value.code == 'not-found'
    with pytest.raises(TypeError, match='authenticated owner identity'):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.withdraw(cur, store, caller, request_id=request,
                             binding_id=result['binding_id'], expected_revision=1)
    with pytest.raises(TypeError, match='verified recipient provenance'):
        with store.conn.transaction(), store.conn.cursor() as cur:
            binding.bind(cur, store, owner, request_id=request, work_item_id=item,
                         instance_id=instance, idempotency_key='scope-loss')
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert binding.withdraw(cur, store, owner, request_id=request,
                                binding_id=result['binding_id'], expected_revision=1)['revision'] == 2
        cur.execute('SELECT revision, withdrawn_at FROM operator_request_recipient_binding WHERE binding_id=%s',
                    (result['binding_id'],))
        row = cur.fetchone()
        assert row['revision'] == 2 and row['withdrawn_at'] is not None
