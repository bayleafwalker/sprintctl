from tests.pg.test_locked_item_status import status_driver as writing_driver
from tests.pg._shared import PG_MARKS
from tests._locked_decision_writing_contract import LockedDecisionWritingCases

pytestmark = PG_MARKS


class TestPostgresLockedDecisionWriting(LockedDecisionWritingCases):
    pass

from dataclasses import replace
from datetime import datetime
import json
from uuid import uuid4

import pytest
from tests.pg._shared import pg
from tests.pg.test_locked_item_status import _consequences
from tests._locked_decision_writing_contract import owner, request, connection, assert_pending, KINDS


def native_fixture(d, awaiting):
    from tests.pg.test_work_lease import A, _run, _claim, TestAwaitingReportStamping
    if awaiting:
        iid, _, lease, report = TestAwaitingReportStamping()._awaiting(d.store, 'direct-'+uuid4().hex)
        item = pg.get_work_item(d.store, iid)
        d = replace(d, sprint=item['sprint_id'], track=item['track_id'])
    else:
        iid = d.new_item()
        lease = _claim(d.store, A, iid, _run(d.store, A), 'direct-'+uuid4().hex)['lease']
        report = None
    return d, iid, lease, report


def native_snapshot(d, iid, store=None):
    value = _consequences(d.store if store is None else store, iid)
    return value, {table: [{k: type(v).__name__ for k, v in row.items()} for row in rows]
                   for table, rows in value.items()}


@pytest.mark.parametrize('awaiting', (False, True))
@pytest.mark.parametrize('kind,resolution', KINDS)
def test_actual_direct_report_and_active_lease_effects(writing_driver, awaiting, kind, resolution, record_property):
    d, iid, lease, report = native_fixture(writing_driver, awaiting)
    successor = d.new_item() if kind == 'supersede' else None
    before, effects_before = d.snapshot(iid), native_snapshot(d, iid)
    with owner(d, iid) as (_, invoke):
        value = invoke(request(kind, superseded_by_item_id=successor))
        assert_pending(d)
        pending, (effects, native_types) = d.snapshot(iid), native_snapshot(d, iid)
        [stored] = effects['work_lease']
        assert stored['lease_id'] == lease['lease_id'] and isinstance(stored['acquired_at'], datetime)
        assert stored['state'] == ('settled' if resolution else 'active')
        assert stored['end_reason'] == ('item-'+resolution if resolution else None)
        assert (stored['ended_at'] is not None) is bool(resolution)
        if resolution:
            assert isinstance(stored['ended_at'], datetime)
        if awaiting:
            [stamped] = effects['work_outcome_report']
            assert stamped['report_id'] == report['report_id']
            assert (stamped['disposition'], stamped['reason_code'], stamped['decision_id']) == (
                'rejected', 'decided-accept-unverified' if kind == 'accept' else 'decided-'+kind, None)
        else:
            assert effects['work_outcome_report'] == []
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
            assert native_snapshot(d, iid, other) == effects_before
        connection(d).commit()
    with d.independent() as other:
        assert d.snapshot(iid, other) == pending
        assert native_snapshot(d, iid, other) == (effects, native_types)
    record_property('state_history', json.dumps({'history': f'direct-native-{kind}-{awaiting}',
        'before': before, 'native_before': effects_before, 'pending': pending,
        'native_pending': effects, 'native_type_metadata': native_types,
        'decision': value}, default=str, sort_keys=True))


@pytest.mark.parametrize('point', ('stamp', 'item-update', 'lease-update'))
def test_after_actual_native_effect_python_fault_leaves_pending_owner_state(writing_driver, point, monkeypatch, record_property):
    d, iid, lease, report = native_fixture(writing_driver, True)
    before, effects_before = d.snapshot(iid), native_snapshot(d, iid)
    observed = []
    with owner(d, iid) as (_, invoke), monkeypatch.context() as patch:
        def observe():
            assert_pending(d)
            pending, (effects, native_types) = d.snapshot(iid), native_snapshot(d, iid)
            assert len(pending['decisions']) == 1
            assert pending['item']['status'] == ('active' if point == 'stamp' else 'done')
            assert effects['work_outcome_report'][0]['reason_code'] == 'decided-accept-unverified'
            assert effects['work_lease'][0]['state'] == ('settled' if point == 'lease-update' else 'active')
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
                assert native_snapshot(d, iid, other) == effects_before
            observed.append({'pending': pending, 'native': effects, 'native_types': native_types})
            raise RuntimeError('after actual direct native effect')
        if point == 'stamp':
            original = pg._stamp_awaiting_reports
            def stamp(*args):
                original(*args)
                observe()
            patch.setattr(pg, '_stamp_awaiting_reports', stamp)
            cursor = invoke.cursor
        else:
            armed = True
            class Cursor:
                def execute(self, query, *args, **kwargs):
                    nonlocal armed
                    result = invoke.cursor.execute(query, *args, **kwargs)
                    sql = ' '.join(query.split()).upper()
                    target = 'UPDATE WORK_ITEM ' if point == 'item-update' else 'UPDATE WORK_LEASE '
                    if armed and sql.startswith(target):
                        armed = False
                        observe()
                    return self if result is invoke.cursor else result
                def __getattr__(self, name):
                    return getattr(invoke.cursor, name)
            cursor = Cursor()
        with pytest.raises(RuntimeError, match='actual direct native effect'):
            invoke(request(), cursor=cursor)
        assert len(observed) == 1
        assert_pending(d)
    with d.independent() as other:
        assert d.snapshot(iid, other) == before
        assert native_snapshot(d, iid, other) == effects_before
    with owner(d, iid) as (_, invoke):
        invoke(request())
        connection(d).commit()
    with d.independent() as other:
        final = d.snapshot(iid, other)
        assert len(final['decisions']) == 1
        assert native_snapshot(d, iid, other)[0]['work_lease'][0]['state'] == 'settled'
    record_property('state_history', json.dumps({'history': 'direct-native-fault-'+point,
        'before': before, 'native_before': effects_before, 'observed_before_owner_cleanup': observed,
        'rollback_then_retry': final}, default=str, sort_keys=True))


