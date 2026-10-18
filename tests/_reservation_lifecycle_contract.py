"""Coordinator-owned public-behavior oracle; source repair must satisfy it."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest


@dataclass
class Driver:
    api: object
    handle: object
    item: int
    sprint: int
    track: int
    independent: object
    backdate: object
    arm_wait: object
    await_wait: object

    @property
    def sql_driver(self):
        return getattr(self.api, "_ReservationLifecyclePg", None) or self.api._ReservationLifecycleSqlite

    def reserve(self, **kwargs):
        return self.api.reserve(self.handle, kwargs.pop('item_id', self.item),
            actor=kwargs.pop('actor', 'operator'), session_id=kwargs.pop('session_id', 'session'), **kwargs)

    def snapshot(self):
        with self.independent() as handle:
            rows = self.api.list_reservations(handle, self.item, active_only=False)
            return {
                'rows': sorted([{k: v for k, v in r.items() if k not in ('stale', 'activity_age_seconds')} for r in rows], key=lambda r: r['id']),
                'releases': self.api.list_releases(handle, self.item),
                'events': sorted([e for e in self.api.list_events(handle, self.sprint) if e['work_item_id'] == self.item], key=lambda e: e['id']),
            }

    def fail_event(self, monkeypatch, event_type, position=1):
        original = self.api._insert_event
        seen = []
        def insert(*args, **kwargs):
            result = original(*args, **kwargs)
            kind = args[3] if len(args) > 3 else kwargs['event_type']
            if kind == event_type:
                seen.append(kind)
                if len(seen) == position:
                    raise RuntimeError('injected lifecycle event failure')
            return result
        monkeypatch.setattr(self.api, '_insert_event', insert)
        return seen


class LifecycleCases:
    @pytest.mark.parametrize('operation', ['reassign', 'release'])
    def test_changed_row_and_event_failure_roll_back(self, lifecycle_driver, monkeypatch, operation):
        d = lifecycle_driver
        row = d.reserve()
        before = d.snapshot()
        kind = 'reservation.' + ('reassigned' if operation == 'reassign' else 'released')
        original_insert = d.api._insert_event
        seen = d.fail_event(monkeypatch, kind)
        with pytest.raises(RuntimeError, match='injected lifecycle'):
            if operation == 'reassign':
                d.api.reassign_reservation(d.handle, row['id'], actor='new', session_id='new')
            else:
                d.api.release_reservation(d.handle, row['id'], actor='new')
        assert seen == [kind]
        assert d.snapshot() == before
        monkeypatch.setattr(d.api, "_insert_event", original_insert)
        invoke(d, operation, row['id'])
        after = d.snapshot()
        assert len(after['events']) == len(before['events']) + 1
        assert after['releases'] == before['releases']

    @pytest.mark.parametrize('position', [1, 2])
    def test_sweep_partial_event_failure_rolls_back_selected_batch(self, lifecycle_driver, monkeypatch, position):
        d = lifecycle_driver
        selected = [d.reserve(), d.reserve(session_id='second')]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        d.backdate([r['id'] for r in selected], (now - timedelta(days=30)).isoformat())
        before = d.snapshot()
        original_insert = d.api._insert_event
        seen = d.fail_event(monkeypatch, 'reservation.interrupted', position)
        with pytest.raises(RuntimeError, match='injected lifecycle'):
            d.api.sweep_stale_reservations(d.handle, now=now.isoformat(), interrupt_after=timedelta(days=7))
        assert len(seen) == position
        assert d.snapshot() == before
        monkeypatch.setattr(d.api, '_insert_event', original_insert)
        changed = d.api.sweep_stale_reservations(d.handle, now=now.isoformat(), interrupt_after=timedelta(days=7))
        assert [r['id'] for r in changed] == [r['id'] for r in selected]
        assert len(d.snapshot()['events']) == len(before['events']) + 2

    def test_reassign_release_and_terminal_noop_keep_event_contract(self, lifecycle_driver):
        d = lifecycle_driver
        row = d.reserve(actor='old', session_id='old')
        reassigned = d.api.reassign_reservation(d.handle, row['id'], actor='new', session_id='new', correlation_ref='handoff')
        assert (reassigned['actor'], reassigned['session_id'], reassigned['correlation_ref']) == ('new', 'new', 'handoff')
        released = d.api.release_reservation(d.handle, row['id'])
        assert released['state'] == 'released'
        before = d.snapshot()
        assert d.api.release_reservation(d.handle, row['id'], actor='ignored')['state'] == 'released'
        assert d.snapshot() == before
        events = before['events']
        assert [e['event_type'] for e in events] == ['reservation.reserved', 'reservation.reassigned', 'reservation.released']
        assert events[-1]['actor'] == 'new'
        assert json.loads(events[1]['payload']) == {'reservation_id': row['id'], 'previous_actor': 'old', 'previous_session_id': 'old'}
        assert reassigned['release_digest'] == released['release_digest'] == row['release_digest']

    def test_touch_same_session_has_no_lifecycle_event(self, lifecycle_driver):
        d = lifecycle_driver
        row = d.reserve()
        before = d.snapshot()
        updated = d.api.touch_reservation(d.handle, row['id'], session_id='session', correlation_ref='outside-work')
        after = d.snapshot()
        assert updated['session_id'] == 'session' and updated['correlation_ref'] == 'outside-work'
        assert after['events'] == before['events']
        assert after['releases'] == before['releases']
        with pytest.raises(ValueError, match='another session'):
            d.api.touch_reservation(d.handle, row['id'], session_id='stranger')
        assert d.snapshot() == after

    def test_no_candidate_sweep_has_no_domain_effect(self, lifecycle_driver):
        d = lifecycle_driver
        d.reserve()
        before = d.snapshot()
        assert d.api.sweep_stale_reservations(d.handle, interrupt_after=timedelta(days=7)) == []
        assert d.snapshot() == before

    def test_sweep_preserves_fresh_and_done_item_reservations(self, lifecycle_driver):
        d = lifecycle_driver
        selected = [d.reserve(), d.reserve(session_id='second')]
        fresh = d.reserve(role='observation')
        done_item = d.api.create_work_item(d.handle, d.sprint, d.track, 'closed history')
        closed = d.reserve(item_id=done_item)
        d.api.set_work_item_status(d.handle, done_item, 'active')
        d.api.set_work_item_status(d.handle, done_item, 'done')
        now = datetime.now(timezone.utc).replace(microsecond=0)
        d.backdate([r['id'] for r in selected] + [closed['id']], (now - timedelta(days=30)).isoformat())
        closed_before = d.api.get_reservation(d.handle, closed['id'])
        before = d.snapshot()
        changed = d.api.sweep_stale_reservations(d.handle, now=now.isoformat(), interrupt_after=timedelta(days=7))
        assert {r['id'] for r in changed} == {r['id'] for r in selected}
        assert all(r['state'] == 'interrupted' for r in changed)
        assert d.api.get_reservation(d.handle, fresh['id'])['state'] == 'active'
        closed_after = d.api.get_reservation(d.handle, closed['id'])
        for key in ('state', 'released_at', 'last_activity_at', 'interruption_reason', 'session_id'):
            assert closed_after[key] == closed_before[key]
        after = d.snapshot()
        assert after['releases'] == before['releases']
        events = after['events'][len(before['events']):]
        assert len(events) == 2 and all(e['event_type'] == 'reservation.interrupted' for e in events)
        assert {json.loads(e['payload'])['reservation_id'] for e in events} == {r['id'] for r in selected}

    @pytest.mark.parametrize('operation', ['touch', 'reassign', 'release'])
    def test_activity_clock_is_sampled_after_lock(self, lifecycle_driver, monkeypatch, operation):
        from sprintctl import reservation
        d = lifecycle_driver
        row = d.reserve()
        locked = []
        original = d.sql_driver.lock_one
        def lock(driver, ident):
            result = original(driver, ident)
            locked.append(ident)
            return result
        def clock():
            assert locked == [row['id']]
            return '2030-01-02T03:04:05Z'
        monkeypatch.setattr(d.sql_driver, 'lock_one', lock)
        monkeypatch.setattr(reservation, 'now_text', clock)
        assert reservation.parse_time(invoke(d, operation, row['id'])['last_activity_at']) == reservation.parse_time('2030-01-02T03:04:05Z')

    def test_handoff_between_peek_and_lock_refuses_old_session_touch(self, lifecycle_driver, monkeypatch):
        d = lifecycle_driver
        row = d.reserve()
        original = d.sql_driver.peek
        seen = []
        def peek(driver, ident):
            result = original(driver, ident)
            handle = getattr(driver, 'store', None) or getattr(driver, 'conn', None)
            if handle is d.handle and not seen:
                seen.append(ident)
                with d.independent() as other:
                    d.api.reassign_reservation(other, ident, actor='new', session_id='new', correlation_ref='handoff')
            return result
        monkeypatch.setattr(d.sql_driver, 'peek', peek)
        with pytest.raises(ValueError, match='another session'):
            d.api.touch_reservation(d.handle, row['id'], session_id='session', correlation_ref='stale-touch')
        final = d.api.get_reservation(d.handle, row['id'])
        assert (final['session_id'], final['correlation_ref']) == ('new', 'handoff')

    @pytest.mark.parametrize('first,second', [
        ('touch', 'reassign'), ('reassign', 'touch'), ('release', 'release'),
        ('sweep', 'touch'), ('touch', 'sweep'),
        ('sweep', 'completion'), ('completion', 'sweep'),
    ])
    def test_competing_operations_observe_locked_winner(self, lifecycle_driver, monkeypatch, first, second, record_property):
        d = lifecycle_driver
        row = d.reserve()
        d.backdate([row['id']], '2000-01-01T00:00:00Z')
        d.api.set_work_item_status(d.handle, d.item, 'active')
        worker_ready = threading.Event()
        result = []
        holder = {}
        trace = []
        def compete():
            try:
                with d.independent() as other:
                    holder['wait'] = d.arm_wait(other)
                    worker_ready.set()
                    trace.append('second-invoked')
                    rival = Driver(d.api, other, d.item, d.sprint, d.track, d.independent, d.backdate, d.arm_wait, d.await_wait)
                    result.append(invoke(rival, second, row['id']))
            except Exception as error:
                result.append(error)
            finally:
                trace.append('second-completed')
        worker = threading.Thread(target=compete)
        target = d.sql_driver
        method = 'lock_stale' if first == 'sweep' else 'lock_one'
        if first == 'completion':
            target = getattr(d.api, '_WorkItemPg', None) or d.api._WorkItemSqlite
            method = 'lock_for_update'
        original = getattr(target, method)
        def lock(driver, *args):
            locked = original(driver, *args)
            handle = (getattr(driver, 'store', None) or getattr(driver, 'conn', None)
                      or getattr(driver, '_store', None) or getattr(driver, '_conn', None))
            if handle is d.handle:
                trace.append('first-locked')
                worker.start()
                assert worker_ready.wait(5)
                assert d.await_wait(d.handle, holder['wait'])
                trace.append('second-wait-observed')
                assert result == []
            return locked
        monkeypatch.setattr(target, method, lock)
        try:
            trace.append('first-invoked')
            invoke(d, first, row['id'])
            trace.append('first-completed')
        finally:
            if worker.ident is not None:
                worker.join(10)
        assert not worker.is_alive()
        assert len(result) == 1
        assert trace.index('first-locked') < trace.index('second-wait-observed') < trace.index('first-completed')
        final = d.api.get_reservation(d.handle, row['id'])
        if (first, second) in [('reassign', 'touch'), ('sweep', 'touch')]:
            assert isinstance(result[0], ValueError)
        else:
            assert not isinstance(result[0], Exception), result[0]
        expected = {'reassign': 'active', 'release': 'released', 'sweep': 'interrupted', 'completion': 'active'}
        state = expected[first] if first != 'touch' else ('active' if second == 'sweep' else expected[second])
        assert final['state'] == state
        events = d.snapshot()['events']
        kinds = [e['event_type'] for e in events]
        assert kinds.count('reservation.released') == (1 if first == 'release' else 0)
        assert kinds.count('reservation.interrupted') == (1 if first == 'sweep' else 0)
        assert kinds.count('reservation.reassigned') == (1 if 'reassign' in (first, second) else 0)
        if 'completion' in (first, second):
            assert d.api.get_work_item(d.handle, d.item)['status'] == 'done'
        record_property('reservation_history', json.dumps({
            'backend': d.api.__name__, 'first': first, 'second': second,
            'observations': trace, 'second_error': type(result[0]).__name__ if isinstance(result[0], Exception) else None,
            'final_state': final['state'], 'final_session': final['session_id'],
            'event_types': kinds,
        }, sort_keys=True))

    @pytest.mark.parametrize('operation', ['touch', 'reassign', 'release', 'sweep'])
    def test_result_construction_failure_rolls_back(self, lifecycle_driver, monkeypatch, operation):
        from sprintctl import reservation
        d = lifecycle_driver
        row = d.reserve()
        d.backdate([row['id']], '2000-01-01T00:00:00Z')
        before = d.snapshot()
        calls = []
        original = reservation.display
        def display(*args, **kwargs):
            value = original(*args, **kwargs)
            calls.append(value['id'])
            raise RuntimeError('result construction failure')
        with monkeypatch.context() as fault:
            fault.setattr(reservation, 'display', display)
            with pytest.raises(RuntimeError, match='result construction'):
                invoke(d, operation, row['id'])
        assert calls == [row['id']]
        assert d.snapshot() == before
        invoke(d, operation, row['id'])


    @pytest.mark.parametrize('operation', ['reassign', 'release'])
    def test_predecessor_and_fallback_actor_come_from_locked_row(self, lifecycle_driver, monkeypatch, operation):
        d = lifecycle_driver
        row = d.reserve(actor='original', session_id='original')
        original = d.sql_driver.peek
        changed = []
        def peek(driver, ident):
            value = original(driver, ident)
            handle = getattr(driver, 'store', None) or getattr(driver, 'conn', None)
            if handle is d.handle and not changed:
                changed.append(ident)
                with d.independent() as other:
                    d.api.reassign_reservation(other, ident, actor='intervening', session_id='intervening')
            return value
        monkeypatch.setattr(d.sql_driver, 'peek', peek)
        invoke(d, operation, row['id'])
        event = d.snapshot()['events'][-1]
        if operation == 'release':
            assert event['actor'] == 'intervening'
        else:
            assert json.loads(event['payload']) == {'reservation_id': row['id'], 'previous_actor': 'intervening', 'previous_session_id': 'intervening'}


def invoke(d, operation, ident):
    if operation == 'completion':
        return d.api.set_work_item_status(d.handle, d.item, 'done')
    if operation == 'touch':
        return d.api.touch_reservation(d.handle, ident, session_id='session')
    if operation == 'reassign':
        return d.api.reassign_reservation(d.handle, ident, actor='new', session_id='new')
    if operation == 'release':
        return d.api.release_reservation(d.handle, ident)
    return d.api.sweep_stale_reservations(d.handle)
