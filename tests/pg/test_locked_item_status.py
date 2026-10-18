from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sprintctl import db
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from tests._locked_item_status_contract import LockedStatusCases, StatusDriver

pytestmark = PG_MARKS


@pytest.fixture
def status_driver(store):
    sid = pg.create_sprint(store, 'locked status PG '+uuid4().hex, status='active')
    tid = pg.get_or_create_track(store, sid, 'status')
    assert store.conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'
    store.conn.commit()  # Fixture-only read preflight, before caller work.

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
            assert_disposable_connection(conn)
            assert conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'
            conn.commit()  # Only this fresh connection's safety preflight.
            yield replace(store, conn=conn)

    return StatusDriver(pg, store, sid, tid, independent, pg._WorkItemPg, True)


class TestPostgresLockedStatus(LockedStatusCases):
    pass


def _consequences(store, iid):
    return {table: [dict(row) for row in store.conn.execute(
        f'SELECT * FROM {table} WHERE repo_id=%s AND work_item_id=%s ORDER BY {key}',
        (store.repo_id, iid)).fetchall()]
        for table, key in [('work_lease', 'lease_id'), ('work_outcome_report', 'report_id')]}


@pytest.mark.parametrize('awaiting', (False, True))
@pytest.mark.parametrize('fault', (False, True))
def test_done_preserves_owner_lease_report_effects_and_rollback(status_driver, awaiting, fault, monkeypatch, record_property):
    import json
    from tests.pg.test_work_lease import A, _run, _claim, TestAwaitingReportStamping
    d = status_driver
    if awaiting:
        iid, _run_id, lease, report = TestAwaitingReportStamping()._awaiting(d.store, 'locked-'+uuid4().hex)
        item = pg.get_work_item(d.store, iid)
        d = replace(d, sprint=item['sprint_id'], track=item['track_id'])
    else:
        iid = d.new_item()
        lease = _claim(d.store, A, iid, _run(d.store, A), 'locked-'+uuid4().hex)['lease']
    before, effects_before = d.snapshot(iid), _consequences(d.store, iid)
    observations = []
    original = pg._decide_locked

    def after_decision(*args, **kwargs):
        result = original(*args, **kwargs)
        pending, effects = d.snapshot(iid), _consequences(d.store, iid)
        assert pending['item']['status'] == 'done'
        assert effects['work_lease'][0]['state'] == 'settled'
        if awaiting:
            assert effects['work_outcome_report'][0]['disposition'] == 'rejected'
            assert effects['work_outcome_report'][0]['reason_code'] == 'decided-accept-unverified'
            assert effects['work_outcome_report'][0]['decision_id'] is None
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
            assert _consequences(other, iid) == effects_before
        observations.append({'pending': pending, 'effects': effects})
        raise RuntimeError('after actual done consequences')

    if fault:
        with monkeypatch.context() as patch:
            patch.setattr(pg, '_decide_locked', after_decision)
            with pytest.raises(RuntimeError, match='actual done consequences'):
                d.transition(iid, 'done', actor='operator')
        assert d.transaction() == 'IDLE'
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
            assert _consequences(other, iid) == effects_before
    assert d.transition(iid, 'done', actor='operator') is None
    with d.independent() as other:
        after, effects_after = d.snapshot(iid, other), _consequences(other, iid)
    assert after['item']['resolution'] == 'accepted'
    assert effects_after['work_lease'][0]['lease_id'] == lease['lease_id']
    assert effects_after['work_lease'][0]['state'] == 'settled'
    assert effects_after['work_lease'][0]['end_reason'] == 'item-accepted'
    if awaiting:
        [stamped] = effects_after['work_outcome_report']
        assert stamped['report_id'] == report['report_id']
        assert stamped['disposition'] == 'rejected' and stamped['reason_code'] == 'decided-accept-unverified'
        assert stamped['decision_id'] is None
    record_property('state_history', json.dumps({'history': f'done-owner-consequences-{awaiting}-{fault}',
        'before': before, 'effects_before': effects_before, 'inside_fault': observations,
        'after': after, 'effects_after': effects_after}, default=str, sort_keys=True))


