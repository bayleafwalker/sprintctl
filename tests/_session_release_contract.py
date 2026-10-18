"""Pre-extraction actual-backend oracle for the caller-owned release primitive."""
from copy import deepcopy
from dataclasses import dataclass
import json
import threading

import pytest

from sprintctl import reservation

SESSION = ' target-session '
ACTOR = ' releasing actor '
NOW = '2026-01-02T03:04:05Z'


def selected_query(statement):
    value = ' '.join(str(statement).split())
    return value.startswith('SELECT * FROM reservation WHERE') and 'session_id =' in value


class OrderedCursor:
    def __init__(self, cursor, log=None):
        self.cursor, self.selected, self.log, self.phase = cursor, False, log, 'other'

    def execute(self, statement, *args, **kwargs):
        self.selected = selected_query(statement)
        value = ' '.join(str(statement).split())
        self.phase = 'select' if self.selected else ('update' if value.startswith("UPDATE reservation SET state = 'released'") else 'other')
        self.cursor.execute(statement, *args, **kwargs)
        return self

    def fetchall(self):
        rows = self.cursor.fetchall()
        if self.selected:
            rows = sorted(rows, key=lambda row: row['id'])
            # Authored nonmonotonic order catches either ascending or descending sorting.
            return rows[1:] + rows[:1]
        return rows

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        result = self.cursor.__exit__(*args)
        if self.log is not None:
            self.log.append(('cursor-exit', self.phase))
        return result

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class ConnectionProbe:
    def __init__(self, conn, *, forbid=False, log=None):
        self.conn, self.forbid, self.log = conn, forbid, log

    def execute(self, statement, *args, **kwargs):
        assert not self.forbid, 'falsey session reached SQL'
        cur = self.conn.execute(statement, *args, **kwargs)
        proxy = OrderedCursor(cur)
        proxy.selected = selected_query(statement)
        return proxy

    def cursor(self, *args, **kwargs):
        assert not self.forbid, 'falsey session opened cursor'
        return OrderedCursor(self.conn.cursor(*args, **kwargs), self.log)

    def commit(self):
        pytest.fail('release primitive committed caller transaction')

    def rollback(self):
        pytest.fail('release primitive rolled back caller transaction')

    def __getattr__(self, name):
        return getattr(self.conn, name)


@dataclass
class ReleaseDriver:
    api: object
    handle: object
    item: dict
    independent: object
    begin: object
    pending_write: object
    raw_insert: object
    fault: object
    lock_item: object
    arm_wait: object
    await_wait: object
    proxy_handle: object
    is_pg: bool

    @property
    def conn(self):
        return self.handle.conn if self.is_pg else self.handle

    def tx(self):
        return self.conn.info.transaction_status.name if self.is_pg else ('ACTIVE' if self.conn.in_transaction else 'IDLE')

    def reserve(self, **kwargs):
        return self.api.reserve(self.handle, self.item['id'], actor=kwargs.pop('actor', 'seed actor'),
            session_id=kwargs.pop('session_id', SESSION), **kwargs)

    def release(self, *, item=None, session_id=SESSION, actor=ACTOR, handle=None):
        return self.api._release_caller_reservations_locked(self.handle if handle is None else handle,
            self.item if item is None else item, session_id=session_id, actor=actor)

    def snapshot(self, handle=None):
        def read(owner):
            rows = self.api.list_reservations(owner, self.item['id'], active_only=False)
            return {'item': self.api.get_work_item(owner, self.item['id']),
                'rows': sorted([{k: v for k, v in row.items() if k not in ('stale', 'activity_age_seconds')}
                    for row in rows], key=lambda row: row['id']),
                'events': sorted([e for e in self.api.list_events(owner, self.item['sprint_id'])
                    if e['work_item_id'] == self.item['id']], key=lambda e: e['id'])}
        if handle is not None:
            return read(handle)
        with self.independent() as owner:
            return read(owner)


def retain(record_property, name, value):
    record_property('state_history', json.dumps({'name': name, **value}, default=str))


