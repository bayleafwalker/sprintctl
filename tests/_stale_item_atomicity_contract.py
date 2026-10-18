"""Shared semantic oracle for temporary SQLite and disposable PostgreSQL."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from sprintctl import maintain

NOW = datetime(2030, 1, 1, 12)
WINDOW = timedelta(hours=4)
OLD = '2020-01-01T00:00:00Z'


class SweepFault(RuntimeError):
    pass


@dataclass
class StaleItemDriver:
    backend: object
    store: object
    source: int
    target: int
    item: int
    independent: object
    arm_wait: object
    await_wait: object

    def connection(self, store):
        return getattr(store, 'conn', store)

    def update(self, store, assignments, values, item=None, *, commit=True):
        conn = self.connection(store)
        if hasattr(store, 'repo_id'):
            with conn.cursor() as cur:
                cur.execute('UPDATE work_item SET '+assignments+' WHERE repo_id=%s AND id=%s',
                            (*values, store.repo_id, self.item if item is None else item))
        else:
            conn.execute('UPDATE work_item SET '+assignments.replace('%s', '?')+' WHERE id=?',
                         (*values, self.item if item is None else item))
        if commit:
            conn.commit()

    def make_stale(self, item=None):
        self.update(self.store, 'updated_at=%s', (OLD,), item)

    def snapshot(self, store):
        return {'item': self.backend.get_work_item(store, self.item),
                'events': self.backend.list_events(store, self.source)}

    def sweep(self, store=None):
        return maintain.sweep_stale_items(self.store if store is None else store,
                                         self.source, NOW, WINDOW, _m=self.backend)


class StaleItemAtomicityCases:
    @pytest.mark.parametrize('phase', ['before', 'after'])
    def test_event_failure_preserves_before_state_and_retry_emits_once(self, stale_item_driver, monkeypatch, phase):
        d = stale_item_driver
        before = d.snapshot(d.store)
        original = d.backend._insert_event

        def fail(store, sprint, actor, kind, *args, **kwargs):
            if kind != 'auto-blocked-stale':
                return original(store, sprint, actor, kind, *args, **kwargs)
            if phase == 'after':
                original(store, sprint, actor, kind, *args, **kwargs)
            raise SweepFault('event '+phase)

        with monkeypatch.context() as patch:
            patch.setattr(d.backend, '_insert_event', fail)
            with pytest.raises(SweepFault, match='event '+phase):
                d.sweep()
        with d.independent() as other:
            assert d.snapshot(other) == before
        [affected] = d.sweep()
        assert affected['id'] == d.item
        assert d.backend.get_work_item(d.store, d.item)['status'] == 'blocked'
        events = [e for e in d.backend.list_events(d.store, d.source)
                  if e['event_type'] == 'auto-blocked-stale']
        assert len(events) == 1
        assert d.sweep() == []

    def test_affected_projection_and_explicit_clock_are_preserved(self, stale_item_driver):
        d = stale_item_driver
        [before] = d.backend.list_work_items(d.store, sprint_id=d.source, status='active')
        [affected] = d.sweep()
        assert affected == before
        assert affected['status'] == 'active'
        event = next(e for e in d.backend.list_events(d.store, d.source)
                     if e['event_type'] == 'auto-blocked-stale')
        import json
        payload = json.loads(event['payload']) if isinstance(event['payload'], str) else event['payload']
        assert payload['threshold_seconds'] == int(WINDOW.total_seconds())
        assert payload['idle_seconds'] == 315662400

    @pytest.mark.parametrize('change', ['refreshed', 'terminal', 'moved', 'deleted', 'blocked'])
    def test_selection_is_rechecked_before_blocking(self, stale_item_driver, monkeypatch, change):
        d = stale_item_driver
        original = d.backend.list_work_items
        changed = False

        def select(store, *args, **kwargs):
            nonlocal changed
            rows = original(store, *args, **kwargs)
            if store is d.store and kwargs.get('status') == 'active' and not changed:
                changed = True
                with d.independent() as other:
                    if change == 'refreshed':
                        d.update(other, 'updated_at=%s', (NOW.strftime('%Y-%m-%dT%H:%M:%SZ'),))
                    elif change == 'terminal':
                        d.backend.record_decision(other, d.item, 'withdraw', actor='other', rationale='fixture closes source')
                    elif change == 'moved':
                        track = d.backend.get_or_create_track(other, d.target, 'moved')
                        d.update(other, 'sprint_id=%s,track_id=%s', (d.target, track))
                    elif change == 'blocked':
                        d.backend.set_work_item_status(other, d.item, 'blocked')
                    else:
                        conn = d.connection(other)
                        if hasattr(other, 'repo_id'):
                            with conn.cursor() as cur:
                                cur.execute('DELETE FROM work_item WHERE repo_id=%s AND id=%s', (other.repo_id,d.item))
                        else:
                            conn.execute('DELETE FROM work_item WHERE id=?', (d.item,))
                        conn.commit()
            return rows

        monkeypatch.setattr(d.backend, 'list_work_items', select)
        assert d.sweep() == []
        assert changed
        assert not [e for e in d.backend.list_events(d.store, d.source)
                    if e['event_type'] == 'auto-blocked-stale']

    def test_later_item_failure_preserves_earlier_commit_and_retry(self, stale_item_driver, monkeypatch):
        d = stale_item_driver
        track = d.backend.get_or_create_track(d.store, d.source, 'second')
        second = d.backend.create_work_item(d.store, d.source, track, 'later item')
        d.backend.set_work_item_status(d.store, second, 'active')
        d.make_stale(second)
        original = d.backend._insert_event

        def fail_later(store, sprint, actor, kind, *args, **kwargs):
            result = original(store, sprint, actor, kind, *args, **kwargs)
            if kind == 'auto-blocked-stale' and kwargs.get('work_item_id') == second:
                raise SweepFault('second item event')
            return result

        with monkeypatch.context() as patch:
            patch.setattr(d.backend, '_insert_event', fail_later)
            with pytest.raises(SweepFault, match='second item'):
                d.sweep()
        with d.independent() as other:
            assert d.backend.get_work_item(other, d.item)['status'] == 'blocked'
            assert d.backend.get_work_item(other, second)['status'] == 'active'
            events = [e for e in d.backend.list_events(other, d.source)
                      if e['event_type'] == 'auto-blocked-stale']
            assert [e['work_item_id'] for e in events] == [d.item]
        assert [row['id'] for row in d.sweep()] == [second]
        assert d.sweep() == []
        events = [e for e in d.backend.list_events(d.store, d.source)
                  if e['event_type'] == 'auto-blocked-stale']
        assert len(events) == 2

    def test_generic_event_admission_refusal_leaves_source_unchanged(self, stale_item_driver, monkeypatch):
        from sprintctl import contracts
        d = stale_item_driver
        before = d.snapshot(d.store)
        original = contracts.require_generic_event_write_allowed

        def refuse(kind):
            original(kind)
            if kind == 'auto-blocked-stale':
                raise SweepFault('generic admission')

        monkeypatch.setattr(contracts, 'require_generic_event_write_allowed', refuse)
        with pytest.raises(SweepFault, match='generic admission'):
            d.sweep()
        with d.independent() as other:
            assert d.snapshot(other) == before

    def test_competing_sweeps_emit_one_event(self, stale_item_driver, monkeypatch):
        import threading
        d = stale_item_driver
        original = d.backend.list_work_items
        selected = threading.Barrier(2, timeout=10)
        outputs, errors, history = [], [], []
        history_lock = threading.Lock()

        def record(value):
            with history_lock:
                history.append(value)

        def same_selection(store, *args, **kwargs):
            rows = original(store, *args, **kwargs)
            if kwargs.get('sprint_id') == d.source and kwargs.get('status') == 'active':
                record('selected')
                selected.wait()
            return rows

        def worker():
            try:
                with d.independent() as other:
                    record('invoked')
                    result = d.sweep(other)
                    with history_lock:
                        outputs.append(result)
                    record('returned')
            except Exception as exc:
                with history_lock:
                    errors.append(exc)

        monkeypatch.setattr(d.backend, 'list_work_items', same_selection)
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        assert not any(thread.is_alive() for thread in threads), history
        assert not errors, [str(e) for e in errors]
        assert sorted(len(rows) for rows in outputs) == [0, 1]
        assert history.count('selected') == history.count('returned') == 2
        with d.independent() as other:
            assert d.backend.get_work_item(other, d.item)['status'] == 'blocked'
            events = [e for e in d.backend.list_events(other, d.source)
                      if e['event_type'] == 'auto-blocked-stale']
            assert len(events) == 1

    @pytest.mark.parametrize('change', ['refreshed', 'terminal'])
    def test_waiting_writer_rechecks_holder_changes(self, stale_item_driver, change):
        import queue
        import threading
        from sprintctl import decisions
        d = stale_item_driver
        ready = queue.Queue()
        finished = threading.Event()
        outcomes = []

        def waiter():
            try:
                with d.independent() as other:
                    ready.put(d.arm_wait(other))
                    outcomes.append(d.sweep(other))
            except Exception as exc:
                outcomes.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=waiter)
        try:
            with d.backend._stale_item_transaction(d.store) as participant:
                locked = participant.lock_item(d.item)
                thread.start()
                token = ready.get(timeout=5)
                assert d.await_wait(d.store, token)
                assert not finished.is_set()
                if change == 'refreshed':
                    d.update(d.store, 'updated_at=%s', (NOW.strftime('%Y-%m-%dT%H:%M:%SZ'),), commit=False)
                else:
                    decision = decisions.normalize_decision('withdraw', actor='holder', rationale='holder closes source')
                    if hasattr(d.store, 'repo_id'):
                        with d.store.conn.cursor() as cursor:
                            recorded = d.backend._decide_locked(cursor, d.store.repo_id, locked, decision)
                    else:
                        recorded = d.backend._decide_locked(d.store, locked, decision)
                    d.backend._insert_event(d.store, d.source, 'holder', decisions.ITEM_DECIDED_EVENT_TYPE,
                                            work_item_id=d.item,
                                            payload=decisions.decided_event_payload(recorded, None, legacy_remark=False))
        finally:
            if thread.ident is not None:
                thread.join(timeout=15)
        assert not thread.is_alive()
        assert outcomes == [[]], outcomes
        with d.independent() as other:
            row = d.backend.get_work_item(other, d.item)
            assert row['status'] == ('active' if change == 'refreshed' else 'done')
            assert not [e for e in d.backend.list_events(other, d.source)
                        if e['event_type'] == 'auto-blocked-stale']
