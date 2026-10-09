"""Bound producer recovery against the actual disposable work owner."""
import copy
import json
import pytest
from sprintctl import outbox, proposal_intake as intake, pg
from tests.pg._shared import PG_MARKS
from tests.pg.test_bound_proposal_owner import admitted_basis, append
from tests.pg.test_effect_intent import _reject, _accept, _mark_applied
from tests.test_native_proposal_intake import capture

pytestmark = PG_MARKS


@pytest.mark.parametrize('advance', ['none', 'edit-tail-release-reject', 'accepted', 'applied'])
def test_actual_owner_committed_lost_reply_replays_original_admission(admitted_basis, tmp_path, advance):
    store, app, ctx, args, reservation = admitted_basis
    binding = app.invoke(intake.RESOLVE, {'run_id': args['run_id']}, ctx)
    request = {'schema_version': intake.BOUND_SCHEMA, 'operation': intake.BOUND_OPERATION,
               'arguments': args}
    path = tmp_path/'producer.db'; captured = capture(path, request, binding)
    committed = []
    def lost(op, arguments):
        result = app.invoke(op, arguments, ctx)
        if op == intake.BOUND_OPERATION:
            committed.append(result)
            raise OSError('actual owner committed; reply lost')
        return result
    from sprintctl.application import ApplicationRejection
    report = intake.synchronize(path, repo_id=store.repo_id, invoke=lost, rejection_type=ApplicationRejection)
    # Transport faults are unknown, not explicit owner refusals.
    assert report['pending_proposal_request_ids'] == [captured['request_id']]
    assert committed
    if advance == 'edit-tail-release-reject':
        pg.update_work_item_description(store, args['item_id'], 'edit after commit')
        append(store, args['run_id'])
        pg.release_reservation(store, reservation['id'], actor='test')
        _reject(store, committed[0]['intent'])
    elif advance in {'accepted','applied'}:
        accepted = _accept(store, committed[0]['intent'])
        if advance == 'applied': _mark_applied(store, accepted)
    from sprintctl.application import ApplicationRejection
    report = intake.synchronize(path, repo_id=store.repo_id,
        invoke=lambda op,a: app.invoke(op,a,ctx), rejection_type=ApplicationRejection)
    assert report['confirmed_proposal_request_ids'] == [captured['request_id']]
    conn = outbox.open_outbox(path)
    receipt = json.loads(conn.execute("SELECT result_json FROM native_proposal_attempt WHERE phase='confirmed'").fetchone()[0]); conn.close()
    assert receipt['admission'] == committed[0]['admission']
    assert receipt['intent']['state'] == {'none':'proposed','edit-tail-release-reject':'rejected','accepted':'accepted','applied':'applied'}[advance]
    assert not intake.status(path)['pending_proposal_request_ids']


def test_actual_stale_tail_stops_mixed_fifo_without_refresh(admitted_basis, tmp_path):
    from sprintctl.application import ApplicationRejection
    store, app, ctx, args, _ = admitted_basis
    binding = app.invoke(intake.RESOLVE, {'run_id': args['run_id']}, ctx)
    request = {'schema_version': intake.BOUND_SCHEMA, 'operation': intake.BOUND_OPERATION, 'arguments': args}
    path = tmp_path/'producer.db'; first = capture(path, request, binding)
    legacy = copy.deepcopy(request); legacy.update(schema_version=intake.SCHEMA, operation=intake.OPERATION)
    del legacy['arguments']['causal_basis']; legacy['arguments']['idempotency_key'] = 'later-legacy-key'
    second = capture(path, legacy, binding)
    append(store, args['run_id'])
    report = intake.synchronize(path, repo_id=store.repo_id,
        invoke=lambda op,a: app.invoke(op,a,ctx), rejection_type=ApplicationRejection)
    assert report['pending_proposal_request_ids'] == [first['request_id'], second['request_id']]
    assert report['proposal_attempts'][0]['code'] == 'effect-causal-evidence-head-mismatch'
    refreshed = copy.deepcopy(request); tail = pg.evidence_tail(store, args['run_id'])
    refreshed['arguments']['causal_basis']['evidence_tail'] = {
        'item_id': tail['item_id'], 'chain_seq': tail['chain_seq'], 'entry_digest': pg.evidence_entry_digest(tail)}
    with pytest.raises(ValueError, match='already captured'): capture(path, refreshed, binding)


@pytest.mark.parametrize('legacy_racer', [False, True])
def test_independent_producers_share_one_owner_key(admitted_basis, tmp_path, legacy_racer):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from sprintctl.application import ApplicationRejection, WorkApplication
    from tests.pg.test_native_reserve_owner import sibling
    from tests.pg.test_native_proposal_intake import counts
    store, app, ctx, args, _ = admitted_basis
    binding = app.invoke(intake.RESOLVE, {'run_id':args['run_id']},ctx)
    requests=[]; paths=[]
    for index in range(2):
        request={'schema_version':intake.BOUND_SCHEMA,'operation':intake.BOUND_OPERATION,'arguments':copy.deepcopy(args)}
        if legacy_racer and index:
            request.update(schema_version=intake.SCHEMA,operation=intake.OPERATION)
            del request['arguments']['causal_basis']
        path=tmp_path/f'producer-{index}.db'; capture(path,request,binding)
        requests.append(request); paths.append(path)
    barrier=threading.Barrier(2)
    def producer(index):
        own=sibling(store)
        try:
            with own.conn.cursor() as cur: cur.execute("SET statement_timeout='8s'")
            own.conn.commit()
            application=WorkApplication.postgres(own)
            def invoke(op,arguments):
                if op != intake.RESOLVE: barrier.wait(timeout=8)
                return application.invoke(op,arguments,ctx)
            return intake.synchronize(paths[index],repo_id=store.repo_id,invoke=invoke,rejection_type=ApplicationRejection)
        finally: own.conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool: reports=list(pool.map(producer,range(2)))
    assert counts(store)=={'intents':1,'keys':1}
    phases=[r['proposal_attempts'][0]['phase'] for r in reports]
    if legacy_racer:
        assert sorted(phases)==['confirmed','rejected']
        refusal=next(r for r in reports if r['proposal_attempts'][0]['phase']=='rejected')
        assert refusal['proposal_attempts'][0]['code']=='idempotency-conflict'
    else: assert phases==['confirmed','confirmed']
