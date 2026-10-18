"""Native owner responses survive offline read-packet transport; not an owner import."""
import json
import os
import subprocess
import sys
import uuid

import pytest

from sprintctl import effect_verification, pg
from sprintctl.result_packet import create_packet, canonical
from tests.pg._shared import PG_MARKS, _PG_URL, dict_row, psycopg
from tests.pg.test_effect_intent import PROPOSER, _app, _items, _propose, _run
from tests.pg.test_protected_artifact_acceptance import CHECKER, accept, receipt

pytestmark = PG_MARKS


@pytest.fixture(autouse=True)
def snapshot_factory(store):
    store.connection_factory = lambda: psycopg.connect(_PG_URL, row_factory=dict_row)


@pytest.mark.parametrize('condition', ['matching', 'omitted-receipt', 'changed-artifact'])
def test_native_result_read_packet_survives_fresh_process_inspection(store, tmp_path, condition):
    app = _app(store)
    item = _items(store)[0]
    reserved = pg.reserve(store, item, actor='fixture', session_id=uuid.uuid4().hex, role='execution',
        acceptance_contract={'effect_verification_required': True,
            'effect_check_revisions': {'patch-check': 'sha256:' + 'd' * 64}})
    intent = _propose(store, item, _run(store, PROPOSER))
    ref, _, _ = receipt(store, intent, pg.get_release(store, reserved['release_digest']))
    accept(app, intent, ref)
    before = pg.get_effect_intent(store, intent['intent_id'])
    arguments = {k: intent[k] for k in ('intent_id', 'revision', 'canonical_intent_digest')}
    arguments['artifact_sha256'] = ('sha256:' + '0' * 64 if condition == 'changed-artifact'
        else effect_verification.artifact_digest(intent['unified_diff']))
    if condition != 'omitted-receipt':
        arguments['verification_ref'] = ref
    acceptance = app.invoke('work.effect.acceptance-snapshot-v1', arguments, CHECKER)
    continuation = app.invoke('work.run.continuation-delta-v1', {
        'run_id': _run(store, CHECKER), 'item_id': item}, CHECKER)
    packet = create_packet(acceptance, continuation, packaged_at='2026-10-18T12:00:00Z')
    path = tmp_path / 'result.json'
    path.write_bytes(canonical(packet))
    result = subprocess.run([sys.executable, '-m', 'sprintctl', 'result', 'packet', 'inspect', str(path),
        '--expected-sha256', packet['packet_sha256'], '--format', 'json'], cwd=tmp_path,
        env={'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['packet']['sources']['acceptance']['response'] == acceptance
    assert report['packet']['sources']['continuation']['response'] == continuation
    assert report['integrity']['external_expectation'] == 'matches'
    assert report['source_authenticity'] == 'unverified'
    assert report['cross_response_atomicity'] == 'not-established'
    assert report['authorizes_action'] is False
    explanation = report['effect_explanation']
    if condition == 'changed-artifact':
        assert 'REQUESTED ARTIFACT: MISMATCH' in explanation
        assert 'artifact-check comparison: matches' in explanation
    elif condition == 'omitted-receipt':
        assert 'Receipt: not-supplied' in explanation
    else:
        assert 'REQUESTED ARTIFACT: matches' in explanation
    assert 'terminal work acceptance is not established' in explanation
    assert pg.get_effect_intent(store, intent['intent_id']) == before
