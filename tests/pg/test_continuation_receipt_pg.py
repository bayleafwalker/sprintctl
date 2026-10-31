"""Principal receipt boundaries on a disposable PostgreSQL owner."""
from uuid import uuid4
import threading

import pytest

from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl import operator_request as contract, operator_request_pg as storage, pg
from sprintctl.pg import PgStore
from sprintctl.workitemcore import item_status_revision
from tests.test_operator_request_pg import _request_context, pytestmark, request_owner  # noqa: F401
from tests.test_operator_request_revision_pg import _continuation_request, _resolve_continuation, _continuation


def setup_case(store, seed, *, fresh=True):
    identity, item = _continuation_request(store, seed, fresh=fresh)
    _resolve_continuation(store, identity)
    work = pg.get_work_item(store, item)
    return {'request_id': identity, 'resolution_event_revision': 3, 'work_item_id': item,
            'expected_work_revision': item_status_revision(work), 'idempotency_key': str(uuid4())}


def invoke(store, args, *, principal='principal-a', workspace='workspace-a', authorities=frozenset({'work:write'})):
    ctx = _request_context(store, authorities=authorities, workspace_id=workspace)
    ctx.identity.principal_id = principal
    return WorkApplication.postgres(store).invoke(contract.OPERATION_CONTINUATION_RECEIPT, args, ctx)


