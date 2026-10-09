"""Atomic causal admission and historical replay on disposable owner state."""
import copy
from dataclasses import replace

import jsonschema
import pytest

from sprintctl import pg
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.effect_intent import OPERATION_BOUND_PROPOSE as BOUND
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import PG_MARKS
from tests.pg.test_effect_intent import (
    PROPOSER, _items, _run, _propose_args, _reject,
)
from tests.pg.test_native_proposal_intake import counts

pytestmark = PG_MARKS
CONTRACT = next(c for c in WORK_OPERATION_CONTRACTS if c.name == BOUND)


@pytest.fixture
def admitted_basis(store, pg_test_scope):
    store = replace(store, repo_id=pg_test_scope('bound-proposal'))
    ctx = copy.deepcopy(PROPOSER)
    ctx.identity.authorities = ctx.identity.authorities | {'work:write'}
    ctx.idempotency_key = 'bound-reserve-key'
    item, = _items(store)
    run = _run(store, ctx)
    app = WorkApplication.postgres(store)
    revision = pg.item_release_revision(store, item)
    reservation = app.invoke('work.reservation.reserve-v1', {
        'item_id': item, 'actor': ctx.identity.actor, 'session_id': 'bound-session',
        'expected_revision': revision,
    }, ctx)['reservation']
    digest = reservation['release_digest']
    # A precommitted trailer observation; trailer ingestion itself is separately
    # qualified. Admission must read this owner relation, never ingest for us.
    with store.conn.cursor() as cur:
        cur.execute('INSERT INTO release_commit(repo_id,release_digest,commit_sha) VALUES (%s,%s,%s)',
                    (store.repo_id, digest, 'c' * 40))
    store.conn.commit()
    tail = append(store, run)
    args = _propose_args(item, run, 'bound-proposal-key', causal_basis={
        'expected_revision': revision, 'release_digest': digest,
        'reserve_idempotency_key': ctx.idempotency_key, 'commit_sha': 'c' * 40,
        'evidence_tail': {'item_id': tail['item_id'], 'chain_seq': tail['chain_seq'],
                          'entry_digest': pg.evidence_entry_digest(tail)},
    })
    return store, app, ctx, args, reservation


def append(store, run):
    old = pg.evidence_tail(store, run)
    seq = 0 if old is None else old['chain_seq'] + 1
    return pg.append_evidence(
        store, run, idempotency_key=f'evidence-{seq}', request_digest=f"{seq:064x}",
        item_id=f'proof-{seq}', kind='test', ref='local:test', digest='sha256:' + 'a' * 64,
        collector='bound-tests', validity={}, claims=[], provenance={}, chain_seq=seq,
        chain_prev_digest=None if old is None else pg.evidence_entry_digest(old),
    )


def invoke(app, ctx, args):
    jsonschema.validate(args, CONTRACT.input_schema)
    result = app.invoke(BOUND, args, ctx)
    jsonschema.validate(result, CONTRACT.result_schema)
    return result


def test_original_admission_survives_changes_and_current_lifecycle(admitted_basis):
    store, app, ctx, args, reservation = admitted_basis
    first = invoke(app, ctx, args)
    assert first['admission']['causal_basis'] == args['causal_basis']
    assert first['admission']['reservation_id'] == reservation['id']
    assert first['admission']['run_binding'] == app.invoke(
        'work.run.resolve-v1', {'run_id': args['run_id']}, ctx)
    rejected = _reject(store, first['intent'])
    pg.update_work_item_description(store, args['item_id'], 'changed after admission')
    append(store, args['run_id'])
    pg.release_reservation(store, reservation['id'], actor='test')
    replay = invoke(app, ctx, args)
    assert replay['admission'] == first['admission']
    assert replay['intent'] == rejected
    assert counts(store) == {'intents': 1, 'keys': 1}
    changed = copy.deepcopy(args)
    changed['causal_basis']['evidence_tail']['entry_digest'] = 'sha256:' + 'b' * 64
    with pytest.raises(ApplicationRejection) as exc:
        invoke(app, ctx, changed)
    assert exc.value.code == 'idempotency-conflict'


