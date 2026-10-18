"""Coordinator-authored actual-backend examples, before keyed workflow extraction."""
from copy import deepcopy
from dataclasses import replace
import json
import threading
import time
from uuid import uuid4

import pytest
from sprintctl import db, decisions


def decide(d, iid, kind='revise', **kwargs):
    return d.backend.record_decision_keyed(d.store, iid, kind, actor='reviewer', **kwargs)


def idle(d):
    # End only test observation transactions, before arranging a new caller.
    if d.is_pg:
        d.store.conn.rollback()


def fresh_events(before, after):
    prior = {e['id'] for e in before['events']}
    return sorted((e for e in after['events'] if e['id'] not in prior), key=lambda e: e['id'])


class KeyedDecisionCases:
    def test_legacy_remark_is_once_preserves_close_time_and_replays(self, decision_driver):
        d = decision_driver
        if d.is_pg:
            from tests.pg.test_decisions import _legacy_item
        else:
            from tests.test_decisions import _legacy_item
        sid, tid, iid = _legacy_item(d.store, 'done')
        d = replace(d, sprint=sid, track=tid)
        before = d.snapshot(iid)
        key = uuid4().hex
        args = dict(rationale='historical closure', evidence_digests=['ab'*32], idempotency_key=key)
        first, replayed = decide(d, iid, 'withdraw', **args)
        assert replayed is False
        after = d.snapshot(iid)
        assert after['item']['updated_at'] == before['item']['updated_at']
        [event] = fresh_events(before, after)
        assert json.loads(event['payload'])['legacy_remark'] is True
        second, replayed = decide(d, iid, 'withdraw', **args)
        assert replayed is True and second == first
        with pytest.raises(db.InvalidTransition, match='terminal'):
            decide(d, iid, 'withdraw', **{**args, 'idempotency_key': uuid4().hex})
        with d.independent() as other: assert d.snapshot(iid, other) == after

    @pytest.mark.parametrize('status', ('pending', 'active', 'blocked'))
    @pytest.mark.parametrize('kind,resolution', [('accept', 'accepted'), ('reject', 'rejected'),
        ('withdraw', 'withdrawn'), ('supersede', 'superseded'), ('revise', None)])
    def test_authored_kind_matrix_and_canonical_event(self, decision_driver, status, kind, resolution):
        d = decision_driver
        iid = d.new_item(status)
        successor = d.new_item()
        before = d.snapshot(iid)
        options = {'rationale': 'explicit reason', 'idempotency_key': uuid4().hex,
                   'evidence_digests': ['ab'*32, 'cd'*32, 'ab'*32]}
        if kind == 'supersede':
            options['superseded_by_item_id'] = successor
        if kind == 'accept' and status != 'active':
            with pytest.raises(db.InvalidTransition, match='requires an active item'):
                decide(d, iid, kind, **options)
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            return
        value, replay = decide(d, iid, kind, **options)
        assert type(value) is dict and replay is False
        assert value['kind'] == kind and value['actor'] == 'reviewer'
        assert value['evidence_digests'] == ['ab'*32, 'cd'*32]
        assert type(value['id']) is int and type(value['created_at']) is str
        with d.independent() as other:
            after = d.snapshot(iid, other)
        assert after['decisions'] == [value]
        assert after['item']['status'] == ('done' if resolution else status)
        assert after['item']['resolution'] == resolution
        assert after['item']['terminal_decision_id'] == (value['id'] if resolution else None)
        [event] = fresh_events(before, after)
        assert event['event_type'] == 'item-decided' and event['actor'] == 'reviewer'
        assert event['source_type'] == 'actor' and event['work_item_id'] == iid
        assert json.loads(event['payload']) == {'decision_id': value['id'], 'kind': kind,
            'resolution': resolution, 'release_digest': None, 'idempotency_key': options['idempotency_key']}

    def test_unkeyed_revise_is_two_legitimate_writes(self, decision_driver):
        d = decision_driver
        iid = d.new_item('active')
        a, replay_a = decide(d, iid)
        b, replay_b = decide(d, iid)
        assert replay_a is replay_b is False and a['id'] != b['id']
        assert d.backend.list_decisions(d.store, iid) == [a, b]
        events = d.backend.list_events(d.store, d.sprint)
        assert len(events) == 2 and all('idempotency_key' not in json.loads(e['payload']) for e in events)

    @pytest.mark.parametrize('field', ('kind', 'actor', 'rationale', 'evidence', 'successor', 'release', 'item'))
    def test_each_replay_identity_mismatch_refuses_without_new_effect(self, decision_driver, field):
        d = decision_driver
        iid, successor, other_item = (d.new_item('active') for _ in range(3))
        key = uuid4().hex
        args = dict(actor='reviewer', rationale='reason', evidence_digests=['ab'*32],
                    superseded_by_item_id=successor, idempotency_key=key)
        d.backend.record_decision_keyed(d.store, iid, 'supersede', **args)
        before = d.snapshot(iid)
        target, kind = iid, 'supersede'
        if field == 'kind':
            kind = 'revise'
            args['superseded_by_item_id'] = None
        elif field == 'actor': args['actor'] = 'different'
        elif field == 'rationale': args['rationale'] = 'different'
        elif field == 'evidence': args['evidence_digests'] = ['cd'*32]
        elif field == 'successor': args['superseded_by_item_id'] = other_item
        elif field == 'release': args['release_digest'] = 'ef'*32
        elif field == 'item': target = other_item
        with pytest.raises(decisions.IdempotencyConflict, match='already used'):
            d.backend.record_decision_keyed(d.store, target, kind, **args)
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
            assert d.backend.list_decisions(other, other_item) == []

    def test_replay_precedes_stale_basis_and_preserves_original_release(self, decision_driver):
        d = decision_driver
        iid = d.new_item('active')
        reservation = d.backend.reserve(d.store, iid, actor='fixture', session_id='fixture')
        digest = reservation['release_digest']
        key = uuid4().hex
        first, replay = decide(d, iid, 'accept', idempotency_key=key, release_digest=digest)
        before = d.snapshot(iid)
        stale = db.item_status_revision({**before['item'], 'status': 'pending'})
        again, replay = decide(d, iid, 'accept', idempotency_key=key, expected_revision=stale)
        assert replay is True and again == first and again['release_digest'] == digest
        with d.independent() as other:
            assert d.snapshot(iid, other) == before
        with pytest.raises(ValueError, match='valid item status revision'):
            decide(d, iid, 'accept', idempotency_key=key, expected_revision='malformed')

    @pytest.mark.parametrize('case,error,fragment', [
        ('normalize', ValueError, 'decision kind'), ('key', ValueError, 'idempotency key'),
        ('basis-syntax', ValueError, 'valid item status revision'), ('missing', ValueError, 'not found'),
        ('replay-before-stale', decisions.IdempotencyConflict, 'already used'),
        ('stale-before-admission', db.StatusConflict, 'status revision mismatch'),
        ('admission-before-successor', db.InvalidTransition, 'requires an active item'),
        ('self-before-release', ValueError, 'cannot supersede itself'),
        ('missing-successor-before-release', ValueError, 'Superseding item')])
    def test_error_precedence_is_observed_through_real_owner(self, decision_driver, case, error, fragment):
        d = decision_driver
        iid = d.new_item('pending')
        missing = 2**60
        key = uuid4().hex
        decide(d, iid, idempotency_key=key)
        before = d.snapshot(iid)
        target, kind = iid, 'accept'
        args = dict(idempotency_key=key, expected_revision='bad')
        if case == 'normalize': kind = 'invalid'
        elif case == 'key': args['idempotency_key'] = ''
        elif case == 'basis-syntax': pass
        else:
            args['expected_revision'] = db.item_status_revision({**before['item'], 'status': 'blocked'})
            if case == 'missing': target = missing
            elif case == 'replay-before-stale': pass
            else:
                args['idempotency_key'] = uuid4().hex
                if case != 'stale-before-admission': args['expected_revision'] = None
                args['release_digest'] = 'ab'*32
                if case == 'self-before-release':
                    kind = 'supersede';args.update(rationale='why', superseded_by_item_id=iid)
                elif case == 'missing-successor-before-release':
                    kind = 'supersede';args.update(rationale='why', superseded_by_item_id=missing)
        with pytest.raises(error, match=fragment):
            decide(d, target, kind, **args)
        with d.independent() as other:
            assert d.snapshot(iid, other) == before

    @pytest.mark.parametrize('fault', ('decision', 'payload', 'event'))
    def test_after_real_effect_fault_rollback_and_same_key_retry(self, decision_driver, fault, monkeypatch, record_property):
        d = decision_driver
        iid = d.new_item('active')
        before = d.snapshot(iid)
        key = uuid4().hex
        captured = []
        def capture():
            captured.append({'transaction': d.transaction(), 'pending': d.snapshot(iid)})
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            raise RuntimeError('actual keyed effect fault')
        with monkeypatch.context() as m:
            owner, name = (decisions, 'decided_event_payload') if fault == 'payload' else (d.backend, '_decide_locked' if fault == 'decision' else '_insert_event')
            original = getattr(owner, name)
            def fail(*args, **kwargs):
                value = original(*args, **kwargs)
                capture()
                return value
            m.setattr(owner, name, fail)
            with pytest.raises(RuntimeError, match='actual keyed effect fault'):
                decide(d, iid, 'accept', idempotency_key=key)
        assert len(captured) == 1 and captured[0]['transaction'] in ('ACTIVE', 'INTRANS')
        assert captured[0]['pending']['item']['status'] == 'done'
        assert len(captured[0]['pending']['decisions']) == 1
        assert len(fresh_events(before, captured[0]['pending'])) == (1 if fault == 'event' else 0)
        assert d.transaction() == 'IDLE'
        with d.independent() as other: assert d.snapshot(iid, other) == before
        recorded, replayed = decide(d, iid, 'accept', idempotency_key=key)
        assert replayed is False
        with d.independent() as other: after = d.snapshot(iid, other)
        assert after['decisions'] == [recorded] and len(fresh_events(before, after)) == 1
        record_property('state_history', json.dumps({'history': 'keyed-fault-'+fault,
            'backend': d.backend.__name__, 'before': before, 'inside_fault': captured,
            'after_retry': after}, default=str, sort_keys=True))

    @pytest.mark.parametrize('case', ('fresh', 'replay', 'handler', 'pretry'))
    def test_existing_implicit_caller_policy_before_cleanup(self, decision_driver, case):
        d = decision_driver
        iid = d.new_item('active')
        key = uuid4().hex
        if case == 'replay': decide(d, iid, idempotency_key=key)
        before = d.snapshot(iid)
        original_goal = d.backend.get_sprint(d.store, d.sprint)['goal']
        idle(d)
        if d.is_pg:
            d.store.conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',
                ('pending caller', d.store.repo_id, d.sprint))
        else:
            d.store.execute('UPDATE sprint SET goal=? WHERE id=?', ('pending caller', d.sprint))
        if case == 'handler':
            with pytest.raises(db.StatusConflict):
                decide(d, iid, idempotency_key=key, expected_revision=db.item_status_revision({**before['item'], 'status': 'pending'}))
        elif case == 'pretry':
            with pytest.raises(ValueError): decide(d, iid, idempotency_key='')
        else:
            _, replayed = decide(d, iid, idempotency_key=key)
            assert replayed is (case == 'replay')
        assert d.transaction() == (('INTRANS' if d.is_pg else 'ACTIVE') if case == 'pretry' else 'IDLE')
        expected_primary = 'pending caller' if case in ('fresh', 'pretry') else original_goal
        assert d.backend.get_sprint(d.store, d.sprint)['goal'] == expected_primary
        with d.independent() as other:
            assert d.backend.get_sprint(other, d.sprint)['goal'] == ('pending caller' if case == 'fresh' else original_goal)
        (d.store.conn if d.is_pg else d.store).rollback()

    def test_native_decision_object_and_normalized_input_are_not_mutated(self, decision_driver, monkeypatch):
        d = decision_driver
        iid = d.new_item('active')
        seen = []
        original = d.backend._decide_locked
        def spy(*args, **kwargs):
            incoming = args[-1]
            before = deepcopy(incoming)
            recorded = original(*args, **kwargs)
            seen.append((incoming, before, recorded, deepcopy(recorded)))
            return recorded
        monkeypatch.setattr(d.backend, '_decide_locked', spy)
        value, replayed = decide(d, iid, idempotency_key=uuid4().hex, evidence_digests=['ab'*32])
        assert replayed is False and len(seen) == 1
        incoming, before, recorded, recorded_before = seen[0]
        assert incoming == before and value is recorded and value == recorded_before
        assert {k:type(v) for k,v in value.items()} == {k:type(v) for k,v in recorded_before.items()}

    def test_replay_returns_exact_lookup_object_without_fresh_callbacks(self, decision_driver, monkeypatch):
        d = decision_driver
        iid = d.new_item('active')
        key = uuid4().hex
        decide(d, iid, idempotency_key=key)
        lookup_name = '_keyed_decision_locked' if d.is_pg else '_keyed_decision'
        lookup = getattr(d.backend, lookup_name)
        seen = []
        def spy(*args, **kwargs):
            stored = lookup(*args, **kwargs)
            seen.append((stored, deepcopy(stored)))
            return stored
        def forbidden(*args, **kwargs):
            pytest.fail('replay evaluated a fresh-only callback')
        monkeypatch.setattr(d.backend, lookup_name, spy)
        monkeypatch.setattr(d.backend, '_decide_locked', forbidden)
        monkeypatch.setattr(d.backend, 'item_status_revision', forbidden)
        monkeypatch.setattr(decisions, 'is_legacy_remark_target', forbidden)
        monkeypatch.setattr(decisions, 'decided_event_payload', forbidden)
        value, replayed = decide(d, iid, idempotency_key=key)
        assert replayed is True and len(seen) == 1
        stored, before = seen[0]
        assert value is stored and value == before
        assert {k:type(v) for k,v in value.items()} == {k:type(v) for k,v in before.items()}

    @pytest.mark.parametrize('different_items', (False, True))
    def test_two_actual_actors_share_one_key(self, decision_driver, different_items, monkeypatch, record_property):
        d = decision_driver
        items = [d.new_item('active'), d.new_item('active')]
        key = uuid4().hex
        held, release, attempted = threading.Event(), threading.Event(), threading.Event()
        results, history, errors, contender_pid, holder_pid = [None, None], [], [], [], []
        original = d.backend._decide_locked
        def hold(*args, **kwargs):
            value = original(*args, **kwargs)
            if threading.current_thread().name == 'keyed-holder':
                held.set()
                assert release.wait(10), 'holder release timed out'
            return value
        monkeypatch.setattr(d.backend, '_decide_locked', hold)
        def actor(index):
            try:
                with d.independent() as store:
                    if not index and d.is_pg:
                        holder_pid.append(store.conn.info.backend_pid)
                    if index:
                        if d.is_pg:
                            contender_pid.append(store.conn.info.backend_pid)
                        else:
                            store.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
                    history.append({'actor': index, 'phase': 'invoke'})
                    try:
                        result = d.backend.record_decision_keyed(store, items[index if different_items else 0],
                            'revise', actor='reviewer', idempotency_key=key)
                        results[index] = result
                        history.append({'actor': index, 'phase': 'complete', 'decision_id': result[0]['id'], 'replayed': result[1]})
                    except decisions.IdempotencyConflict:
                        results[index] = 'conflict'
                        history.append({'actor': index, 'phase': 'complete', 'error': 'IdempotencyConflict'})
            except BaseException as exc:
                errors.append(exc)
        holder = threading.Thread(target=actor, args=(0,), name='keyed-holder')
        contender = threading.Thread(target=actor, args=(1,), name='keyed-contender')
        holder.start()
        try:
            assert held.wait(5), errors
            contender.start()
            if d.is_pg:
                deadline = time.monotonic() + 5
                blocked = False
                with d.independent() as observer:
                    while time.monotonic() < deadline:
                        if contender_pid:
                            blockers = observer.conn.execute('SELECT pg_blocking_pids(%s) AS pids', (contender_pid[0],)).fetchone()['pids']
                            if holder_pid and holder_pid[0] in blockers:
                                blocked = True
                                break
                        if results[1] is not None or errors: break
                        time.sleep(.005)
                assert blocked, (results, errors)
            else:
                assert attempted.wait(5), errors
            assert contender.is_alive() and results == [None, None] and not errors
            history.append({'phase': 'contender-waits-before-holder-commit',
                'observation': 'PG blocking PID' if d.is_pg else 'SQLite attempted BEGIN IMMEDIATE'})
        finally:
            release.set()
            holder.join(15)
            if contender.ident is not None: contender.join(15)
        assert not holder.is_alive() and not contender.is_alive() and not errors
        assert all(r is not None for r in results)
        if different_items:
            assert results.count('conflict') == 1
        else:
            assert sorted(r[1] for r in results) == [False, True] and results[0][0] == results[1][0]
        with d.independent() as observer:
            rows = [d.backend.list_decisions(observer, iid) for iid in items]
            events = d.backend.list_events(observer, d.sprint)
        assert sum(map(len, rows)) == 1 and len(events) == 1
        record_property('state_history', json.dumps({'history': 'two-key-actors-'+str(different_items),
            'backend': d.backend.__name__, 'operations': history, 'decisions': rows, 'events': events}, default=str, sort_keys=True))

    def test_discard_committed_response_reconnect_and_replay(self, decision_driver, record_property):
        d = decision_driver
        iid = d.new_item('active')
        key = uuid4().hex
        with d.independent() as first:
            d.backend.record_decision_keyed(first, iid, 'accept', actor='reader', idempotency_key=key)
        with d.independent() as second:
            value, replayed = d.backend.record_decision_keyed(second, iid, 'accept', actor='reader', idempotency_key=key)
            assert replayed is True
            assert d.backend.list_decisions(second, iid) == [value]
            assert len(d.backend.list_events(second, d.sprint)) == 1
        record_property('state_history', json.dumps({'history': 'response-discard-reconnect',
            'backend': d.backend.__name__, 'replayed': replayed, 'decision': value}, sort_keys=True))
