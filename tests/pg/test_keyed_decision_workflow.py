from dataclasses import replace
import json
from uuid import uuid4

import pytest

from tests.pg.test_locked_item_status import status_driver as decision_driver
from tests.pg.test_locked_item_status import _consequences
from tests.pg._shared import PG_MARKS, pg, psycopg
from tests._keyed_decision_contract import KeyedDecisionCases, decide

pytestmark = PG_MARKS


class TestPostgresKeyedDecision(KeyedDecisionCases):
    pass


@pytest.mark.parametrize('awaiting', (False, True))
@pytest.mark.parametrize('kind,resolution', [('accept', 'accepted'), ('reject', 'rejected'),
    ('withdraw', 'withdrawn'), ('supersede', 'superseded'), ('revise', None)])
def test_native_report_and_lease_consequences_and_replay(decision_driver, awaiting, kind, resolution):
    from tests.pg.test_work_lease import A, _run, _claim, TestAwaitingReportStamping
    d = decision_driver
    if awaiting:
        iid, _, lease, report = TestAwaitingReportStamping()._awaiting(d.store, 'keyed-'+uuid4().hex)
        item = pg.get_work_item(d.store, iid)
        d = replace(d, sprint=item['sprint_id'], track=item['track_id'])
    else:
        iid = d.new_item()
        lease = _claim(d.store, A, iid, _run(d.store, A), 'keyed-'+uuid4().hex)['lease']
    args = dict(rationale='explicit reason', idempotency_key=uuid4().hex)
    if kind == 'supersede': args['superseded_by_item_id'] = d.new_item()
    value, replayed = decide(d, iid, kind, **args)
    assert replayed is False
    with d.independent() as other:
        after = d.snapshot(iid, other)
        effects = _consequences(other, iid)
    [stored_lease] = effects['work_lease']
    assert stored_lease['lease_id'] == lease['lease_id']
    assert stored_lease['state'] == ('settled' if resolution else 'active')
    assert stored_lease['end_reason'] == ('item-'+resolution if resolution else None)
    assert (stored_lease['ended_at'] is not None) is bool(resolution)
    if awaiting:
        [stamped] = effects['work_outcome_report']
        assert stamped['report_id'] == report['report_id'] and stamped['disposition'] == 'rejected'
        assert stamped['reason_code'] == ('decided-accept-unverified' if kind == 'accept' else 'decided-'+kind)
        assert stamped['decision_id'] is None
    repeated, replayed = decide(d, iid, kind, **args)
    assert replayed is True and repeated == value
    with d.independent() as other:
        assert d.snapshot(iid, other) == after and _consequences(other, iid) == effects


@pytest.mark.parametrize('awaiting', (False, True))
def test_after_real_native_effect_fault_is_observed_before_rollback(decision_driver, awaiting, monkeypatch, record_property):
    from tests.pg.test_work_lease import A, _run, _claim, TestAwaitingReportStamping
    d = decision_driver
    if awaiting:
        iid, _, _, _ = TestAwaitingReportStamping()._awaiting(d.store, 'keyed-fault-'+uuid4().hex)
        item = pg.get_work_item(d.store, iid)
        d = replace(d, sprint=item['sprint_id'], track=item['track_id'])
    else:
        iid = d.new_item()
        _claim(d.store, A, iid, _run(d.store, A), 'keyed-fault-'+uuid4().hex)
    before, effects_before = d.snapshot(iid), _consequences(d.store, iid)
    captured = []
    original = pg._decide_locked
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        pending, effects = d.snapshot(iid), _consequences(d.store, iid)
        assert pending['item']['status'] == 'done' and len(pending['decisions']) == 1
        assert effects['work_lease'][0]['state'] == 'settled'
        if awaiting: assert effects['work_outcome_report'][0]['reason_code'] == 'decided-accept-unverified'
        with d.independent() as other:
            assert d.snapshot(iid, other) == before and _consequences(other, iid) == effects_before
        captured.append({'pending': pending, 'effects': effects})
        raise RuntimeError('after actual keyed native effects')
    key = uuid4().hex
    with monkeypatch.context() as m:
        m.setattr(pg, '_decide_locked', fail)
        with pytest.raises(RuntimeError, match='actual keyed native effects'):
            decide(d, iid, 'accept', idempotency_key=key)
    assert d.transaction() == 'IDLE'
    with d.independent() as other:
        assert d.snapshot(iid, other) == before and _consequences(other, iid) == effects_before
    assert decide(d, iid, 'accept', idempotency_key=key)[1] is False
    record_property('state_history', json.dumps({'history': 'native-keyed-fault-'+str(awaiting),
        'before': before, 'effects_before': effects_before, 'inside_fault': captured}, default=str, sort_keys=True))


def test_superseded_report_reason_precedes_new_accept(decision_driver, monkeypatch):
    from tests.pg.test_work_lease import TestAwaitingReportStamping
    d = decision_driver
    iid, report, _, lease = TestAwaitingReportStamping()._taken_over_under_090(d.store, monkeypatch, 'keyed-sup-'+uuid4().hex)
    decide(d, iid, 'accept', idempotency_key=uuid4().hex)
    with d.independent() as other:
        effects = _consequences(other, iid)
    stamped = next(r for r in effects['work_outcome_report'] if r['report_id'] == report['report_id'])
    assert (stamped['disposition'], stamped['reason_code'], stamped['decision_id']) == ('rejected', 'claim-superseded', None)
    assert next(r for r in effects['work_lease'] if r['lease_id'] == lease['lease_id'])['state'] == 'settled'


