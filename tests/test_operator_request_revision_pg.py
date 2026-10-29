"""Changed procedures invalidate performed reports in the real request owner."""
from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from sprintctl import operator_request as contract
from sprintctl import operator_request_pg as storage
from sprintctl.pg import PgStore
from sprintctl import pg
from sprintctl.application import WorkApplication
from sprintctl.workitemcore import item_status_revision
from tests.test_operator_request_pg import (  # noqa: F401
    _packet, _publish, _read, _request_context, pytestmark, request_owner,
)


def request_id():
    return 'req_' + uuid4().hex[:26].upper()


def _served_snapshot(store, identity, operation):
    arguments = ({'limit': 100} if operation == contract.OPERATION_LIST
                 else {'request_id': identity})
    value = WorkApplication.postgres(store).invoke(
        operation, arguments, _request_context(store))
    if operation == contract.OPERATION_LIST:
        return next(row for row in value['requests'] if row['request_id'] == identity)
    return value


CONTINUATION = 'work.request.continuation-v1'


def _continuation_request(store, seed_id, *, fresh=True, sessions=('session-a', 'session-b')):
    seed = pg.get_work_item(store, seed_id)
    item_id = pg.create_work_item(store, seed['sprint_id'], seed['track_id'], 'Continuation source')
    identity = request_id()
    packet = _packet()
    if fresh:
        packet['blocker']['fresh_until'] = '2099-10-24T11:00:00Z'
    args = contract.publish_arguments({
        'request_id': identity, 'packet': packet,
        'dependants': [{'work_item_id': item_id, 'session_id': session} for session in sessions],
        'idempotency_key': str(uuid4()),
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        storage.publish(cur, store, 'principal-a', 'workspace-a', args)
    return identity, item_id


def _resolve_continuation(store, identity, revision=1):
    report(store, identity, revision=revision)
    with store.conn.transaction(), store.conn.cursor() as cur:
        storage.attest(cur, store, 'principal-reviewer', 'workspace-a',
                       attestation_arguments(identity, revision + 1))


def _continuation(store, identity, **context):
    return WorkApplication.postgres(store).invoke(
        CONTINUATION, {'request_id': identity}, _request_context(store, **context))


def test_continuation_read_contains_one_resolution_for_all_current_dependants(request_owner):
    store, seed_id = request_owner
    identity, item_id = _continuation_request(store, seed_id)
    _resolve_continuation(store, identity)
    history = events(store, identity)
    value = _continuation(store, identity, authorities=frozenset({'work:read'}))
    assert value == {
        'schema': 'operator-request-continuation-ready/v1',
        'owner': {'repo_id': store.repo_id, 'read_operation': CONTINUATION},
        'request_id': identity, 'request_revision': 3,
        'resolution': {'event_revision': 3, 'basis': 'human-attestation',
                       'event_recorded_at': history[-1]['recorded_at']},
        'dependants': [
            {'work_item_id': item_id, 'session_id': session,
             'work_revision': value['dependants'][0]['work_revision'],
             'current_work_revision': value['dependants'][0]['work_revision'],
             'state': 'continuation-ready'} for session in ('session-a', 'session-b')
        ],
    }
    assert events(store, identity) == history  # Reading never acknowledges delivery.


@pytest.mark.parametrize('outcome', [None, 'performed', 'partial', 'uncertain'])
def test_continuation_read_refuses_unresolved_and_unverified_reports(request_owner, outcome):
    from sprintctl.application import ApplicationRejection
    store, seed_id = request_owner
    identity, _ = _continuation_request(store, seed_id)
    if outcome:
        report(store, identity, outcome=outcome)
    with pytest.raises(ApplicationRejection) as caught:
        _continuation(store, identity)
    assert caught.value.code == 'continuation-not-ready'


@pytest.mark.parametrize('change', ['expired', 'changed-work', 'withdrawn-dependant'])
def test_continuation_read_preserves_freshness_and_each_dependant_need(request_owner, change):
    store, seed_id = request_owner
    identity, item_id = _continuation_request(store, seed_id, fresh=change != 'expired')
    revision = 1
    if change == 'withdrawn-dependant':
        with store.conn.transaction(), store.conn.cursor() as cur:
            storage.withdraw_dependant(cur, store, 'principal-a', 'workspace-a', {
                'request_id': identity, 'expected_revision': 1,
                'work_item_id': item_id, 'session_id': 'session-a',
            })
        revision += 1
    _resolve_continuation(store, identity, revision)
    if change == 'changed-work':
        item = pg.get_work_item(store, item_id)
        pg.set_work_item_status(store, item_id, 'active', 'continuation-successor',
                                expected_revision=item_status_revision(item))
    value = _continuation(store, identity)
    assert len(value['dependants']) == (1 if change == 'withdrawn-dependant' else 2)
    assert {row['state'] for row in value['dependants']} == {
        'continuation-ready' if change == 'withdrawn-dependant' else 'needs-refresh'}
    if change == 'withdrawn-dependant':
        assert value['dependants'][0]['session_id'] == 'session-b'


def test_continuation_read_never_leaks_another_workspace_request(request_owner):
    from sprintctl.application import ApplicationRejection
    store, seed_id = request_owner
    identity, _ = _continuation_request(store, seed_id)
    _resolve_continuation(store, identity)
    with pytest.raises(ApplicationRejection) as caught:
        _continuation(store, identity, workspace_id='workspace-b')
    assert caught.value.code == 'not-found'


@pytest.mark.parametrize('change', ['unsupported-basis', 'claimed-effect-authority'])
def test_continuation_read_requires_the_exact_immutable_resolution_basis(request_owner, monkeypatch, change):
    from sprintctl.application import ApplicationRejection
    store, seed_id = request_owner
    identity, _ = _continuation_request(store, seed_id)
    original = storage._record_event

    def malformed_resolution(cur, owner, workspace, selected, revision, actor, action, details, at):
        if action == 'resolved-human-attestation':
            details = dict(details)
            if change == 'unsupported-basis':
                action = 'claimed-source-completion'
                details['completion_basis'] = 'caller-verified'
            else:
                details['protected_effect_authorized'] = True
        return original(cur, owner, workspace, selected, revision, actor, action, details, at)

    monkeypatch.setattr(storage, '_record_event', malformed_resolution)
    _resolve_continuation(store, identity)
    with pytest.raises(ApplicationRejection) as caught:
        _continuation(store, identity)
    assert caught.value.code == 'resolution-evidence-unavailable'


def test_continuation_resolution_and_work_basis_share_one_owner_snapshot(request_owner, monkeypatch):
    store, seed_id = request_owner
    identity, item_id = _continuation_request(store, seed_id)
    _resolve_continuation(store, identity)
    original = storage._snapshot
    committed = []

    def change_work_after_request(cur, reader, workspace, row, now):
        if row['request_id'] == identity and not committed:
            with store.connection_factory() as conn:
                writer = PgStore(conn, store.repo_id, store.authority_repo_uuid)
                item = pg.get_work_item(writer, item_id)
                pg.set_work_item_status(writer, item_id, 'active', 'continuation-writer',
                                        expected_revision=item_status_revision(item))
                committed.append(True)
        return original(cur, reader, workspace, row, now)

    monkeypatch.setattr(storage, '_snapshot', change_work_after_request)
    first = _continuation(store, identity)
    assert committed == [True]
    assert {row['state'] for row in first['dependants']} == {'continuation-ready'}
    assert first['resolution']['event_revision'] == first['request_revision'] == 3
    second = _continuation(store, identity)
    assert {row['state'] for row in second['dependants']} == {'needs-refresh'}


@pytest.mark.parametrize('operation', [contract.OPERATION_GET, contract.OPERATION_LIST])
def test_request_read_excludes_withdrawal_committed_between_constituents(request_owner, monkeypatch, operation):
    """The request header and dependant state must describe one owner snapshot."""
    store, item_id = request_owner
    identity = request_id()
    _publish(store, item_id, identity)
    original = storage._dependants
    committed = []

    def withdraw_after_header(cur, reader, workspace, selected):
        if selected == identity and not committed:
            with store.connection_factory() as conn:
                writer = PgStore(conn, store.repo_id, store.authority_repo_uuid)
                with conn.cursor() as writer_cur:
                    result = storage.withdraw_dependant(writer_cur, writer, 'principal-a', 'workspace-a', {
                        'request_id': identity, 'expected_revision': 1,
                        'work_item_id': item_id, 'session_id': 'session-a',
                    })
                conn.commit()
                committed.append(result['revision'])
        return original(cur, reader, workspace, selected)

    monkeypatch.setattr(storage, '_dependants', withdraw_after_header)
    before = _served_snapshot(store, identity, operation)
    assert committed == [2]
    assert (before['revision'], before['state'], before['dependants'][0]['state']) == (1, 'open', 'active')
    after = _served_snapshot(store, identity, operation)
    assert (after['revision'], after['state'], after['dependants'][0]['state']) == (2, 'withdrawn', 'withdrawn')


@pytest.mark.parametrize('operation', [contract.OPERATION_GET, contract.OPERATION_LIST])
def test_request_read_excludes_work_change_after_request_snapshot(request_owner, monkeypatch, operation):
    """Source-changed flags cannot incorporate a later work transaction."""
    store, seed_id = request_owner
    seed = pg.get_work_item(store, seed_id)
    item_id = pg.create_work_item(store, seed['sprint_id'], seed['track_id'], 'Snapshot work change')
    identity = request_id()
    _publish(store, item_id, identity)
    original = storage._work_item
    committed = []

    def change_after_header(cur, reader, selected, **kwargs):
        if selected == item_id and not committed:
            with store.connection_factory() as conn:
                writer = PgStore(conn, store.repo_id, store.authority_repo_uuid)
                with conn.cursor() as writer_cur:
                    old = original(writer_cur, writer, item_id)
                pg.set_work_item_status(writer, item_id, 'active', 'snapshot-writer',
                                        expected_revision=item_status_revision(old))
                committed.append(True)
        return original(cur, reader, selected, **kwargs)

    monkeypatch.setattr(storage, '_work_item', change_after_header)
    before = _served_snapshot(store, identity, operation)
    assert committed == [True]
    dependant = before['dependants'][0]
    assert dependant['source_changed'] is False
    assert dependant['current_work_revision'] == dependant['work_revision']
    after = _served_snapshot(store, identity, operation)
    assert after['dependants'][0]['source_changed'] is True


@pytest.mark.parametrize('operation,missing', [
    (contract.OPERATION_GET, False), (contract.OPERATION_LIST, False),
    (contract.OPERATION_EVENTS, False), (contract.OPERATION_GET, True),
    (contract.OPERATION_EVENTS, True),
])
def test_request_read_owns_only_a_closed_readonly_sibling(request_owner, monkeypatch, operation, missing):
    from psycopg.pq import TransactionStatus
    from sprintctl.application import ApplicationRejection

    store, item_id = request_owner
    identity = request_id()
    if not missing:
        _publish(store, item_id, identity)
    factory = store.connection_factory
    opened = []

    def factory_spy():
        connection = factory()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, 'connection_factory', factory_spy)
    observed = []
    original = storage._request

    def observe_read(cur, reader, *args, **kwargs):
        cur.execute('SHOW transaction_isolation')
        isolation = next(iter(cur.fetchone().values()))
        cur.execute('SHOW transaction_read_only')
        readonly = next(iter(cur.fetchone().values()))
        observed.append((isolation, readonly, reader.conn is store.conn))
        return original(cur, reader, *args, **kwargs)

    # list starts with its own SELECT before it calls _snapshot/_dependants.
    monkeypatch.setattr(storage, '_request', observe_read)
    original_dependants = storage._dependants
    def observe_dependants(cur, reader, workspace, selected):
        observe_read(cur, reader, workspace, selected)
        return original_dependants(cur, reader, workspace, selected)
    if operation == contract.OPERATION_LIST:
        monkeypatch.setattr(storage, '_dependants', observe_dependants)

    xid = store.conn.execute('SELECT pg_current_xact_id() AS xid').fetchone()['xid']
    assert store.conn.info.transaction_status == TransactionStatus.INTRANS
    args = ({'limit': 100} if operation == contract.OPERATION_LIST else {'request_id': identity})
    if operation == contract.OPERATION_EVENTS:
        args.update(after_revision=0, limit=100)
    app = WorkApplication.postgres(store)
    if missing:
        with pytest.raises(ApplicationRejection) as caught:
            app.invoke(operation, args, _request_context(store))
        assert caught.value.code == 'not-found'
    else:
        app.invoke(operation, args, _request_context(store))
    assert observed and all(row == ('repeatable read', 'on', False) for row in observed)
    assert len(opened) == 1 and opened[0].closed
    assert store.conn.info.transaction_status == TransactionStatus.INTRANS
    assert store.conn.execute('SELECT pg_current_xact_id() AS xid').fetchone()['xid'] == xid
    store.conn.rollback()


def report(store, identity, outcome='performed', revision=1):
    args = contract.report_performed_arguments({
        'request_id': identity, 'expected_revision': revision, 'outcome': outcome,
        'note': 'Synthetic procedure report.', 'idempotency_key': str(uuid4()),
    })
    with store.conn.transaction(), store.conn.cursor() as cur:
        return storage.report_performed(cur, store, 'principal-a', 'workspace-a', args)


def revision_arguments(identity, revision=2):
    packet = _packet('The target changed after the first report.')
    packet['execution']['target'] = 'staging/service-b'
    packet['procedure']['version'] = '2.0.0'
    return contract.revise_arguments({
        'request_id': identity, 'expected_revision': revision, 'packet': packet,
        'idempotency_key': str(uuid4()),
    })


def attestation_arguments(identity, revision):
    return contract.attest_arguments({
        'request_id': identity, 'expected_revision': revision,
        'statement': 'The operator attests to the current procedure.',
        'performed_at': '2026-10-29T10:00:00Z', 'idempotency_key': str(uuid4()),
    })


def events(store, identity):
    with store.conn.cursor() as cur:
        rows = storage.events(cur, store, 'workspace-a', {
            'request_id': identity, 'after_revision': 0, 'limit': 100,
        })['events']
    store.conn.commit()
    return rows


@pytest.mark.parametrize('outcome', ['performed', 'partial', 'uncertain'])
def test_material_change_requires_a_new_report_before_attestation(request_owner, outcome):
    store, item_id = request_owner
    identity = request_id()
    _publish(store, item_id, identity, sessions=('session-a', 'session-b'))
    report(store, identity, outcome)
    before = _read(store, identity)
    old_history = events(store, identity)
    args = revision_arguments(identity)
    with store.conn.transaction(), store.conn.cursor() as cur:
        revised = storage.revise(cur, store, 'principal-a', 'workspace-a', args)
    assert revised == {'request_id': identity, 'revision': 3, 'replayed': False}
    snapshot = _read(store, identity)
    assert snapshot['state'] == 'open'
    assert snapshot['packet'] == args['packet']
    assert snapshot['dependants'] == before['dependants']
    assert events(store, identity)[:2] == old_history
    with store.conn.cursor() as cur:
        cur.execute('SELECT material_revision FROM operator_request WHERE repo_id=%s AND request_id=%s',
                    (store.repo_id, identity))
        assert cur.fetchone()['material_revision'] == 2
    store.conn.commit()
    for revision, code in [(2, 'revision-conflict'), (3, 'request-not-verifiable')]:
        with pytest.raises(contract.RequestRefused) as caught, store.conn.transaction(), store.conn.cursor() as cur:
            storage.attest(cur, store, 'principal-reviewer', 'workspace-a',
                           attestation_arguments(identity, revision))
        assert caught.value.code == code
    assert _read(store, identity) == snapshot
    report(store, identity, revision=3)
    with store.conn.transaction(), store.conn.cursor() as cur:
        assert storage.attest(cur, store, 'principal-reviewer', 'workspace-a',
                              attestation_arguments(identity, 4))['revision'] == 5
    resolved = _read(store, identity)
    assert resolved['state'] == 'resolved' and resolved['packet'] == args['packet']
    assert [row['action'] for row in events(store, identity)] == [
        'published', 'performed-reported', 'revised', 'performed-reported', 'resolved-human-attestation',
    ]
    with pytest.raises(contract.RequestRefused) as caught, store.conn.transaction(), store.conn.cursor() as cur:
        storage.revise(cur, store, 'principal-a', 'workspace-a', revision_arguments(identity, 5))
    assert caught.value.code == 'request-not-open'
    assert _read(store, identity) == resolved


def test_reported_revision_retains_requester_and_workspace_binding(request_owner):
    store, item_id = request_owner
    identity = request_id()
    _publish(store, item_id, identity)
    report(store, identity)
    before = _read(store, identity)
    for principal, workspace, code in [
        ('principal-b', 'workspace-a', 'requester-not-owner'),
        ('principal-a', 'workspace-b', 'not-found'),
    ]:
        with pytest.raises(contract.RequestRefused) as caught, store.conn.transaction(), store.conn.cursor() as cur:
            storage.revise(cur, store, principal, workspace, revision_arguments(identity))
        assert caught.value.code == code
    assert _read(store, identity) == before


def test_served_revision_retry_replays_without_reopening_a_later_resolution(request_owner):
    store, item_id = request_owner
    identity = request_id()
    _publish(store, item_id, identity)
    report(store, identity)
    app = WorkApplication.postgres(store)
    args = revision_arguments(identity)
    context = _request_context(store, request_id='material-revision-' + uuid4().hex)
    revised = app.invoke(contract.OPERATION_REVISE, args, context)
    assert revised == {'request_id': identity, 'revision': 3, 'replayed': False}
    report(store, identity, revision=3)
    with store.conn.transaction(), store.conn.cursor() as cur:
        storage.attest(cur, store, 'principal-reviewer', 'workspace-a', attestation_arguments(identity, 4))
    resolved, history = _read(store, identity), events(store, identity)
    assert app.invoke(contract.OPERATION_REVISE, args, context) == revised | {'replayed': True}
    assert _read(store, identity) == resolved
    assert events(store, identity) == history


def test_material_change_event_failure_rolls_back_packet_state_and_revision(request_owner, monkeypatch):
    store, item_id = request_owner
    identity = request_id()
    _publish(store, item_id, identity)
    report(store, identity)
    before, history = _read(store, identity), events(store, identity)

    def fail_event(*args, **kwargs):
        raise RuntimeError('synthetic event write failure')

    monkeypatch.setattr(storage, '_record_event', fail_event)
    with pytest.raises(RuntimeError, match='synthetic event write failure'), store.conn.transaction(), store.conn.cursor() as cur:
        storage.revise(cur, store, 'principal-a', 'workspace-a', revision_arguments(identity))
    assert _read(store, identity) == before
    assert events(store, identity) == history
    with store.conn.cursor() as cur:
        cur.execute('SELECT material_revision FROM operator_request WHERE repo_id=%s AND request_id=%s',
                    (store.repo_id, identity))
        assert cur.fetchone()['material_revision'] == 1
    store.conn.commit()


@pytest.mark.parametrize('attempt', range(3))
def test_material_revision_competes_with_attestation_on_independent_connections(request_owner, attempt):
    store, item_id = request_owner
    identity = request_id()
    _publish(store, item_id, identity)
    report(store, identity)
    barrier = threading.Barrier(2)
    outcomes = []

    def contender(action):
        conn = store.connection_factory()
        actor = PgStore(conn, store.repo_id, store.authority_repo_uuid)
        try:
            barrier.wait(timeout=5)
            with conn.transaction(), conn.cursor() as cur:
                value = (storage.revise(cur, actor, 'principal-a', 'workspace-a', revision_arguments(identity))
                         if action == 'revise' else storage.attest(cur, actor, 'principal-reviewer', 'workspace-a',
                                                                  attestation_arguments(identity, 2)))
            outcomes.append((action, 'accepted', value['revision']))
        except contract.RequestRefused as error:
            outcomes.append((action, error.code, None))
        except Exception as error:
            outcomes.append((action, 'unexpected', type(error).__name__))
        finally:
            conn.close()

    threads = [threading.Thread(target=contender, args=(action,)) for action in ['revise', 'attest']]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(row[1] for row in outcomes) == ['accepted', 'revision-conflict'], outcomes
    winner = next(row[0] for row in outcomes if row[1] == 'accepted')
    snapshot = _read(store, identity)
    assert snapshot['revision'] == 3
    assert snapshot['state'] == ('open' if winner == 'revise' else 'resolved')
    assert events(store, identity)[-1]['action'] == ('revised' if winner == 'revise' else 'resolved-human-attestation')