@pytest.mark.parametrize('field,value,code', [
    ('release_digest', 'f' * 64, 'effect-causal-release-mismatch'),
    ('reserve_idempotency_key', 'missing-reserve-key', 'effect-causal-reserve-missing'),
    ('commit_sha', 'd' * 40, 'effect-causal-commit-unbound'),
    ('evidence_tail', {'item_id': 'wrong', 'chain_seq': 0, 'entry_digest': 'sha256:' + 'a' * 64},
     'effect-causal-evidence-head-mismatch'),
])
def test_refusal_commits_neither_proposal_nor_key(admitted_basis, field, value, code):
    store, app, ctx, args, _ = admitted_basis
    bad = copy.deepcopy(args)
    bad['causal_basis'][field] = value
    with pytest.raises(ApplicationRejection) as exc:
        invoke(app, ctx, bad)
    assert exc.value.code == code
    assert counts(store) == {'intents': 0, 'keys': 0}
    assert invoke(app, ctx, args)['intent']['state'] == 'proposed'


def test_description_edit_refuses_same_digest_even_with_refreshed_basis(admitted_basis):
    store, app, ctx, args, _ = admitted_basis
    pg.update_work_item_description(store, args['item_id'], 'changed before admission')
    with pytest.raises(ApplicationRejection) as exc:
        invoke(app, ctx, args)
    assert exc.value.code == 'effect-causal-stale-revision'
    args['causal_basis']['expected_revision'] = pg.item_release_revision(store, args['item_id'])
    assert pg.current_release(store, args['item_id'])['release_digest'] == args['causal_basis']['release_digest']
    with pytest.raises(ApplicationRejection) as exc:
        invoke(app, ctx, args)
    assert exc.value.code == 'effect-causal-release-mismatch'
    assert counts(store) == {'intents': 0, 'keys': 0}


def test_append_first_refuses_old_entry_tail_and_payload_digest(admitted_basis):
    store, app, ctx, args, _ = admitted_basis
    tail = append(store, args['run_id'])
    with pytest.raises(ApplicationRejection) as exc:
        invoke(app, ctx, args)
    assert exc.value.code == 'effect-causal-evidence-head-mismatch'
    args['causal_basis']['evidence_tail'] = {
        'item_id': tail['item_id'], 'chain_seq': tail['chain_seq'], 'entry_digest': tail['digest']}
    with pytest.raises(ApplicationRejection) as exc:
        invoke(app, ctx, args)
    assert exc.value.code == 'effect-causal-evidence-head-mismatch'
    args['causal_basis']['evidence_tail']['entry_digest'] = pg.evidence_entry_digest(tail)
    assert invoke(app, ctx, args)['intent']['state'] == 'proposed'


@pytest.mark.parametrize('legacy_first', [True, False])
def test_legacy_bound_same_key_cannot_adopt_each_other(admitted_basis, legacy_first):
    store, app, ctx, args, _ = admitted_basis
    legacy = {k: v for k, v in args.items() if k != 'causal_basis'}
    if legacy_first:
        app.invoke('work.effect.propose-v1', legacy, ctx)
        call = lambda: invoke(app, ctx, args)
    else:
        invoke(app, ctx, args)
        call = lambda: app.invoke('work.effect.propose-v1', legacy, ctx)
    with pytest.raises(ApplicationRejection) as exc:
        call()
    assert exc.value.code == 'idempotency-conflict'
    assert counts(store) == {'intents': 1, 'keys': 1}


def test_failure_after_insert_rolls_back_and_lost_reply_replays(admitted_basis, monkeypatch):
    store, app, ctx, args, _ = admitted_basis
    original = pg.propose_effect_intent
    def failed(*a, **kw):
        original(*a, **kw)
        raise OSError('injected before ledger result')
    with monkeypatch.context() as patch:
        patch.setattr(pg, 'propose_effect_intent', failed)
        with pytest.raises(OSError):
            invoke(app, ctx, args)
    assert counts(store) == {'intents': 0, 'keys': 0}
    committed = invoke(app, ctx, args)
    # Model a dropped transport response after the actual committed invocation.
    replay = invoke(app, ctx, args)
    assert replay == committed
    assert counts(store) == {'intents': 1, 'keys': 1}


def test_released_reservation_original_admission_still_qualifies(admitted_basis):
    store, app, ctx, args, reservation = admitted_basis
    pg.release_reservation(store, reservation['id'], actor='test')
    result = invoke(app, ctx, args)
    assert result['admission']['reservation_id'] == reservation['id']
    assert pg.get_reservation(store, reservation['id'])['state'] == 'released'