@pytest.mark.parametrize('reason', ('rework', 'partial', 'abandoned'))
@pytest.mark.parametrize('fault', (False, True))
def test_pending_release_ends_actual_lease_even_without_matching_reservation(status_driver, reason, fault, monkeypatch):
    import json
    from tests.pg.test_work_lease import A, _run, _claim
    d = status_driver
    iid = d.new_item()
    lease = _claim(d.store, A, iid, _run(d.store, A), 'locked-release-'+uuid4().hex)['lease']
    before, effects_before = d.snapshot(iid), _consequences(d.store, iid)
    original = pg._insert_event
    observed = []

    def fail_after(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == 'item-released':
            effects = _consequences(d.store, iid)
            assert effects['work_lease'][0]['state'] == 'released'
            observed.append(effects)
            with d.independent() as other:
                assert _consequences(other, iid) == effects_before
                assert d.snapshot(iid, other) == before
            raise RuntimeError('after actual lease release')
        return result

    if fault:
        with monkeypatch.context() as patch:
            patch.setattr(pg, '_insert_event', fail_after)
            with pytest.raises(RuntimeError, match='actual lease release'):
                d.transition(iid, 'pending', reason=reason, session_id='unmatched')
        assert d.transaction() == 'IDLE' and len(observed) == 1
        with d.independent() as other:
            assert _consequences(other, iid) == effects_before
            assert d.snapshot(iid, other) == before
    assert d.transition(iid, 'pending', reason=reason, session_id='unmatched') is None
    with d.independent() as other:
        effects = _consequences(other, iid)
        events = pg.list_events(other, d.sprint)
    assert effects['work_lease'][0]['state'] == 'released'
    assert effects['work_lease'][0]['end_reason'] == 'item-released-'+reason
    [event] = [e for e in events if e['event_type'] == 'item-released']
    assert json.loads(event['payload'])['released_lease_ids'] == [lease['lease_id']]


@pytest.mark.parametrize('case', ('success', 'stale', 'syntax', 'event-fault'))
def test_existing_implicit_caller_transaction_boundary(status_driver, case, monkeypatch):
    d = status_driver
    iid = d.new_item('active')
    before = d.snapshot(iid)
    protected = pg.create_sprint(d.store, 'caller '+uuid4().hex, goal='original')
    d.store.conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',
                         ('pending caller', d.store.repo_id, protected))
    options, target = {}, 'blocked'
    if case == 'stale':
        options['expected_revision'] = db.item_status_revision(before['item']).replace('@status:active', '@status:blocked')
    elif case == 'syntax':
        options['expected_revision'] = 'bad'
    elif case == 'event-fault':
        target, options = 'pending', {'reason': 'rework'}
        original = pg._insert_event
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('caller event fault')
        monkeypatch.setattr(pg, '_insert_event', fail)
    if case == 'success':
        assert d.transition(iid, target, **options) is None
    else:
        error = RuntimeError if case == 'event-fault' else ValueError
        with pytest.raises(error):
            d.transition(iid, target, **options)
    assert d.transaction() == ('INTRANS' if case == 'syntax' else 'IDLE')
    primary_expected = 'pending caller' if case in ('success', 'syntax') else 'original'
    assert pg.get_sprint(d.store, protected)['goal'] == primary_expected
    with d.independent() as other:
        expected = 'pending caller' if case == 'success' else 'original'
        assert pg.get_sprint(other, protected)['goal'] == expected
        assert pg.get_work_item(other, iid)['status'] == ('blocked' if case == 'success' else 'active')
    if case == 'syntax':
        assert pg.get_sprint(d.store, protected)['goal'] == 'pending caller'
    d.store.conn.rollback()


@pytest.mark.parametrize('caller', ('managed', 'two-phase'))
def test_existing_forbidden_commit_leaves_pending_effect_until_owner_rollback(status_driver, caller, record_property):
    import json
    d = status_driver
    iid = d.new_item('active')
    before = d.snapshot(iid)
    conn = d.store.conn
    conn.rollback()  # End only the fixture's read-only snapshot transaction.
    observed = []

    def exercise():
        with pytest.raises(psycopg.ProgrammingError, match='rollback'):
            d.transition(iid, 'blocked')
        pending = d.snapshot(iid)
        assert pending['item']['status'] == 'blocked'
        with d.independent() as other:
            durable = d.snapshot(iid, other)
            assert durable == before
        observed.append({'pending': pending, 'durable': durable})

    if caller == 'managed':
        with conn.transaction(force_rollback=True):
            exercise()
    else:
        conn.tpc_begin(conn.xid(2784, 'status-'+str(iid), 'caller'))
        try:
            exercise()
        finally:
            conn.tpc_rollback()
    with d.independent() as other:
        assert d.snapshot(iid, other) == before
    record_property('state_history', json.dumps({'history': 'caller-'+caller,
        'observations_before_caller_cleanup': observed, 'after_caller_rollback': before}, default=str, sort_keys=True))


def test_foreign_tenant_target_is_missing(status_driver, pg_test_scope):
    d = status_driver
    foreign = replace(d.store, repo_id=pg_test_scope('status-foreign'), authority_repo_uuid=str(uuid4()))
    pg.init_db(foreign)
    sid = pg.create_sprint(foreign, 'foreign status', status='active')
    tid = pg.get_or_create_track(foreign, sid, 'status')
    iid = pg.create_work_item(foreign, sid, tid, 'foreign item')
    before = pg.get_work_item(foreign, iid)
    with pytest.raises(ValueError, match=f'Item #{iid} not found'):
        d.transition(iid, 'active')
    assert pg.get_work_item(foreign, iid) == before
    assert pg.list_events(foreign, sid) == []
