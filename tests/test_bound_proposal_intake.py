"""Bound envelope isolation and exact immutable admission correlation."""
import copy
import json
import pytest
from sprintctl import outbox, proposal_intake as intake
from tests.test_native_proposal_intake import payload, capture, receipt, Refused
from tests.test_bound_proposal_owner import BASIS


def bound_payload():
    request, binding = payload()
    request.update(schema_version=intake.BOUND_SCHEMA, operation=intake.BOUND_OPERATION)
    request['arguments']['causal_basis'] = copy.deepcopy(BASIS)
    return request, binding


def bound_receipt(request, binding):
    result = receipt(request, binding)
    result['intent'].pop('causal_basis')
    result['intent']['release_digest'] = request['arguments']['causal_basis']['release_digest']
    result['admission'] = {'schema_version': 'sprintctl-bound-proposal-admission/v1',
        'causal_basis': copy.deepcopy(request['arguments']['causal_basis']),
        'run_binding': copy.deepcopy(binding), 'reservation_id': 1}
    return result


@pytest.mark.parametrize('change', ['schema', 'operation', 'basis-missing', 'basis-extra', 'plain-revision', 'bool-seq', 'envelope-key'])
def test_closed_bound_capture_never_creates_invalid_outbox(tmp_path, change):
    request, binding = bound_payload()
    if change == 'schema': request['schema_version'] = intake.SCHEMA
    elif change == 'operation': request['operation'] = intake.OPERATION
    elif change == 'basis-missing': del request['arguments']['causal_basis']['commit_sha']
    elif change == 'basis-extra': request['arguments']['causal_basis']['extra'] = 1
    elif change == 'plain-revision': request['arguments']['causal_basis']['expected_revision'] = BASIS['expected_revision'].rsplit('@revise:', 1)[0]
    elif change == 'bool-seq': request['arguments']['causal_basis']['evidence_tail']['chain_seq'] = True
    else: request['idempotency_key'] = 'envelope-key'
    path = tmp_path/'producer.db'
    with pytest.raises(ValueError): capture(path, request, binding)
    assert not path.exists()


@pytest.mark.parametrize('bound_first', [True, False])
def test_cross_variant_same_key_cannot_replace_captured_intent(tmp_path, bound_first):
    legacy, binding = payload(); bound, _ = bound_payload(); path = tmp_path/'producer.db'
    first, second = (bound, legacy) if bound_first else (legacy, bound)
    capture(path, first, binding)
    with pytest.raises(ValueError, match='already captured'): capture(path, second, binding)
    assert len(intake.status(path)['pending_proposal_request_ids']) == 1


@pytest.mark.parametrize('failure', ['basis', 'binding', 'release', 'reservation-bool', 'reservation-zero', 'extra', 'missing', 'schema', 'legacy-only'])
def test_wrong_admission_stops_fifo_without_confirmation(tmp_path, failure):
    request, binding = bound_payload(); path = tmp_path/'producer.db'
    first = capture(path, request, binding)
    later, _ = payload(); later['arguments']['idempotency_key'] = 'later-legacy'
    second = capture(path, later, binding)
    result = bound_receipt(request, binding)
    if failure == 'basis': result['admission']['causal_basis']['commit_sha'] = 'f' * 40
    elif failure == 'binding': result['admission']['run_binding']['grant_id'] = 'different'
    elif failure == 'release': result['intent']['release_digest'] = 'd' * 64
    elif failure == 'reservation-bool': result['admission']['reservation_id'] = True
    elif failure == 'reservation-zero': result['admission']['reservation_id'] = 0
    elif failure == 'extra': result['admission']['extra'] = 1
    elif failure == 'missing': del result['admission']['run_binding']
    elif failure == 'schema': result['admission']['schema_version'] = 'wrong'
    else: del result['admission']
    calls = []
    def invoke(op, args):
        calls.append(op)
        return binding if op == intake.RESOLVE else result
    report = intake.synchronize(path, repo_id='repo', invoke=invoke, rejection_type=Refused)
    assert report['pending_proposal_request_ids'] == [first['request_id'], second['request_id']]
    assert report['proposal_attempts'][0]['phase'] == 'unknown'
    assert calls == [intake.RESOLVE, intake.BOUND_OPERATION]


def test_mixed_fifo_preserves_original_bound_receipt(tmp_path):
    bound, binding = bound_payload(); legacy, _ = payload()
    legacy['arguments']['idempotency_key'] = 'later-legacy'
    path = tmp_path/'producer.db'
    first = capture(path, bound, binding); second = capture(path, legacy, binding)
    calls = []
    def invoke(op, args):
        calls.append(op)
        if op == intake.RESOLVE: return binding
        return bound_receipt(bound, binding) if op == intake.BOUND_OPERATION else receipt(legacy, binding)
    report = intake.synchronize(path, repo_id='repo', invoke=invoke, rejection_type=Refused)
    assert report['confirmed_proposal_request_ids'] == [first['request_id'], second['request_id']]
    assert calls == [intake.RESOLVE, intake.BOUND_OPERATION, intake.RESOLVE, intake.OPERATION]
    assert not intake.status(path)['pending_proposal_request_ids']