def test_other_item_reserve_receipt_refused(admitted_basis):
    store, app, ctx, args, _ = admitted_basis
    other, = _items(store)
    other_ctx = copy.deepcopy(ctx); other_ctx.idempotency_key = 'other-item-reserve'
    app.invoke('work.reservation.reserve-v1', {
        'item_id': other, 'actor': ctx.identity.actor, 'session_id': 'other-session',
        'expected_revision': pg.item_release_revision(store, other),
    }, other_ctx)
    args['causal_basis']['reserve_idempotency_key'] = other_ctx.idempotency_key
    with pytest.raises(ApplicationRejection) as exc: invoke(app, ctx, args)
    assert exc.value.code == 'effect-causal-reserve-mismatch'
    assert counts(store) == {'intents': 0, 'keys': 0}


@pytest.mark.parametrize('legacy_racer', [False, True])
def test_independent_same_key_races_serialize_before_item(admitted_basis, legacy_racer):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from tests.pg.test_native_reserve_owner import sibling
    store, _, ctx, args, _ = admitted_basis
    barrier = threading.Barrier(4)
    def racer(index):
        own = sibling(store)
        try:
            with own.conn.cursor() as cur: cur.execute("SET statement_timeout='8s'")
            own.conn.commit()
            request = copy.deepcopy(args)
            op = BOUND
            if legacy_racer and index % 2:
                op = 'work.effect.propose-v1'
                del request['causal_basis']
                # Different target exercises the cross-item unique key race.
                request['item_id'], = _items(own)
            barrier.wait(timeout=8)
            try: return WorkApplication.postgres(own).invoke(op, request, ctx)
            except ApplicationRejection as exc: return exc.code
        finally: own.conn.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(racer, range(4)))
    assert counts(store) == {'intents': 1, 'keys': 1}
    successes = [r for r in results if isinstance(r, dict)]
    assert successes
    assert len({r['intent']['intent_id'] for r in successes}) == 1
    if legacy_racer:
        # Each legacy racer names a different item; one operation wins.
        assert 'idempotency-conflict' in results
    else:
        assert len(successes) == 4
        assert all(r == successes[0] for r in successes)


@pytest.mark.parametrize('prerequisite', ['reserve', 'trailer'])
def test_uncommitted_prerequisite_is_missing_without_wait(admitted_basis, prerequisite):
    from tests.pg.test_native_reserve_owner import sibling
    store, app, ctx, args, _ = admitted_basis
    other = sibling(store)
    try:
        with store.conn.cursor() as cur: cur.execute("SET statement_timeout='1s'")
        store.conn.commit()
        with other.conn.cursor() as cur:
            if prerequisite == 'trailer':
                args['causal_basis']['commit_sha'] = 'd' * 40
                cur.execute('INSERT INTO release_commit(repo_id,release_digest,commit_sha) VALUES (%s,%s,%s)',
                            (store.repo_id, args['causal_basis']['release_digest'], 'd' * 40))
                code = 'effect-causal-commit-unbound'
            else:
                # Hold a real uncommitted ledger row. The bound read must not
                # claim or wait for this reserve key while holding item.
                args['causal_basis']['reserve_idempotency_key'] = 'uncommitted-reserve'
                cur.execute('INSERT INTO work_idempotency_ledger(repo_id,workspace_id,principal_id,tool,idempotency_key,request_digest,result) '
                            'SELECT repo_id,workspace_id,principal_id,tool,%s,request_digest,result '
                            'FROM work_idempotency_ledger WHERE repo_id=%s AND tool=%s',
                            ('uncommitted-reserve', store.repo_id, 'reservation.reserve-v1'))
                code = 'effect-causal-reserve-missing'
        with pytest.raises(ApplicationRejection) as exc: invoke(app, ctx, args)
        assert exc.value.code == code
        assert counts(store) == {'intents': 0, 'keys': 0}
        other.conn.commit()
        assert invoke(app, ctx, args)['intent']['state'] == 'proposed'
    finally:
        other.conn.rollback(); other.conn.close()