class SessionReleaseCases:
    @pytest.mark.parametrize('session', [None, '', 0])
    def test_falsey_session_precedes_item_access_sql_and_clock(self, release_driver, monkeypatch, session):
        d = release_driver
        monkeypatch.setattr(reservation, 'now_text', lambda: pytest.fail('falsey session reached clock'))
        before = d.snapshot()
        handle = d.proxy_handle(ConnectionProbe(d.conn, forbid=True))
        d.begin()
        try:
            d.pending_write()
            assert d.api._release_caller_reservations_locked(handle, None, session_id=session, actor=None) == []
            assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            assert d.snapshot(d.handle)['item']['assignee'] == 'pending caller'
            assert d.snapshot() == before
        finally:
            d.conn.rollback()
        assert d.snapshot() == before

    def test_empty_selection_avoids_clock_and_unused_sprint_id(self, release_driver, monkeypatch):
        d = release_driver
        monkeypatch.setattr(reservation, 'now_text', lambda: pytest.fail('empty selection reached clock'))
        d.begin()
        try:
            d.pending_write()
            assert d.release(item={'id': str(d.item['id'])}) == []
            assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            assert d.snapshot(d.handle)['item']['assignee'] == 'pending caller'
            assert d.snapshot()['item']['assignee'] == 'original'
        finally:
            d.conn.rollback()

    @pytest.mark.parametrize('item,error', [({}, KeyError), ({'id': 'not-an-id'}, ValueError)])
    def test_item_conversion_precedes_sql(self, release_driver, item, error):
        d = release_driver
        handle = d.proxy_handle(ConnectionProbe(d.conn, forbid=True))
        with pytest.raises(error):
            d.api._release_caller_reservations_locked(handle, item, session_id=SESSION, actor=ACTOR)
        assert d.tx() == 'IDLE'

    @pytest.mark.parametrize('finish', ['commit', 'rollback'])
    def test_mixed_roles_exact_session_clock_order_and_owner(self, release_driver, monkeypatch, record_property, finish):
        d = release_driver
        selected = [d.reserve(role=role, actor='other actor') for role in ('execution', 'verification', 'observation')]
        foreign = d.reserve(role='observation', actor=ACTOR, session_id=SESSION.strip())
        terminal = d.reserve(role='observation')
        d.api.release_reservation(d.handle, terminal['id'], actor='fixture')
        before = d.snapshot()
        calls = []
        monkeypatch.setattr(reservation, 'now_text', lambda: calls.append('clock') or NOW)
        handle = d.proxy_handle(ConnectionProbe(d.conn))
        d.begin()
        try:
            d.pending_write()
            original_item = deepcopy(d.item)
            ids = d.release(handle=handle)
            expected_ids = [selected[1]['id'], selected[2]['id'], selected[0]['id']]
            assert ids == expected_ids and all(type(i) is int for i in ids)
            assert d.item == original_item and calls == ['clock']
            assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            pending = d.snapshot(d.handle)
            old = {r['id']: r for r in before['rows']}
            rows = {r['id']: r for r in pending['rows']}
            assert pending['item']['assignee'] == 'pending caller'
            for row in selected:
                actual = rows[row['id']]
                assert actual['state'] == 'released'
                assert reservation.parse_time(actual['released_at']) == reservation.parse_time(NOW)
                assert actual['last_activity_at'] == actual['released_at']
            assert rows[foreign['id']] == old[foreign['id']] and rows[terminal['id']] == old[terminal['id']]
            events = pending['events'][len(before['events']):]
            assert [e['event_type'] for e in events] == ['reservation.released'] * 3
            assert [json.loads(e['payload']) for e in events] == [{'reservation_id': i} for i in expected_ids]
            assert all(e['actor'] == ACTOR and e['source_type'] == 'system' for e in events)
            assert d.snapshot() == before
            retain(record_property, 'mixed-owner-' + finish, {'before': before, 'pending': pending,
                'returned_ids': ids, 'owner_state_before_cleanup': d.tx()})
            getattr(d.conn, finish)()
            assert d.snapshot() == (pending if finish == 'commit' else before)
        finally:
            d.conn.rollback()

    @pytest.mark.parametrize('missing', [True, False])
    def test_late_sprint_conversion_error_leaves_actual_update_pending(self, release_driver, monkeypatch, record_property, missing):
        d = release_driver
        original = d.reserve(role='observation')
        before = d.snapshot()
        item = {'id': d.item['id']} if missing else {**d.item, 'sprint_id': 'not-a-sprint'}
        monkeypatch.setattr(reservation, 'now_text', lambda: NOW)
        d.begin()
        try:
            d.pending_write()
            with pytest.raises(KeyError if missing else ValueError):
                d.release(item=item)
            assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            pending = d.snapshot(d.handle)
            assert pending['rows'][0]['id'] == original['id'] and pending['rows'][0]['state'] == 'released'
            assert pending['events'] == before['events'] and pending['item']['assignee'] == 'pending caller'
            assert d.snapshot() == before
            retain(record_property, 'late-sprint-conversion', {'pending': pending, 'durable': before,
                'owner_state_before_cleanup': d.tx()})
        finally:
            d.conn.rollback()
        assert d.snapshot() == before

    def test_clock_hook_added_matching_row_preserves_selected_vs_updated_population(self, release_driver, monkeypatch, record_property):
        d = release_driver
        first = d.reserve(role='observation')
        before = d.snapshot()
        added = []
        def clock():
            added.append(d.raw_insert())  # Raw valid observation row, SAME owner, no commit/public reserve.
            return NOW
        monkeypatch.setattr(reservation, 'now_text', clock)
        d.begin()
        try:
            ids = d.release()
            assert ids == [first['id']] and len(added) == 1
            assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            pending = d.snapshot(d.handle)
            assert {r['id'] for r in pending['rows'] if r['state'] == 'released'} == {first['id'], added[0]}
            assert [json.loads(e['payload']) for e in pending['events'][len(before['events']):]] == [{'reservation_id': first['id']}]
            assert d.snapshot() == before
            retain(record_property, 'deliberate-same-owner-population-probe', {'pending': pending,
                'durable': before, 'returned_ids': ids, 'late_raw_id': added[0]})
        finally:
            d.conn.rollback()

    @pytest.mark.parametrize('position', [1, 2])
    def test_after_actual_event_python_failure_leaves_cleanup_to_owner(self, release_driver, monkeypatch, record_property, position):
        d = release_driver
        [d.reserve(role='observation') for _ in range(2)]
        before = d.snapshot()
        original, seen = d.api._insert_event, []
        def insert(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == 'reservation.released':
                seen.append(result)
                if len(seen) == position:
                    raise RuntimeError('injected after-real-release event')
            return result
        d.begin()
        try:
            d.pending_write()
            with monkeypatch.context() as fault:
                fault.setattr(d.api, '_insert_event', insert)
                with pytest.raises(RuntimeError, match='after-real-release'):
                    d.release()
            assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            pending = d.snapshot(d.handle)
            assert all(row['state'] == 'released' for row in pending['rows'])
            assert len(pending['events']) == len(before['events']) + position
            assert pending['item']['assignee'] == 'pending caller' and d.snapshot() == before
            retain(record_property, 'after-event-' + str(position), {'pending': pending,
                'durable': before, 'owner_state_before_cleanup': d.tx()})
        finally:
            d.conn.rollback()
        assert d.snapshot() == before
        d.begin()
        try:
            assert len(d.release()) == 2
            d.conn.commit()
        finally:
            d.conn.rollback()

    def test_real_update_sql_fault_does_not_rollback_owner(self, release_driver, record_property):
        d = release_driver
        d.reserve(role='observation')
        before = d.snapshot()
        with d.fault():
            d.begin()
            d.pending_write()
            with pytest.raises(Exception, match='injected session-release SQL fault'):
                d.release()
            state = d.tx()
            assert state == ('INERROR' if d.is_pg else 'ACTIVE')
            if not d.is_pg:
                pending = d.snapshot(d.handle)
                assert pending['item']['assignee'] == 'pending caller'
                assert pending['rows'] == before['rows'] and pending['events'] == before['events']
            else:
                from psycopg.errors import InFailedSqlTransaction
                with pytest.raises(InFailedSqlTransaction):
                    d.snapshot(d.handle)
                assert d.tx() == 'INERROR'
            assert d.snapshot() == before
            retain(record_property, 'actual-update-SQL-fault', {'owner_state_before_cleanup': state,
                'durable': before, 'primary_rows_unreadable_until_rollback': d.is_pg})
            d.conn.rollback()
        assert d.snapshot() == before

    @pytest.mark.parametrize('finish', ['commit', 'rollback'])
    def test_existing_owner_lock_gates_public_admission(self, release_driver, record_property, finish):
        d = release_driver
        prior = d.reserve(role='observation')
        d.begin()
        d.lock_item()
        ready = threading.Event()
        signal, results, errors = [], [], []
        def contender():
            try:
                with d.independent() as other:
                    signal.append(d.arm_wait(other))
                    ready.set()
                    results.append(d.api.reserve(other, d.item['id'], actor='contender', session_id=SESSION, role='observation'))
            except BaseException as exc:
                errors.append(exc)
                ready.set()
        worker = threading.Thread(target=contender)
        worker.start()
        try:
            assert ready.wait(5) and signal
            assert d.await_wait(signal[0]) and worker.is_alive() and results == []
            ids = d.release()
            assert ids == [prior['id']] and d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE')
            pending = d.snapshot(d.handle)
            getattr(d.conn, finish)()
        finally:
            d.conn.rollback()
            worker.join(15)
        assert not worker.is_alive() and errors == [] and len(results) == 1
        durable = d.snapshot()
        rows = {r['id']: r for r in durable['rows']}
        assert rows[prior['id']]['state'] == ('released' if finish == 'commit' else 'active')
        assert rows[results[0]['id']]['state'] == 'active'
        assert results[0]['conflict'] is (finish == 'rollback')
        retain(record_property, 'owner-lock-admission-' + finish, {'pending_before_owner_finish': pending,
            'returned_ids': ids, 'admission': results[0], 'durable': durable,
            'wait_observed': 'PG exact blocking PID' if d.is_pg else 'SQLite attempted BEGIN'})