def test_receipt_retries_and_v1_unchanged(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    before = _continuation(store, args['request_id'])
    receipt = invoke(store, args)
    assert receipt['sessions'] == ['session-a', 'session-b']
    assert receipt['replayed'] is False
    replay = invoke(store, args)
    assert replay == receipt | {'replayed': True}
    second = invoke(store, args | {'idempotency_key': str(uuid4())})
    assert second == replay
    assert _continuation(store, args['request_id']) == before
    assert pg.get_work_item(store, args['work_item_id'])['status'] == 'pending'
    with store.conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM operator_continuation_receipt WHERE repo_id=%s AND request_id=%s', (store.repo_id, args['request_id']))
        assert cur.fetchone()['n'] == 1
    store.conn.commit()


@pytest.mark.parametrize('fault,code', [('principal','requester-not-owner'), ('workspace','not-found'),
    ('authority','authority-required'), ('resolution','continuation-not-ready'),
    ('work','dependant-not-active'), ('revision','continuation-needs-refresh'), ('expired','continuation-needs-refresh')])
def test_receipt_refuses_foreign_stale_or_unauthorized(request_owner, fault, code):
    store, seed = request_owner
    args = setup_case(store, seed, fresh=fault != 'expired')
    kw = {}
    if fault == 'principal': kw['principal'] = 'principal-b'
    if fault == 'workspace': kw['workspace'] = 'workspace-b'
    if fault == 'authority': kw['authorities'] = frozenset({'work:read'})
    if fault == 'resolution': args['resolution_event_revision'] = 1
    if fault == 'work': args['work_item_id'] = seed
    if fault == 'revision': args['expected_work_revision'] = 'stale'
    with pytest.raises(ApplicationRejection) as caught:
        invoke(store, args, **kw)
    assert caught.value.code == code


def test_caller_cannot_choose_session(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed) | {'session_id': 'foreign-session'}
    with pytest.raises(ApplicationRejection) as caught:
        invoke(store, args)
    assert caught.value.code == 'invalid-arguments'


def test_work_change_waits_for_receipt_commit(request_owner, monkeypatch):
    store, seed = request_owner
    args = setup_case(store, seed)
    reached, release, update_done = threading.Event(), threading.Event(), threading.Event()
    # The receipt obtains the work lock after _now; pause on its INSERT instead.
    class ProxyCursor:
        def __init__(self, cur): self.cur = cur
        def execute(self, query, params=None):
            result = self.cur.execute(query, params)
            if query.startswith('SELECT id,aggregate_uuid,status FROM work_item'):
                reached.set()
                assert release.wait(10)
            return result
        def __getattr__(self, name): return getattr(self.cur, name)
    errors = []
    def receipt():
        try:
            with store.connection_factory() as conn:
                sibling = PgStore(conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid)
                with conn.transaction(), conn.cursor() as cur:
                    storage.continuation_receipt(ProxyCursor(cur), sibling, 'principal-a', 'workspace-a', args)
        except Exception as exc: errors.append(exc)
    def update():
        try:
            with store.connection_factory() as conn:
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute("UPDATE work_item SET status='active' WHERE repo_id=%s AND id=%s", (store.repo_id, args['work_item_id']))
            update_done.set()
        except Exception as exc: errors.append(exc)
    worker = threading.Thread(target=receipt); worker.start()
    assert reached.wait(10)
    writer = threading.Thread(target=update); writer.start()
    assert not update_done.wait(.2)
    release.set(); worker.join(10); writer.join(10)
    assert not errors and not worker.is_alive() and not writer.is_alive() and update_done.is_set()
    with pytest.raises(ApplicationRejection) as caught:
        invoke(store, args | {'idempotency_key': str(uuid4())})
    assert caught.value.code == 'continuation-needs-refresh'


def test_schema30_exact_shape_gate_refuses_missing_guard(request_owner):
    from sprintctl import pg_migrations
    store, _ = request_owner
    with store.conn.cursor() as cur:
        assert pg_migrations.compatibility_handshake(store)['compatible'] is True
        cur.execute('ALTER TABLE operator_continuation_receipt DISABLE TRIGGER sprintctl_continuation_receipt_append_only')
        check = pg_migrations.compatibility_handshake(store)
        assert check['compatible'] is False
        assert check['reason'] == 'continuation-receipt-storage-partial'
    store.conn.rollback()
    assert pg_migrations.compatibility_handshake(store)['compatible'] is True


def test_schema28_refuses_receipt_handler(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    version = store.remote_schema_version
    try:
        store.remote_schema_version = 28
        with pytest.raises(ApplicationRejection) as caught:
            invoke(store, args)
        assert caught.value.code == 'owner-capability-unavailable'
    finally:
        store.remote_schema_version = version


@pytest.mark.parametrize('command', ['DELETE', 'UPDATE', 'TRUNCATE'])
def test_receipt_append_only(request_owner, command):
    import psycopg
    store, seed = request_owner
    args = setup_case(store, seed)
    invoke(store, args)
    with pytest.raises(psycopg.errors.CheckViolation):
        with store.conn.transaction(), store.conn.cursor() as cur:
            if command == 'TRUNCATE':
                cur.execute('TRUNCATE operator_continuation_receipt')
            else:
                sql = ('DELETE FROM operator_continuation_receipt' if command == 'DELETE' else
                       'UPDATE operator_continuation_receipt SET principal_id=principal_id')
                cur.execute(sql + ' WHERE repo_id=%s AND request_id=%s',
                            (store.repo_id, args['request_id']))
    store.conn.rollback()


def test_same_receipt_concurrent_different_keys(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    barrier = threading.Barrier(2)
    results, errors = [], []
    def participant():
        try:
            with store.connection_factory() as conn:
                sibling = PgStore(conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid)
                barrier.wait(10)
                sibling.remote_schema_version = 30
                results.append(invoke(sibling, args | {'idempotency_key': str(uuid4())}))
        except Exception as exc: errors.append(exc)
    first = threading.Thread(target=participant); second = threading.Thread(target=participant)
    first.start(); second.start(); first.join(10); second.join(10)
    assert not errors and not first.is_alive() and not second.is_alive()
    assert sorted(r['replayed'] for r in results) == [False, True]


def test_withdrawn_dependant_is_not_selected(request_owner):
    store, seed = request_owner
    identity, item = _continuation_request(store, seed)
    with store.conn.transaction(), store.conn.cursor() as cur:
        storage.withdraw_dependant(cur, store, 'principal-a', 'workspace-a', {
            'request_id': identity, 'expected_revision': 1, 'work_item_id': item, 'session_id': 'session-a'})
    _resolve_continuation(store, identity, revision=2)
    args = {'request_id': identity, 'resolution_event_revision': 4, 'work_item_id': item,
            'expected_work_revision': item_status_revision(pg.get_work_item(store, item)), 'idempotency_key': str(uuid4())}
    assert invoke(store, args)['sessions'] == ['session-b']


def test_lost_response_replay_is_historical_after_work_changes(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    first = invoke(store, args)
    pg.set_work_item_status(store, args['work_item_id'], 'active', 'successor',
                            expected_revision=args['expected_work_revision'])
    assert invoke(store, args) == first | {'replayed': True}
    with pytest.raises(ApplicationRejection) as caught:
        invoke(store, args | {'idempotency_key': str(uuid4())})
    assert caught.value.code == 'continuation-needs-refresh'


def test_status_aba_and_description_edits_are_not_detected_by_status_token(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    item = args['work_item_id']
    pending = args['expected_work_revision']
    pg.set_work_item_status(store, item, 'active', 'successor', expected_revision=pending)
    active = item_status_revision(pg.get_work_item(store, item))
    pg.set_work_item_status(store, item, 'pending', 'successor', reason='rework', expected_revision=active)
    pg.update_work_item_description(store, item, 'Changed scope after request', actor='successor')
    assert item_status_revision(pg.get_work_item(store, item)) == pending
    receipt = invoke(store, args)
    assert receipt['work_revision'] == pending


def test_exact_replay_still_requires_live_authority_and_owner_binding(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    invoke(store, args)
    for kw, code in (({'authorities': frozenset({'work:read'})}, 'authority-required'),
                     ({'principal': 'principal-b'}, 'requester-not-owner'),
                     ({'workspace': 'workspace-b'}, 'not-found')):
        with pytest.raises(ApplicationRejection) as caught:
            invoke(store, args, **kw)
        assert caught.value.code == code


def test_same_idempotency_key_with_changed_arguments_refuses(request_owner):
    store, seed = request_owner
    args = setup_case(store, seed)
    invoke(store, args)
    with pytest.raises(ApplicationRejection) as caught:
        invoke(store, args | {'expected_work_revision': 'different'})
    assert caught.value.code == 'idempotency-conflict'