@pytest.mark.parametrize("contender", ["append", "edit"])
def test_bound_first_blocks_change_until_admission_commit(admitted_basis, monkeypatch, contender):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from tests.pg.test_native_reserve_owner import sibling
    store, app, ctx, args, _ = admitted_basis
    reached = threading.Event(); release = threading.Event()
    original = pg.propose_effect_intent
    def paused(*a, **kw):
        reached.set()
        assert release.wait(8), 'test did not release owner admission'
        return original(*a, **kw)
    monkeypatch.setattr(pg, 'propose_effect_intent', paused)
    appender = sibling(store); observer = sibling(store)
    try:
        with appender.conn.cursor() as cur: cur.execute("SET statement_timeout='8s'")
        appender.conn.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = pool.submit(invoke, app, ctx, args)
            assert reached.wait(8)
            if contender == 'append':
                appended = pool.submit(append, appender, args['run_id'])
            else:
                appended = pool.submit(pg.update_work_item_description, appender,
                                       args['item_id'], 'edit racing admission')
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    with observer.conn.cursor() as cur:
                        cur.execute('SELECT pg_blocking_pids(%s) AS blockers', (appender.conn.info.backend_pid,))
                        blockers = cur.fetchone()['blockers']
                    if store.conn.info.backend_pid in blockers: break
                    time.sleep(.01)
                else: pytest.fail('change never blocked on the actual admission transaction')
            finally: release.set()
            first = admitted.result(timeout=8)
            tail = appended.result(timeout=8)
        if contender == 'append':
            assert tail['chain_seq'] == args['causal_basis']['evidence_tail']['chain_seq'] + 1
        else:
            assert pg.item_release_revision(store, args['item_id']) != args['causal_basis']['expected_revision']
        assert invoke(app, ctx, args)['admission'] == first['admission']
    finally:
        release.set(); appender.conn.close(); observer.conn.close()


@pytest.mark.parametrize('change', ['authority', 'principal', 'workspace', 'client', 'grant'])
def test_bound_actual_authorization_refusals_have_no_effects(admitted_basis, change):
    store, app, ctx, args, _ = admitted_basis
    changed = copy.deepcopy(ctx)
    if change == 'authority':
        changed.identity.authorities = changed.identity.authorities - {'work.effect.propose'}
        code = 'authority-required'
    else:
        setattr(changed.identity, f'{change}_id', 'foreign-binding')
        code = 'run-not-found'
    with pytest.raises(ApplicationRejection) as exc: invoke(app, changed, args)
    assert exc.value.code == code
    assert counts(store) == {'intents': 0, 'keys': 0}


def test_reserve_key_only_in_foreign_scope_is_missing(admitted_basis):
    store, app, ctx, args, _ = admitted_basis
    foreign = copy.deepcopy(ctx)
    foreign.identity.principal_id = 'foreign-principal'
    foreign.idempotency_key = 'foreign-scoped-reserve'
    app.invoke('work.reservation.reserve-v1', {
        'item_id': args['item_id'], 'actor': foreign.identity.actor, 'session_id': 'foreign-session',
        'expected_revision': args['causal_basis']['expected_revision'],
    }, foreign)
    args['causal_basis']['reserve_idempotency_key'] = foreign.idempotency_key
    with pytest.raises(ApplicationRejection) as exc: invoke(app, ctx, args)
    assert exc.value.code == 'effect-causal-reserve-missing'
    assert counts(store) == {'intents': 0, 'keys': 0}


@pytest.mark.parametrize('lock', ['existing-intent', 'repository', 'replay-item-chain'])
def test_forbidden_reverse_locks_are_not_requested(admitted_basis, lock):
    from tests.pg.test_native_reserve_owner import sibling
    store, app, ctx, args, _ = admitted_basis
    original = None
    if lock == 'existing-intent':
        legacy = {k: v for k, v in args.items() if k != 'causal_basis'}
        legacy['idempotency_key'] = 'existing-intent-key'
        existing = app.invoke('work.effect.propose-v1', legacy, ctx)['intent']
    elif lock == 'replay-item-chain':
        original = invoke(app, ctx, args)
    other = sibling(store)
    try:
        with store.conn.cursor() as cur: cur.execute("SET statement_timeout='1s'")
        store.conn.commit()
        with other.conn.cursor() as cur:
            if lock == 'existing-intent':
                cur.execute('SELECT * FROM work_effect_intent WHERE repo_id=%s AND intent_id=%s FOR UPDATE',
                            (store.repo_id, existing['intent_id']))
            elif lock == 'repository':
                pg._lock_repo_for_claims(cur, other)
            else:
                pg._release_basis_locked(cur, store.repo_id, args['item_id'])
                pg.lock_evidence_chain_in_transaction(cur, store.repo_id, args['run_id'])
        result = invoke(app, ctx, args)
        if original: assert result == original
        assert result['admission']['causal_basis'] == args['causal_basis']
    finally:
        other.conn.rollback(); other.conn.close()
