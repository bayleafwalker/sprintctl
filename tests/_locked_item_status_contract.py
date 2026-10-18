"""Coordinator-owned compatibility examples, authored before status extraction."""
from dataclasses import dataclass
import json

import pytest
from sprintctl import db


# Frozen public transition contract, rather than consulting the implementation's
# transition map to generate its own expected answers.
TRANSITIONS = {
    'pending': {'active'},
    'active': {'done', 'blocked', 'pending'},
    'blocked': {'active', 'pending'},
    'done': set(),
}
STATUSES = ('pending', 'active', 'blocked', 'done')


@dataclass
class StatusDriver:
    backend: object
    store: object
    sprint: int
    track: int
    independent: object
    adapter_class: object
    is_pg: bool

    def new_item(self, status='pending', title='status oracle'):
        iid = self.backend.create_work_item(self.store, self.sprint, self.track, title,
                                            description='preserved scope', assignee='owner', priority=3)
        if status != 'pending':
            self.backend.set_work_item_status(self.store, iid, 'active')
        if status == 'blocked':
            self.backend.set_work_item_status(self.store, iid, 'blocked')
        elif status == 'done':
            self.backend.record_decision(self.store, iid, 'accept', actor='fixture', rationale='existing terminal')
        return iid

    def snapshot(self, iid, store=None):
        store = self.store if store is None else store
        reservations = self.backend.list_reservations(store, iid, active_only=False)
        # Compare stored state, not display fields recomputed from wall time.
        for row in reservations:
            assert type(row['activity_age_seconds']) is int and type(row['stale']) is bool
        stored_reservations = [{k: v for k, v in row.items()
                                if k not in ('activity_age_seconds', 'stale')}
                               for row in reservations]
        result = {'item': self.backend.get_work_item(store, iid),
                  'events': self.backend.list_events(store, self.sprint),
                  'reservations': stored_reservations,
                  'decisions': self.backend.list_decisions(store, iid),
                  'releases': self.backend.list_releases(store, iid)}
        result['native_types'] = {
            name: ([{k: type(v).__name__ for k, v in row.items()} for row in value]
                   if isinstance(value, list) else {k: type(v).__name__ for k, v in value.items()})
            for name, value in result.items()}
        return result

    def transaction(self):
        if self.is_pg:
            return self.store.conn.info.transaction_status.name
        return 'ACTIVE' if self.store.in_transaction else 'IDLE'

    def transition(self, iid, target, **kwargs):
        return self.backend.set_work_item_status(self.store, iid, target, **kwargs)