def test_unavailable_bound_never_falls_back(tmp_path):
    request, binding = bound_payload(); path = tmp_path/'producer.db'; capture(path, request, binding)
    calls = []
    def invoke(op, args):
        calls.append(op)
        if op == intake.RESOLVE: return binding
        raise Refused('unsupported bound operation')
    report = intake.synchronize(path, repo_id='repo', invoke=invoke, rejection_type=Refused)
    assert report['proposal_attempts'][0]['phase'] == 'rejected'
    assert calls == [intake.RESOLVE, intake.BOUND_OPERATION]


def test_released_legacy_confirmed_and_pending_history_remains_unchanged(tmp_path):
    import base64
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent/'fixtures/bound-carrier-v0142-legacy-history.json').read_text())
    path = tmp_path/'producer.db'; conn = outbox.open_outbox(path)
    for table in fixture['tables']: conn.execute(table['schema'])
    for table in fixture['tables']:
        for row in table['rows']:
            values = [base64.b64decode(v['bytes_base64']) if isinstance(v, dict) else v for v in row]
            conn.execute('INSERT INTO '+table['name']+' VALUES ('+','.join('?' for _ in values)+')', values)
    for trigger in fixture['triggers']: conn.execute(trigger)
    conn.commit()
    original = conn.execute('SELECT source,source_sha256 FROM native_proposal_request ORDER BY rowid').fetchall()
    confirmed = conn.execute("SELECT result_json,result_sha256 FROM native_proposal_attempt WHERE phase='confirmed'").fetchone()
    conn.close()
    report = intake.status(path)
    assert len(report['pending_proposal_request_ids']) == 1
    binding = fixture['binding']; pending = fixture['requests'][1]
    request, _ = bound_payload(); request['arguments']['idempotency_key'] = 'new-bound-request'
    # Same captured run/scope as the released fixture; only the repository differs.
    appended = capture(path, request, binding)
    def invoke(op,args):
        if op == intake.RESOLVE: return binding
        return receipt(pending,binding) if op == intake.OPERATION else bound_receipt(request,binding)
    report = intake.synchronize(path,repo_id=binding['repo_id'],invoke=invoke,rejection_type=Refused)
    assert len(report['confirmed_proposal_request_ids']) == 2
    assert report['confirmed_proposal_request_ids'][-1] == appended['request_id']
    conn = outbox.open_outbox(path)
    assert conn.execute('SELECT source,source_sha256 FROM native_proposal_request ORDER BY rowid LIMIT 2').fetchall() == original
    assert conn.execute("SELECT result_json,result_sha256 FROM native_proposal_attempt WHERE phase='confirmed' ORDER BY sequence LIMIT 1").fetchone() == confirmed
    conn.close()


@pytest.mark.parametrize('damage', ['source', 'admission', 'confirmed-operation'])
@pytest.mark.parametrize('reader', ['status', 'capture', 'sync'])
def test_damaged_bound_history_refused_before_any_rpc(tmp_path, damage, reader):
    request,binding = bound_payload(); path = tmp_path/'producer.db'; capture(path,request,binding)
    intake.synchronize(path,repo_id='repo',invoke=lambda op,a: binding if op==intake.RESOLVE else bound_receipt(request,binding),rejection_type=Refused)
    conn = outbox.open_outbox(path)
    if damage == 'source':
        conn.execute('DROP TRIGGER native_proposal_request_update')
        conn.execute("UPDATE native_proposal_request SET source=?", (b'{}',))
    else:
        conn.execute('DROP TRIGGER native_proposal_attempt_update')
        if damage == 'confirmed-operation':
            conn.execute("UPDATE native_proposal_attempt SET operation=? WHERE phase='confirmed'", (intake.OPERATION,))
        else:
            corrupted = bound_receipt(request,binding)
            corrupted['admission']['run_binding']['workspace_id'] = 'foreign-workspace'
            content = json.dumps(corrupted,sort_keys=True,separators=(',',':'))
            conn.execute("UPDATE native_proposal_attempt SET result_json=?,result_sha256=? WHERE phase='confirmed'", (content,intake._digest(content.encode())))
    conn.commit(); conn.close()
    with pytest.raises(ValueError):
        if reader == 'status': intake.status(path)
        elif reader == 'capture': capture(path,request,binding)
        else: intake.synchronize(path,repo_id='repo',invoke=lambda *a: pytest.fail('RPC after damaged history'),rejection_type=Refused)


@pytest.mark.parametrize('field', ['schema_version', 'operation'])
@pytest.mark.parametrize('value', [[], {}])
def test_nonstrings_are_controlled_validation_refusals(tmp_path, field, value):
    request,binding = bound_payload(); request[field] = value
    path = tmp_path/'producer.db'
    with pytest.raises(ValueError,match='must be strings'): capture(path,request,binding)
    assert not path.exists()
