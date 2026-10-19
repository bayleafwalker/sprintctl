"""Seeded persisted violations must fail the independent full-state checker."""
from copy import deepcopy
import json
import sqlite3

import pytest

from sprintctl import projection
from tests._projection_rebuild_contract import (
    canonical, check_state, digest, expected_record, snapshot,
)

AT = '2026-10-18T00:00:00Z'
REPO = 'authored-oracle-fixture'
STREAM = '00000000-0000-4000-8000-000000000001'


def populated(path, count):
    records = [expected_record(i, STREAM, AT) for i in range(1, count + 1)]
    connection = projection.open_cached_projection(path, repo_id=REPO)
    try:
        projection.apply_ingested_records(connection, [
            projection.CachedIngestRecord(i, deepcopy(record))
            for i, record in enumerate(records, 1)], advanced_at=AT)
    finally:
        connection.close()
    return records


@pytest.mark.parametrize('count', [0, 3])
def test_projection_rebuild_oracle_accepts_valid_manifest(tmp_path, count):
    path = tmp_path / 'cache.db'
    records = populated(path, count)
    state, evidence = snapshot(path)
    check_state(state, REPO, records, 0, 2_000_000_000)
    assert evidence['reader']['returncode'] == 0


FAULTS = [
    ('record-json', 'cached record hash'),
    ('record-hash', 'cached record hash'),
    ('boolean-schema-version', 'authored remote envelope'),
    ('float-origin-seq', 'authored remote envelope'),
    ('watermark-ahead', 'ingest watermark population'),
    ('consistent-partial', 'projection record population'),
    ('wrong-repository', 'projection repository/schema binding'),
    ('authority-record', 'unexpected authority records'),
    ('authority-watermark', 'unexpected authority watermark'),
]


@pytest.mark.parametrize('fault,reason', FAULTS)
def test_projection_rebuild_oracle_refuses_seeded_violation(tmp_path, fault, reason):
    path = tmp_path / 'cache.db'
    records = populated(path, 3)
    valid, valid_evidence = snapshot(path)
    check_state(valid, REPO, records, 0, 2_000_000_000)
    connection = sqlite3.connect(path)
    try:
        if fault == 'record-json':
            changed = {**records[1], 'actor': 'different-authored-actor'}
            connection.execute('UPDATE cached_ingest_record SET record_json=? WHERE ingest_offset=2',
                               (canonical(changed),))
        elif fault in {'boolean-schema-version', 'float-origin-seq'}:
            field, value = ('schema_version', True) if fault == 'boolean-schema-version' else ('origin_seq', 2.0)
            encoded = canonical({**records[1], field: value})
            connection.execute('UPDATE cached_ingest_record SET record_json=?,record_sha256=? WHERE ingest_offset=2',
                               (encoded, digest(encoded)))
        elif fault == 'record-hash':
            connection.execute("UPDATE cached_ingest_record SET record_sha256=? WHERE ingest_offset=2", ('0'*64,))
        elif fault == 'watermark-ahead':
            connection.execute('UPDATE cached_ingest_watermark SET ingest_offset=4')
        elif fault == 'consistent-partial':
            connection.execute('DELETE FROM cached_ingest_record WHERE ingest_offset=3')
            connection.execute('UPDATE cached_ingest_watermark SET ingest_offset=2')
        elif fault == 'wrong-repository':
            connection.execute("UPDATE cached_projection_meta SET repo_id='other-repository'")
        elif fault == 'authority-record':
            connection.execute('INSERT INTO cached_authority_decision VALUES (?,?,?,?,?)',
                               (REPO, 7, '{}', digest('{}'), AT))
        elif fault == 'authority-watermark':
            connection.execute('UPDATE cached_authority_watermark SET decision_ingest_offset=7,advanced_at=?', (AT,))
        else:
            raise AssertionError('unknown authored fault')
        connection.commit()
    finally:
        connection.close()
    changed, changed_evidence = snapshot(path)
    with pytest.raises(AssertionError) as caught:
        check_state(changed, REPO, records, 0, 2_000_000_000)
    assert str(caught.value) == reason, 'unrelated oracle failure'
    (tmp_path / 'oracle-control.json').write_text(json.dumps({
        'fault': fault, 'expected_reason': reason, 'actual_reason': str(caught.value),
        'valid': valid, 'changed': changed, 'valid_reader': valid_evidence,
        'changed_reader': changed_evidence}, indent=2) + '\n')