class LockedStatusCases:
    @pytest.mark.parametrize('source', STATUSES)
    @pytest.mark.parametrize('target', STATUSES)
    def test_public_transition_matrix(self, status_driver, source, target):
        d = status_driver
        iid = d.new_item(source)
        before = d.snapshot(iid)
        revision = db.item_status_revision(before['item'])
        kwargs = {'actor': 'matrix', 'expected_revision': revision}
        if target == 'pending':
            kwargs['reason'] = 'partial'
        if target not in TRANSITIONS[source]:
            with pytest.raises(db.InvalidTransition, match=f'cannot transition {source} -> {target}'):
                d.transition(iid, target, **kwargs)
            assert d.transaction() == 'IDLE'
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            return
        assert d.transition(iid, target, **kwargs) is None
        with d.independent() as other:
            after = d.snapshot(iid, other)
        assert after['item']['status'] == target
        for key, value in before['item'].items():
            if key not in ('status', 'updated_at', 'resolution', 'terminal_decision_id'):
                assert after['item'][key] == value
        assert after['releases'] == before['releases']
        if target == 'done':
            [decision] = after['decisions']
            assert decision['kind'] == 'accept' and decision['actor'] == 'matrix'
            assert after['item']['terminal_decision_id'] == decision['id']
            assert after['item']['resolution'] == 'accepted'
            assert after['events'] == before['events']
        else:
            assert after['decisions'] == before['decisions']
            assert after['item']['resolution'] is None
            assert after['item']['terminal_decision_id'] is None
            if target == 'pending':
                [event] = after['events']
                expected = {'reason': 'partial', 'previous_status': source,
                            'released_reservation_ids': []}
                if d.is_pg:
                    expected['released_lease_ids'] = []
                assert event['event_type'] == 'item-released' and event['source_type'] == 'system'
                assert event['work_item_id'] == iid and event['actor'] == 'matrix'
                assert json.loads(event['payload']) == expected
            else:
                assert after['events'] == before['events']

    @pytest.mark.parametrize('reason', ('rework', 'partial', 'abandoned'))
    @pytest.mark.parametrize('session', ('mine', 'foreign', None))
    def test_release_session_membership_payload_and_order(self, status_driver, reason, session):
        d = status_driver
        iid = d.new_item('active')
        mine = [d.backend.reserve(d.store, iid, actor='shared-name', session_id='mine', role=role)
                for role in ('execution', 'verification')]
        foreign = d.backend.reserve(d.store, iid, actor='shared-name', session_id='foreign')
        before = d.snapshot(iid)
        assert d.transition(iid, 'pending', actor=None, reason=reason, session_id=session) is None
        with d.independent() as other:
            after = d.snapshot(iid, other)
        expected_ids = {r['id'] for r in mine} if session == 'mine' else {foreign['id']} if session == 'foreign' else set()
        for row in after['reservations']:
            assert row['state'] == ('released' if row['id'] in expected_ids else 'active')
            assert row['release_digest'] == next(r for r in before['reservations'] if r['id'] == row['id'])['release_digest']
        # Public list_events orders by timestamp only, so equal timestamps do
        # not promise append position. Observe membership and local insertion
        # sequence by native event IDs, without changing that public ordering.
        old_ids = {event['id'] for event in before['events']}
        new_events = sorted((event for event in after['events'] if event['id'] not in old_ids),
                            key=lambda event: event['id'])
        assert len(new_events) == len(expected_ids) + 1
        assert {json.loads(e['payload'])['reservation_id'] for e in new_events[:-1]} == expected_ids
        assert all(e['event_type'] == 'reservation.released' for e in new_events[:-1])
        final = new_events[-1]
        assert final['event_type'] == 'item-released' and final['actor'] == 'sprintctl'
        payload = json.loads(final['payload'])
        assert set(payload['released_reservation_ids']) == expected_ids
        assert payload['reason'] == reason and payload['previous_status'] == 'active'
        if d.is_pg:
            assert payload['released_lease_ids'] == []
        else:
            assert 'released_lease_ids' not in payload
        assert after['releases'] == before['releases'] and after['decisions'] == before['decisions']

    @pytest.mark.parametrize('fault', ('status', 'decision', 'reservation.released', 'item-released'))
    def test_after_real_effect_fault_rolls_back_before_cleanup(self, status_driver, fault, monkeypatch, record_property):
        d = status_driver
        iid = d.new_item('active')
        d.backend.reserve(d.store, iid, actor='worker', session_id='mine')
        before = d.snapshot(iid)
        captured = []
        target = 'done' if fault == 'decision' else 'blocked' if fault == 'status' else 'pending'
        options = {'actor': 'fault oracle'}
        if target == 'pending':
            options.update(reason='rework', session_id='mine')

        def observe_then_fail():
            pending = d.snapshot(iid)
            with d.independent() as other:
                durable = d.snapshot(iid, other)
            captured.append({'transaction': d.transaction(), 'pending': pending, 'durable': durable})
            assert pending['item']['status'] == target
            assert durable == before
            raise RuntimeError('after actual status effect')

        with monkeypatch.context() as patched:
            if fault == 'status':
                original = d.adapter_class.execute
                def execute(adapter, sql, params):
                    result = original(adapter, sql, params)
                    if sql.startswith('UPDATE work_item SET status'):
                        observe_then_fail()
                    return result
                patched.setattr(d.adapter_class, 'execute', execute)
            elif fault == 'decision':
                original = d.backend._decide_locked
                def decide(*args, **kwargs):
                    result = original(*args, **kwargs)
                    observe_then_fail()
                    return result
                patched.setattr(d.backend, '_decide_locked', decide)
            else:
                original = d.backend._insert_event
                def insert(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if args[3] == fault:
                        observe_then_fail()
                    return result
                patched.setattr(d.backend, '_insert_event', insert)
            with pytest.raises(RuntimeError, match='after actual status effect'):
                d.transition(iid, target, **options)
        assert len(captured) == 1
        assert captured[0]['transaction'] in ('ACTIVE', 'INTRANS')
        assert d.transaction() == 'IDLE'
        with d.independent() as other:
            rolled_back = d.snapshot(iid, other)
        assert rolled_back == before
        assert d.transition(iid, target, **options) is None
        with d.independent() as other:
            retried = d.snapshot(iid, other)
        assert retried['item']['status'] == target
        record_property('state_history', json.dumps({'history': 'after-real-effect-'+fault,
            'backend': d.backend.__name__, 'before': before, 'inside_fault': captured,
            'after_handler': rolled_back, 'after_retry': retried}, default=str, sort_keys=True))

    @pytest.mark.parametrize('case,error,fragment', [
        ('bad-revision-before-missing', ValueError, 'valid item status revision'),
        ('missing-before-stale', ValueError, 'not found'),
        ('stale-before-transition', db.StatusConflict, 'status revision mismatch'),
        ('transition-before-reason', db.InvalidTransition, 'cannot transition pending -> done'),
        ('missing-reason', db.InvalidTransition, 'without --reason'),
        ('unknown-reason', db.InvalidTransition, 'without --reason'),
        ('reason-on-nonpending', db.InvalidTransition, '--reason is only accepted'),
    ])
    def test_validation_precedence_preserves_state(self, status_driver, case, error, fragment):
        d = status_driver
        iid = d.new_item('pending' if case == 'transition-before-reason' else 'active')
        before = d.snapshot(iid)
        target_id, target = iid, 'pending'
        options = {'reason': 'rework'}
        if case == 'bad-revision-before-missing':
            target_id = 999999999
            options['expected_revision'] = 'bad'
        elif case == 'missing-before-stale':
            target_id = 999999999
            options['expected_revision'] = db.item_status_revision(before['item'])
        elif case == 'stale-before-transition':
            target = 'active'
            options['expected_revision'] = db.item_status_revision(before['item']).replace('@status:active', '@status:blocked')
        elif case == 'transition-before-reason':
            target = 'done'
            options['actor'] = ' '
        elif case == 'missing-reason':
            options.clear()
        elif case == 'unknown-reason':
            options['reason'] = 'invalid reason'
        else:
            target = 'blocked'
        with pytest.raises(error, match=fragment):
            d.transition(target_id, target, **options)
        with d.independent() as other:
            assert d.snapshot(iid, other) == before

    @pytest.mark.parametrize('blocker_status', ('pending', 'active', 'blocked'))
    def test_activation_preserves_backend_blocker_callback_order(self, status_driver, blocker_status, monkeypatch):
        d = status_driver
        iid = d.new_item()
        first = d.new_item(blocker_status, 'first blocker')
        second = d.new_item(blocker_status, 'second blocker')
        d.backend.add_dep(d.store, first, iid)
        d.backend.add_dep(d.store, second, iid)
        before = d.snapshot(iid)
        original = d.backend.list_deps_blocking
        def permuted(*args, **kwargs):
            rows = original(*args, **kwargs)  # Real scoped read, authored callback order.
            by_id = {row['item_id']: row for row in rows}
            assert set(by_id) == {first, second}
            return [by_id[second], by_id[first]]
        monkeypatch.setattr(d.backend, 'list_deps_blocking', permuted)
        with pytest.raises(db.InvalidTransition) as caught:
            d.transition(iid, 'active')
        assert str(caught.value) == f'cannot transition pending -> active while blockers remain unresolved: {[second, first]}'
        with d.independent() as other:
            assert d.snapshot(iid, other) == before

    def test_all_done_resolutions_resolve_activation_edges(self, status_driver):
        d = status_driver
        iid = d.new_item()
        successor = d.new_item(title='superseding successor')
        for kind in ('accept', 'reject', 'withdraw', 'supersede'):
            blocker = d.new_item('active', 'terminal '+kind)
            options = {'superseded_by_item_id': successor} if kind == 'supersede' else {}
            d.backend.record_decision(d.store, blocker, kind, actor='reviewer', rationale='fixture terminal', **options)
            d.backend.add_dep(d.store, blocker, iid)
        assert d.transition(iid, 'active') is None
        with d.independent() as other:
            assert d.backend.get_work_item(other, iid)['status'] == 'active'

    @pytest.mark.parametrize('actor', (None, '', ' '))
    @pytest.mark.parametrize('target', ('done', 'blocked', 'pending'))
    def test_actor_normalization_only_at_existing_branches(self, status_driver, actor, target):
        d = status_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        options = {'actor': actor}
        if target == 'pending':
            options['reason'] = 'rework'
        if target == 'done' and actor == ' ':
            with pytest.raises(ValueError, match='actor'):
                d.transition(iid, target, **options)
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            return
        assert d.transition(iid, target, **options) is None
        after = d.snapshot(iid)
        if target == 'done':
            assert after['decisions'][-1]['actor'] == 'sprintctl'
        elif target == 'pending':
            [event] = after['events']
            assert event['actor'] == (actor or 'sprintctl')
        else:
            assert after['events'] == before['events']

    def test_stale_owner_private_participant_stays_noncommitting(self, status_driver):
        d = status_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        # End the PG read-only implicit transaction before this independent owner.
        (d.store.conn if d.is_pg else d.store).rollback()
        with pytest.raises(RuntimeError, match='owner abort'):
            with d.backend._stale_item_transaction(d.store) as participant:
                # Existing owner work must survive the helper and roll back
                # with the owner; a premature helper commit cannot hide.
                if d.is_pg:
                    d.store.conn.execute(
                        'UPDATE work_item SET assignee=%s WHERE repo_id=%s AND id=%s',
                        ('outer owner pending', d.store.repo_id, iid))
                else:
                    d.store.execute('UPDATE work_item SET assignee=? WHERE id=?',
                                    ('outer owner pending', iid))
                assert participant.block_item(iid) is None
                pending = d.backend.get_work_item(d.store, iid)
                assert pending['status'] == 'blocked'
                assert pending['assignee'] == 'outer owner pending'
                with d.independent() as other:
                    assert d.snapshot(iid, other) == before
                raise RuntimeError('owner abort')
        with d.independent() as other:
            assert d.snapshot(iid, other) == before

    @pytest.mark.parametrize('case', ('basis', 'reason'))
    def test_basis_and_reason_errors_precede_unresolved_blocker(self, status_driver, case):
        d = status_driver
        iid, blocker = d.new_item(), d.new_item('active', 'blocking item')
        d.backend.add_dep(d.store, blocker, iid)
        before = d.snapshot(iid)
        if case == 'basis':
            options = {'expected_revision': db.item_status_revision(before['item']).replace('@status:pending', '@status:active')}
            error, fragment = db.StatusConflict, 'status revision mismatch'
        else:
            options = {'reason': 'rework'}
            error, fragment = db.InvalidTransition, '--reason is only accepted'
        with pytest.raises(error, match=fragment):
            d.transition(iid, 'active', **options)
        with d.independent() as other:
            assert d.snapshot(iid, other) == before

    def test_release_payload_preserves_backend_result_order(self, status_driver, monkeypatch):
        d = status_driver
        iid = d.new_item('active')
        rows = [d.backend.reserve(d.store, iid, actor='order oracle', session_id='mine', role=role)
                for role in ('execution', 'verification', 'observation')]
        expected = (rows[1]['id'], rows[2]['id'], rows[0]['id'])
        original = d.backend._release_caller_reservations_locked
        def permuted(*args, **kwargs):
            actual = original(*args, **kwargs)  # Real row/event writes remain in this transaction.
            assert set(actual) == set(expected)
            return list(expected)
        monkeypatch.setattr(d.backend, '_release_caller_reservations_locked', permuted)
        assert d.transition(iid, 'pending', reason='partial', session_id='mine') is None
        with d.independent() as other:
            events = d.backend.list_events(other, d.sprint)
        [event] = [e for e in events if e['event_type'] == 'item-released']
        assert json.loads(event['payload'])['released_reservation_ids'] == list(expected)