@pytest.mark.parametrize('caller', ('managed', 'two-phase'))
@pytest.mark.parametrize('replay', (False, True))
def test_forbidden_owner_boundary_retains_actual_pending_state(decision_driver, caller, replay, record_property):
    d = decision_driver
    iid = d.new_item('active')
    key = uuid4().hex
    if replay: decide(d, iid, idempotency_key=key)
    before = d.snapshot(iid)
    conn = d.store.conn
    conn.rollback()  # Only the preceding read-only test observation.
    observed = []
    def exercise():
        conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s', ('pending caller', d.store.repo_id, d.sprint))
        with pytest.raises(psycopg.ProgrammingError, match='rollback'):
            decide(d, iid, idempotency_key=key)
        assert conn.info.transaction_status.name == 'INTRANS'
        assert pg.get_sprint(d.store, d.sprint)['goal'] == 'pending caller'
        pending = d.snapshot(iid)
        assert len(pending['decisions']) == len(before['decisions']) + int(not replay)
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
            assert pg.get_sprint(other, d.sprint)['goal'] != 'pending caller'
        observed.append({'pending': pending, 'durable': before, 'caller_goal': 'pending caller'})
    if caller == 'managed':
        with conn.transaction(force_rollback=True): exercise()
    else:
        conn.tpc_begin(conn.xid(2789, 'keyed-'+str(iid), 'caller'))
        try: exercise()
        finally: conn.tpc_rollback()
    with d.independent() as other: assert d.snapshot(iid, other) == before
    record_property('state_history', json.dumps({'history': 'keyed-owner-'+caller+'-'+str(replay),
        'observed_before_caller_cleanup': observed}, sort_keys=True))


@pytest.mark.parametrize('replay', (False, True))
def test_key_cursor_lifetime_replay_rollback_and_fresh_event_order(decision_driver, replay, monkeypatch):
    d = decision_driver
    iid = d.new_item('active')
    key = uuid4().hex
    if replay: decide(d, iid, idempotency_key=key)
    trace, opened = [], []
    class Cursor:
        def __init__(self, raw): self.raw, self.item = raw, False
        def __getattr__(self, name): return getattr(self.raw, name)
        def __enter__(self): self.raw.__enter__(); return self
        def __exit__(self, *exc):
            try: return self.raw.__exit__(*exc)
            finally:
                if self.item: opened.remove(self);trace.append('item-cursor-exit')
        def execute(self, sql, params=None, **kwargs):
            if 'pg_advisory_xact_lock' in sql: trace.append('key-lock')
            if sql.startswith('SELECT * FROM work_item WHERE repo_id') and 'FOR UPDATE' in sql:
                self.item = True;opened.append(self);trace.append('item-lock')
            return self.raw.execute(sql, params, **kwargs)
    class Connection:
        def __getattr__(self, name): return getattr(d.store.conn, name)
        def cursor(self, *args, **kwargs): return Cursor(d.store.conn.cursor(*args, **kwargs))
        def rollback(self): trace.append(('rollback', bool(opened)));return d.store.conn.rollback()
        def commit(self): trace.append(('commit', bool(opened)));return d.store.conn.commit()
    store = replace(d.store, conn=Connection())
    original = pg._insert_event
    def event(*args, **kwargs):
        trace.append(('event', bool(opened)))
        return original(*args, **kwargs)
    monkeypatch.setattr(pg, '_insert_event', event)
    value, repeated = pg.record_decision_keyed(store, iid, 'revise', actor='reviewer', idempotency_key=key)
    assert repeated is replay and value['kind'] == 'revise'
    assert trace[:2] == ['key-lock', 'item-lock'] and opened == []
    if replay:
        assert trace[2:] == [('rollback', True), 'item-cursor-exit']
    else:
        assert trace[2:] == ['item-cursor-exit', ('event', False), ('commit', False)]


@pytest.mark.parametrize('kind', ('target', 'successor', 'release', 'independent-key'))
def test_repository_scope_retained(decision_driver, pg_test_scope, kind):
    d = decision_driver
    foreign = replace(d.store, repo_id=pg_test_scope('keyed-foreign'), authority_repo_uuid=str(uuid4()))
    pg.init_db(foreign)
    sid = pg.create_sprint(foreign, 'foreign keyed', status='active')
    tid = pg.get_or_create_track(foreign, sid, 'keyed')
    foreign_item = pg.create_work_item(foreign, sid, tid, 'foreign')
    iid = d.new_item('active')
    key = uuid4().hex
    if kind == 'target':
        with pytest.raises(ValueError, match='not found'): decide(d, foreign_item, idempotency_key=key)
    elif kind == 'successor':
        with pytest.raises(ValueError, match='Superseding item'):
            decide(d, iid, 'supersede', rationale='why', superseded_by_item_id=foreign_item, idempotency_key=key)
    elif kind == 'release':
        digest = pg.reserve(foreign, foreign_item, actor='foreign', session_id='foreign')['release_digest']
        with pytest.raises(ValueError, match='[Rr]elease'):
            decide(d, iid, release_digest=digest, idempotency_key=key)
    else:
        a, a_replayed = pg.record_decision_keyed(foreign, foreign_item, 'revise', actor='reviewer', idempotency_key=key)
        b, b_replayed = decide(d, iid, idempotency_key=key)
        assert a_replayed is b_replayed is False and a['id'] != b['id']
    assert pg.get_work_item(foreign, foreign_item)['status'] == 'pending'
