"""Actual pre-extraction admission oracle; backend differences stay explicit."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import queue
import threading

import pytest

from sprintctl import releases, reservation


@dataclass
class AdmissionDriver:
    api: object
    handle: object
    item: int
    sprint: int
    independent: object
    sql_fault: object
    lock_admission: object
    arm_wait: object
    await_wait: object
    backend: str

    def reserve(self, **kwargs):
        return self.api.reserve(self.handle, kwargs.pop('item_id', self.item),
            actor=kwargs.pop('actor', ' operator '), session_id=kwargs.pop('session_id', ' session '), **kwargs)

    def snapshot(self, handle=None):
        def read(owner):
            rows = self.api.list_reservations(owner, self.item, active_only=False)
            return {'reservations': sorted([{k: v for k, v in r.items()
                    if k not in ('stale', 'activity_age_seconds')} for r in rows], key=lambda r: r['id']),
                'releases': self.api.list_releases(owner, self.item),
                'events': sorted([e for e in self.api.list_events(owner, self.sprint)
                    if e['work_item_id'] == self.item], key=lambda e: e['id'])}
        if handle is not None:
            return read(handle)
        with self.independent() as other:
            return read(other)

    def assert_idle(self):
        if self.backend == 'sqlite':
            assert not self.handle.in_transaction
        else:
            assert self.handle.conn.info.transaction_status.name == 'IDLE'

    def basis(self):
        if self.backend == 'sqlite':
            return self.api._release_basis(self.handle, self.item)[1]
        with self.handle.conn.cursor() as cur:
            value = self.api._release_basis_locked(cur, self.handle.repo_id, self.item)[1]
        self.handle.conn.commit()
        return value


def retain(record_property, name, observations):
    record_property('state_history', json.dumps({'name': name, 'observations': observations}, default=str))


class AdmissionCases:
    @pytest.mark.parametrize('role,expected', [(None, 'execution'), ('execution', 'execution'),
        (' execute ', 'execution'), ('REVIEW', 'verification'), ('inspect', 'observation'),
        ('coordinate', 'observation'), ('verification', 'verification'), ('observation', 'observation')])
    def test_role_payload_and_native_result_types(self, admission_driver, role, expected):
        d = admission_driver
        row = d.reserve(role=role, correlation_ref=' correlation ')
        d.assert_idle()
        assert (row['role'], row['actor'], row['session_id'], row['correlation_ref']) == (
            expected, ' operator ', ' session ', ' correlation ')
        assert (row['conflict'], row['conflict_severity'], row['conflicting_reservations']) == (False, 'none', [])
        timestamp_type = str if d.backend == 'sqlite' else datetime
        assert type(row['created_at']) is timestamp_type and type(row['last_activity_at']) is timestamp_type
        snap = d.snapshot()
        assert len(snap['releases']) == (expected == 'execution')
        assert [e['event_type'] for e in snap['events']] == ['reservation.reserved']
        assert json.loads(snap['events'][0]['payload']) == {
            'reservation_id': row['id'], 'session_id': ' session ', 'role': expected,
            'correlation_ref': ' correlation ', 'interrupt_existing': False,
            'conflicting_reservation_ids': [], 'release_digest': row['release_digest']}

    @pytest.mark.parametrize('role', ['execution', 'verification', 'observation'])
    def test_ordered_overlap_and_takeover_of_execution_only(self, admission_driver, role):
        d = admission_driver
        original = [d.reserve(role=r) for r in ('execution', 'verification', 'execution', 'observation')]
        before = d.snapshot()
        result = d.reserve(role=role, interrupt_existing=True)
        d.assert_idle()
        after = d.snapshot()
        assert [r['id'] for r in result['conflicting_reservations']] == [original[1]['id'], original[3]['id']]
        assert result['conflict_severity'] == 'informational'
        states = {r['id']: r for r in after['reservations']}
        for old in (original[0], original[2]):
            assert states[old['id']]['state'] == 'interrupted'
            assert states[old['id']]['interruption_reason'] == 'interrupted by  operator  ( session )'
        new_events = after['events'][len(before['events']):]
        assert [e['event_type'] for e in new_events] == ['reservation.interrupted'] * 2 + ['reservation.reserved']
        assert [json.loads(e['payload']) for e in new_events[:2]] == [
            {'reservation_id': old['id'], 'reason': 'explicit-takeover', 'replacement_id': result['id']}
            for old in (original[0], original[2])]
        assert json.loads(new_events[-1]['payload'])['conflicting_reservation_ids'] == [original[1]['id'], original[3]['id']]
        assert result['release_digest'] == (original[0]['release_digest'] if role == 'execution' else None)

    def test_input_rows_contract_and_repeated_unkeyed_calls_are_not_mutated_or_deduplicated(self, admission_driver, monkeypatch):
        d = admission_driver
        first = d.reserve()
        contract = {'review_required': False, 'evidence_obligations': ['review']}
        untouched = deepcopy(contract)
        captured = []
        name = '_release_basis' if d.backend == 'sqlite' else '_release_basis_locked'
        original = getattr(d.api, name)
        def basis(*args, **kwargs):
            value = original(*args, **kwargs)
            captured.append((value[0], deepcopy(value[0])))
            return value
        monkeypatch.setattr(d.api, name, basis)
        original_event = d.api._reservation_event_in_transaction
        def event(*args, **kwargs):
            captured.extend((r, deepcopy(r)) for r in (args[1], args[2]))
            return original_event(*args, **kwargs)
        monkeypatch.setattr(d.api, '_reservation_event_in_transaction', event)
        with monkeypatch.context() as capture:
            annotate = reservation.annotate_conflicts
            def conflicts(row, others):
                captured.extend((r, deepcopy(r)) for r in [row, *others])
                return annotate(row, others)
            capture.setattr(reservation, 'annotate_conflicts', conflicts)
            second = d.reserve(acceptance_contract=contract)
        assert contract == untouched and all(row == expected for row, expected in captured)
        third = d.reserve(acceptance_contract=contract)
        d.assert_idle()
        assert len({first['id'], second['id'], third['id']}) == 3
        assert second['release_digest'] == third['release_digest'] != first['release_digest']
        assert [r['id'] for r in third['conflicting_reservations']] == [first['id'], second['id']]
        assert third['conflict_severity'] == 'warning'
        assert len(d.snapshot()['releases']) == 2

    def test_direct_api_preserves_truthy_takeover_flag_in_payload(self, admission_driver):
        d = admission_driver
        original = d.reserve()
        replacement = d.reserve(role='observation', interrupt_existing='explicit')
        d.assert_idle()
        after = d.snapshot()
        assert after['reservations'][0]['id'] == original['id']
        assert after['reservations'][0]['state'] == 'interrupted'
        payload = json.loads(after['events'][-1]['payload'])
        assert payload['interrupt_existing'] == 'explicit'
        assert payload['reservation_id'] == replacement['id']

    @pytest.mark.parametrize('role', ['verification', 'observation'])
    @pytest.mark.parametrize('explicit_basis', [False, True])
    def test_nonexecution_basis_phase_preserves_backend_difference(self, admission_driver, monkeypatch, role, explicit_basis):
        d = admission_driver
        revision = d.basis()
        name = '_release_basis' if d.backend == 'sqlite' else '_release_basis_locked'
        original = getattr(d.api, name)
        calls = []
        def basis(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(d.api, name, basis)
        result = d.reserve(role=role, expected_revision=revision if explicit_basis else None)
        d.assert_idle()
        assert len(calls) == int(explicit_basis or d.backend == 'pg')
        assert result['release_digest'] is None and d.snapshot()['releases'] == []

    @pytest.mark.parametrize('role', ['execution', 'observation'])
    def test_stale_basis_does_not_interrupt_freeze_or_insert(self, admission_driver, role):
        d = admission_driver
        d.reserve()
        revision = d.basis()
        item, edit_revision = d.api.get_work_item_with_edit_revision(d.handle, d.item)
        d.api.update_work_item_description(d.handle, d.item, 'new actual description', expected_revision=edit_revision, actor='editor')
        before = d.snapshot()
        with pytest.raises(releases.StaleReleaseBasis):
            d.reserve(role=role, expected_revision=revision, interrupt_existing=True)
        d.assert_idle()
        assert d.snapshot() == before

    @pytest.mark.parametrize('stage,expected', [('role', 'invalid reservation role'),
        ('basis', 'valid item revision'), ('contract', 'evidence_obligations')])
    def test_validation_order_before_clock(self, admission_driver, monkeypatch, stage, expected):
        d = admission_driver
        def unexpected_clock():
            pytest.fail('validation reached reservation clock')
        monkeypatch.setattr(reservation, 'now_text', unexpected_clock)
        with pytest.raises(ValueError, match=expected):
            d.reserve(role='invalid' if stage == 'role' else 'execution',
                expected_revision='bad' if stage in ('role', 'basis') else None,
                acceptance_contract={'evidence_obligations': 'bad'})
        d.assert_idle()
        assert d.snapshot()['reservations'] == []

    @pytest.mark.parametrize('operation', ['INSERT', 'UPDATE'])
    @pytest.mark.parametrize('when', ['BEFORE', 'AFTER'])
    def test_actual_sql_fault_rolls_back_before_fixture_cleanup(self, admission_driver, record_property, operation, when):
        d = admission_driver
        d.reserve()
        before = d.snapshot()
        with d.sql_fault(operation, when):
            with pytest.raises(Exception, match='injected admission SQL fault'):
                d.reserve(interrupt_existing=True)
            d.assert_idle()
            pending = d.snapshot(d.handle)
            durable = d.snapshot()
            assert pending == durable == before
            retain(record_property, f'{d.backend}-{when}-{operation}', [{'owner': pending, 'durable': durable, 'owner_idle_before_cleanup': True}])
        d.reserve(interrupt_existing=True)

    @pytest.mark.parametrize('phase', ['display', 'annotation', 'reserved-event'])
    def test_after_actual_effect_fault_rolls_back_and_retries(self, admission_driver, monkeypatch, record_property, phase):
        d = admission_driver
        d.reserve()
        before = d.snapshot()
        with monkeypatch.context() as fault:
            if phase == 'reserved-event':
                original = d.api._reservation_event_in_transaction
                def fail(*args, **kwargs):
                    original(*args, **kwargs)
                    if args[3] == 'reservation.reserved':
                        raise RuntimeError('injected admission result fault')
                fault.setattr(d.api, '_reservation_event_in_transaction', fail)
            else:
                name = 'display' if phase == 'display' else 'annotate_conflicts'
                original = getattr(reservation, name)
                def fail(*args, **kwargs):
                    original(*args, **kwargs)
                    raise RuntimeError('injected admission result fault')
                fault.setattr(reservation, name, fail)
            with pytest.raises(RuntimeError, match='injected admission result fault'):
                d.reserve(interrupt_existing=True)
        d.assert_idle()
        assert d.snapshot(d.handle) == before and d.snapshot() == before
        retain(record_property, f'{d.backend}-{phase}', [{'rolled_back_before_cleanup': True, 'durable': before}])
        d.reserve(interrupt_existing=True)

    def test_one_prelock_timestamp_survives_actual_writer_wait(self, admission_driver, monkeypatch, record_property):
        d = admission_driver
        fixed = '2020-01-02T03:04:05Z'
        clock_seen = threading.Event()
        calls, results, errors = [], [], []
        def clock():
            calls.append(fixed)
            clock_seen.set()
            return fixed
        monkeypatch.setattr(reservation, 'now_text', clock)
        ready = queue.Queue()
        with d.independent() as blocker:
            d.lock_admission(blocker)
            def run():
                try:
                    # SQLite connections belong to their creating thread.
                    with d.independent() as contender:
                        ready.put(d.arm_wait(contender))
                        results.append(d.api.reserve(contender, d.item, actor='waiter', session_id='waiter'))
                except BaseException as exc:
                    errors.append(exc)
            worker = threading.Thread(target=run)
            worker.start()
            try:
                assert clock_seen.wait(5)
                signal = ready.get(timeout=5)
                assert d.await_wait(blocker, signal)
                assert worker.is_alive() and results == []
            finally:
                (blocker if d.backend == 'sqlite' else blocker.conn).rollback()
                worker.join(10)
            assert not worker.is_alive() and errors == []
        assert calls == [fixed]
        result = results[0]
        assert reservation.parse_time(result['created_at']) == reservation.parse_time(fixed)
        assert result['last_activity_at'] == result['created_at']
        retain(record_property, f'{d.backend}-captured-clock-writer-wait', [{'single_prelock_clock': fixed,
            'wait_observation': 'SQLite BEGIN attempt' if d.backend == 'sqlite' else 'PG actual blocking PID',
            'result': result}])
