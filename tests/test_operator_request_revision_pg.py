"""Changed procedures invalidate performed reports in the real request owner."""
from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from sprintctl import operator_request as contract
from sprintctl import operator_request_pg as storage
from sprintctl.pg import PgStore
from sprintctl.application import WorkApplication
from tests.test_operator_request_pg import (  # noqa: F401
    _packet, _publish, _read, _request_context, pytestmark, request_owner,
)


def request_id():
    return 'req_' + uuid4().hex[:26].upper()


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
