"""Disposable-process verification support, never imported by product code."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import sys

from sprintctl import outbox, projection

REPO = 'process-recovery-fixture'
SEED_AT = '2026-10-18T01:00:00Z'
TARGET_AT = '2026-10-18T02:00:00Z'


def append(conn, event_id, phase):
    return outbox.append_observation(conn, event_type='work.completed', actor='verification',
        event_id=event_id, occurred_at=TARGET_AT, runtime_session_id='process-fixture', payload={'phase': phase})


def ingest(offset):
    return projection.CachedIngestRecord(offset, {'event_id': f'ingest-{offset}', 'value': offset})


def decision(offset):
    return projection.CachedAuthorityDecision(offset, {'request_event_id': f'request-{offset}', 'outcome': 'accepted'})


def apply(conn, kind):
    if kind == 'outbox':
        return asdict(append(conn, 'new-event', 'new'))
    if kind == 'ingest':
        return asdict(projection.apply_ingested_records(conn, [ingest(2), ingest(3)], advanced_at=TARGET_AT))
    if kind == 'authority':
        return asdict(projection.apply_authority_decisions(conn, [decision(8), decision(13)], advanced_at=TARGET_AT))
    raise ValueError('invalid fixture kind')


def seed(path, kind):
    conn = outbox.open_outbox(path) if kind == 'outbox' else projection.open_cached_projection(path, repo_id=REPO)
    try:
        if kind == 'outbox':
            append(conn, 'seed-event', 'seed')
        else:
            projection.apply_ingested_records(conn, [ingest(1)], advanced_at=SEED_AT)
            projection.apply_authority_decisions(conn, [decision(3)], advanced_at=SEED_AT)
    finally:
        conn.close()


def state(conn, kind):
    """Read stored fields directly; no product helper, cleanup or repair."""
    def rows(sql):
        return [dict(row) for row in conn.execute(sql)]
    if kind == 'outbox':
        return {'records': rows('SELECT * FROM outbox_record ORDER BY origin_seq'),
            'stream': rows('SELECT * FROM outbox_stream ORDER BY singleton')}
    return {'ingest': rows('SELECT * FROM cached_ingest_record ORDER BY repo_id,ingest_offset'),
        'ingest_cursor': rows('SELECT * FROM cached_ingest_watermark ORDER BY repo_id'),
        'authority': rows('SELECT * FROM cached_authority_decision ORDER BY repo_id,decision_ingest_offset'),
        'authority_cursor': rows('SELECT * FROM cached_authority_watermark ORDER BY repo_id'),
        'meta': rows('SELECT * FROM cached_projection_meta ORDER BY singleton')}


def raw_recover(path, kind):
    # This normal opening performs SQLite automatic recovery when necessary.
    # Never use immutable/readonly mode or remove rollback journals/WAL files.
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise AssertionError(f'SQLite integrity check: {integrity}')
        return state(conn, kind)
    finally:
        conn.close()


class RecoveryInvariantFailure(AssertionError):
    pass


def check_recovered(kind, actual, expected):
    """One checker for killed processes and deliberately invalid stored fixtures."""
    if kind == 'outbox':
        records = actual['records']
        seqs = [r['origin_seq'] for r in records]
        if seqs != list(range(1, len(records) + 1)) or len(actual['stream']) != 1:
            raise RecoveryInvariantFailure('outbox retained sequence')
        if actual['stream'][0]['next_origin_seq'] != len(records) + 1:
            raise RecoveryInvariantFailure('outbox next sequence')
    else:
        for rows_key, cursor_key, offset_key in [
            ('ingest', 'ingest_cursor', 'ingest_offset'),
            ('authority', 'authority_cursor', 'decision_ingest_offset'),
        ]:
            offsets = [r[offset_key] for r in actual[rows_key]]
            if len(actual[cursor_key]) != 1 or actual[cursor_key][0][offset_key] != max(offsets, default=0):
                raise RecoveryInvariantFailure(rows_key + ' watermark')
        # Authority offsets are intentionally sparse and independent of ingest.
    if actual != expected:
        raise RecoveryInvariantFailure('exact committed manifest')


class CheckpointGuardError(RuntimeError):
    pass


def fatal(marker, code):
    try:
        print(json.dumps(marker), flush=True)
    finally:
        os._exit(code)  # Also fail closed if writing the error marker itself fails.


def worker():
    path, kind, boundary, guard, expected_root = sys.argv[1:]
    if kind not in ('outbox', 'ingest', 'authority') or boundary not in ('mid_batch', 'before_commit', 'after_return'):
        raise ValueError('invalid process scenario')
    expected_root = Path(expected_root).resolve()
    for module in (outbox, projection):
        if Path(module.__file__).resolve().parent != expected_root / 'sprintctl':
            raise RuntimeError('unexpected product import path')
    conn = outbox.open_outbox(Path(path)) if kind == 'outbox' else projection.open_cached_projection(Path(path), repo_id=REPO)
    metadata = {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'executable': sys.executable,
        'imports': {m.__name__: str(Path(m.__file__).resolve()) for m in (outbox, projection)},
        'journal_mode': conn.execute('PRAGMA journal_mode').fetchone()[0],
        'synchronous': conn.execute('PRAGMA synchronous').fetchone()[0]}
    armed, insert_count = True, 0

    def pause(result=None):
        # A marker is emitted only after collecting the actual owner state.
        marker = {'status': 'ready', 'pid': os.getpid(), 'kind': kind, 'boundary': boundary,
            'in_transaction': conn.in_transaction, 'state': state(conn, kind),
            'returned_result': result, 'runtime': metadata}
        print(json.dumps(marker, sort_keys=True), flush=True)
        while True:
            signal.pause()

    def trace(statement):
        nonlocal armed, insert_count
        if not armed:
            return
        normalized = ' '.join(statement.split()).upper()
        selected = boundary == 'before_commit' and normalized == 'COMMIT'
        table = 'CACHED_INGEST_RECORD' if kind == 'ingest' else 'CACHED_AUTHORITY_DECISION'
        if boundary == 'mid_batch' and normalized.startswith('INSERT INTO ' + table + ' '):
            insert_count += 1
            selected = insert_count == 2  # First INSERT completed; second has not run.
        if not selected:
            return
        armed = False  # Owner-state SELECTs must not recursively re-arm this callback.
        try:
            if guard == 'guard-error':
                raise CheckpointGuardError('injected checkpoint guard error')
            pause()
        except BaseException as exc:
            # sqlite trace exceptions may be swallowed. Terminate without returning
            # to SQLite: neither sys.exit nor raising alone is an adequate guard.
            fatal({'status': 'error', 'pid': os.getpid(), 'kind': kind,
                'boundary': boundary, 'callback_fired': True, 'in_transaction': conn.in_transaction,
                'error_type': type(exc).__name__, 'error': str(exc)}, 73)

    conn.set_trace_callback(trace)
    result = apply(conn, kind)
    armed = False
    if boundary != 'after_return':
        raise RuntimeError('requested checkpoint was not reached')
    pause(result)


if __name__ == '__main__':
    try:
        worker()
    except BaseException as exc:
        fatal({'status': 'error', 'error_type': type(exc).__name__, 'error': str(exc)}, 74)