def test_superseded_awaiting_report_never_gains_verification(writing_driver, monkeypatch):
    from tests.pg.test_work_lease import TestAwaitingReportStamping
    d = writing_driver
    iid, report, _, lease = TestAwaitingReportStamping()._taken_over_under_090(
        d.store, monkeypatch, 'direct-sup-'+uuid4().hex)
    item = pg.get_work_item(d.store, iid)
    d = replace(d, sprint=item['sprint_id'], track=item['track_id'])
    with owner(d, iid) as (_, invoke):
        invoke(request())
        effects, _ = native_snapshot(d, iid)
        stamped = next(r for r in effects['work_outcome_report'] if r['report_id'] == report['report_id'])
        assert (stamped['disposition'], stamped['reason_code'], stamped['decision_id']) == ('rejected', 'claim-superseded', None)
        assert next(r for r in effects['work_lease'] if r['lease_id'] == lease['lease_id'])['end_reason'] == 'item-accepted'


def test_foreign_repository_successor_is_refused(writing_driver, pg_test_scope):
    d = writing_driver
    iid = d.new_item('active')
    foreign = replace(d.store, repo_id=pg_test_scope('direct-successor-foreign'))
    sid = pg.create_sprint(foreign, 'foreign successor', status='active')
    tid = pg.get_or_create_track(foreign, sid, 'foreign')
    other = pg.create_work_item(foreign, sid, tid, 'foreign successor')
    before = d.snapshot(iid)
    with owner(d, iid) as (_, invoke):
        with pytest.raises(ValueError, match='Superseding item .* not found'):
            invoke(request('supersede', superseded_by_item_id=other))
        assert_pending(d)
        assert d.snapshot(iid) == before


def test_complete_lease_retains_primitive_then_settlement_overwrite(writing_driver, monkeypatch, record_property):
    from tests.pg.test_work_lease import A, _run, _claim, _complete
    d = writing_driver
    iid = d.new_item()
    run = _run(d.store, A)
    lease = _claim(d.store, A, iid, run, 'direct-settlement-'+uuid4().hex)['lease']
    original, observed = pg._decide_locked, []
    def decide(cur, repo_id, item, normalized):
        value = original(cur, repo_id, item, normalized)
        row = cur.execute('SELECT state,end_reason,ended_at FROM work_lease WHERE repo_id=%s AND lease_id=%s',
                          (repo_id, lease['lease_id'])).fetchone()
        assert row['state'] == 'settled' and row['end_reason'] == 'item-accepted'
        assert isinstance(row['ended_at'], datetime)
        assert normalized['actor'] == pg.SETTLEMENT_ACTOR
        assert cur.execute('SELECT count(*) AS n FROM work_outcome_report WHERE repo_id=%s AND work_item_id=%s',
                           (repo_id, iid)).fetchone()['n'] == 0
        observed.append({'generic_primitive_lease': dict(row), 'decision': value})
        return value
    with monkeypatch.context() as patch:
        patch.setattr(pg, '_decide_locked', decide)
        result = _complete(d.store, A, lease['lease_id'], run, 'direct-complete-'+uuid4().hex)
    assert result['settled'] is True and len(observed) == 1
    with d.independent() as other:
        effects, types = native_snapshot(d, iid, other)
        assert effects['work_lease'][0]['end_reason'] == 'settled'
        assert effects['work_outcome_report'][0]['decision_id'] == observed[0]['decision']['id']
        assert effects['work_outcome_report'][0]['disposition'] == 'settled'
    record_property('state_history', json.dumps({'history': 'actual-complete-lease-overwrite',
        'inside_primitive': observed, 'final_native_effects': effects, 'native_types': types,
        'published_result': result}, default=str, sort_keys=True))
