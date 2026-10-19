"""Independent persisted-state oracle and bounded raw reader for F13 fixtures."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests._outbox_fsync_probe import bounded_regular, observe

TABLES = {
    'cached_ingest_record': [('repo_id', 'TEXT', 1, 1), ('ingest_offset', 'INTEGER', 1, 2),
        ('record_json', 'TEXT', 1, 0), ('record_sha256', 'TEXT', 1, 0), ('received_at', 'TEXT', 1, 0)],
    'cached_ingest_watermark': [('repo_id', 'TEXT', 0, 1), ('ingest_offset', 'INTEGER', 1, 0),
        ('advanced_at', 'TEXT', 0, 0)],
    'cached_authority_decision': [('repo_id', 'TEXT', 1, 1), ('decision_ingest_offset', 'INTEGER', 1, 2),
        ('decision_json', 'TEXT', 1, 0), ('decision_sha256', 'TEXT', 1, 0), ('received_at', 'TEXT', 1, 0)],
    'cached_authority_watermark': [('repo_id', 'TEXT', 0, 1), ('decision_ingest_offset', 'INTEGER', 1, 0),
        ('advanced_at', 'TEXT', 0, 0)],
    'cached_projection_meta': [('singleton', 'INTEGER', 0, 1), ('schema_version', 'INTEGER', 1, 0),
        ('repo_id', 'TEXT', 1, 0)],
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def authored_payload(index, label='source'):
    return {'index': index, 'label': label, 'summary': 'rebuild δ'}


def expected_record(index, stream, created_at, label='source'):
    """Authored fixture values; only producer identity/time are observed inputs."""
    assert str(UUID(stream)) == stream and UUID(stream).version == 4
    check_time(created_at, 0, time.time())
    payload = authored_payload(index, label)
    return {'origin_stream_id': stream, 'origin_seq': index,
            'event_id': f'rebuild-{label}-{index}', 'schema_version': 1,
            'record_class': 'observation', 'event_type': 'work.completed',
            'actor': 'verification', 'runtime_session_id': 'rebuild-contract',
            'occurred_at': '2026-10-18T00:00:00Z', 'basis_revision': None,
            'correlation_id': None, 'causation_id': None, 'payload': payload,
            'payload_sha256': digest(canonical(payload)), 'created_at': created_at}


def check_time(value, earliest, latest):
    assert type(value) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value), 'canonical UTC time'
    instant = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    assert earliest - 1 <= instant <= latest + 1, 'time outside actual operation interval'


def check_state(state, repo_id, records, earliest, latest):
    assert state['integrity'] == 'ok', 'SQLite integrity'
    assert state['journal_mode'] == 'delete', 'unsupported fixture journal mode'
    assert set(state['rows']) == set(TABLES), 'projection table population'
    for table, expected in TABLES.items():
        schema = state['schema'][table]
        assert [(r['name'], r['type'], r['notnull'], r['pk']) for r in schema] == expected, 'projection schema shape'
        assert [r['cid'] for r in schema] == list(range(len(expected)))
        assert all(r['dflt_value'] is None for r in schema)
    rows = state['rows']
    assert rows['cached_projection_meta'] == [
        {'singleton': 1, 'schema_version': 2, 'repo_id': repo_id}], 'projection repository/schema binding'
    assert rows['cached_authority_decision'] == [], 'unexpected authority records'
    assert rows['cached_authority_watermark'] == [
        {'repo_id': repo_id, 'decision_ingest_offset': 0, 'advanced_at': None}], 'unexpected authority watermark'
    actual = sorted(rows['cached_ingest_record'], key=lambda r: r['ingest_offset'])
    assert len(actual) == len(records), 'projection record population'
    for offset, (row, expected) in enumerate(zip(actual, records), 1):
        assert row['repo_id'] == repo_id, 'record repository binding'
        assert type(row['ingest_offset']) is int and row['ingest_offset'] == offset, 'ingest offset continuity'
        value = json.loads(row['record_json'])
        assert canonical(value) == row['record_json'], 'canonical record JSON'
        assert row['record_sha256'] == digest(row['record_json']), 'cached record hash'
        assert row['record_json'] == canonical(expected), 'authored remote envelope'
        check_time(row['received_at'], earliest, latest)
    watermarks = rows['cached_ingest_watermark']
    assert len(watermarks) == 1 and watermarks[0]['repo_id'] == repo_id, 'ingest watermark repository binding'
    mark = watermarks[0]
    assert type(mark['ingest_offset']) is int and mark['ingest_offset'] == len(records), 'ingest watermark population'
    if records:
        check_time(mark['advanced_at'], earliest, latest)
    else:
        assert mark['advanced_at'] is None, 'empty watermark timestamp'


def raw_reader(path):
    deadline = time.monotonic() + .5
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
    try:
        connection.execute('PRAGMA query_only=ON')
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        tables = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        assert len(tables) <= 8, 'raw table budget'
        result = {'integrity': integrity, 'journal_mode': connection.execute('PRAGMA journal_mode').fetchone()[0],
                  'schema': {}, 'rows': {}}
        for name in tables:
            identifier = '"' + name.replace('"', '""') + '"'
            columns = [dict(r) for r in connection.execute('PRAGMA table_info(' + identifier + ')')]
            assert len(columns) <= 32, 'raw column budget'
            rows = [dict(r) for r in connection.execute('SELECT * FROM ' + identifier + ' LIMIT 33')]
            assert len(rows) <= 32, 'raw row budget'
            result['schema'][name] = columns
            result['rows'][name] = sorted(rows, key=canonical)
        return result
    finally:
        connection.close()


def snapshot(path):
    """A separately owned reader never initializes or migrates the target."""
    before, identity = bounded_regular(path)
    state, evidence = observe([sys.executable, '-B', __file__, '--raw-reader', str(path)],
                              timeout=3, output_limit=65536)
    after, _ = bounded_regular(path)
    assert before == after, 'raw reader changed original bytes'
    return state, {'database': identity, 'reader': evidence}


def schema_v1(path):
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE cached_ingest_watermark (
                singleton INTEGER PRIMARY KEY, ingest_offset INTEGER NOT NULL, advanced_at TEXT);
            INSERT INTO cached_ingest_watermark VALUES (1,7,'2026-07-21T12:00:00Z');
            CREATE TABLE cached_projection_meta (singleton INTEGER PRIMARY KEY,schema_version INTEGER NOT NULL);
            INSERT INTO cached_projection_meta VALUES (1,1);
        """)
    finally:
        connection.close()


if __name__ == '__main__':
    assert len(sys.argv) == 3 and sys.argv[1] == '--raw-reader'
    encoded = canonical(raw_reader(sys.argv[2]))
    assert len(encoded.encode()) <= 65536
    print(encoded)
