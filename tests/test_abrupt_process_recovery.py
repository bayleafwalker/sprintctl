"""Bounded actual process-death recovery, not power-loss/fsync qualification."""
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from sprintctl import outbox, projection
from tests import _process_recovery_worker as fixture

pytestmark = pytest.mark.skipif(os.name != 'posix' or not hasattr(signal, 'SIGKILL'),
    reason='actual own-child POSIX SIGKILL verification')
ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(fixture.__file__).resolve()


class RecoveryHarnessFailure(RuntimeError):
    pass


class WorkerGuardFailure(RecoveryHarnessFailure):
    def __init__(self, marker, returncode):
        super().__init__('checkpoint guard terminated before COMMIT')
        self.marker, self.returncode = marker, returncode


def read_marker(proc):
    deadline = time.monotonic() + 15
    data = b''
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline-time.monotonic()))
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise RecoveryHarnessFailure('child exited before a complete checkpoint marker')
        data += chunk
        if len(data) > 65536:
            raise RecoveryHarnessFailure('checkpoint marker exceeded bound')
        if b'\n' in data:
            try:
                return json.loads(data.split(b'\n', 1)[0])
            except (UnicodeError, ValueError) as exc:
                raise RecoveryHarnessFailure('invalid child checkpoint marker') from exc
    raise RecoveryHarnessFailure('checkpoint readiness deadline exceeded')


def kill_at_checkpoint(path, kind, boundary, guard='normal'):
    # No shell or process-group kill: only the exact child this fixture creates.
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    proc = subprocess.Popen([sys.executable, str(WORKER), str(path), kind, boundary, guard, str(ROOT)],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        marker = read_marker(proc)
        if marker.get('status') == 'error':
            code = proc.wait(timeout=10)
            if marker.get('error_type') == 'CheckpointGuardError' and code == 73:
                raise WorkerGuardFailure(marker, code)
            raise RecoveryHarnessFailure(f'worker error {marker}; exit={code}')
        if (marker.get('status'), marker.get('pid'), marker.get('kind'), marker.get('boundary')) != (
                'ready', proc.pid, kind, boundary):
            raise RecoveryHarnessFailure('checkpoint identity/boundary mismatch')
        proc.kill()
        code = proc.wait(timeout=10)
        if code != -signal.SIGKILL:
            raise RecoveryHarnessFailure(f'child did not exit by SIGKILL: {code}')
        return {'marker': marker, 'returncode': code, 'stderr': proc.stderr.read().decode('utf-8', errors='replace')}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


def assert_checkpoint(kind, boundary, marker, seed):
    assert marker['in_transaction'] is (boundary != 'after_return')
    runtime = marker['runtime']
    assert runtime['python'] and runtime['sqlite'] == sqlite3.sqlite_version
    assert Path(runtime['executable']).resolve() == Path(sys.executable).resolve()
    assert set(runtime['imports']) == {'sprintctl.outbox', 'sprintctl.projection'}
    assert all(Path(p).resolve().parent == ROOT / 'sprintctl' for p in runtime['imports'].values())
    assert runtime['journal_mode'] and type(runtime['synchronous']) is int
    assert_manifest(kind, boundary, marker['state'], seed)

    if boundary == 'after_return':
        assert marker['returned_result'] is not None
        if kind == 'outbox':
            assert marker['returned_result']['event_id'] == 'new-event'
            assert marker['returned_result']['origin_seq'] == 2
        else:
            assert marker['returned_result']['ingest_offset'] == (3 if kind == 'ingest' else 13)
    else:
        assert marker['returned_result'] is None


def assert_manifest(kind, boundary, actual, seed):
    if kind == 'outbox':
        assert [r['event_id'] for r in actual['records']] == ['seed-event', 'new-event']
        assert actual['records'][0] == seed['records'][0]
        assert [r['origin_seq'] for r in actual['records']] == [1, 2]
        assert actual['stream'][0]['next_origin_seq'] == 3
        assert actual['stream'][0]['origin_stream_id'] == seed['stream'][0]['origin_stream_id']
        assert json.loads(actual['records'][1]['payload']) == {'phase': 'new'}
    else:
        own, other = ('ingest', 'authority') if kind == 'ingest' else ('authority', 'ingest')
        field = 'ingest_offset' if own == 'ingest' else 'decision_ingest_offset'
        expected_offsets = ([1, 2] if boundary == 'mid_batch' else [1, 2, 3]) if own == 'ingest' else (
            [3, 8] if boundary == 'mid_batch' else [3, 8, 13])
        assert [r[field] for r in actual[own]] == expected_offsets
        assert actual[own][0] == seed[own][0]
        cursor = actual[own + '_cursor'][0]
        assert cursor[field] == (seed[own + '_cursor'][0][field] if boundary == 'mid_batch' else expected_offsets[-1])
        assert actual[other] == seed[other] and actual[other + '_cursor'] == seed[other + '_cursor']
        assert actual['meta'] == seed['meta']
        for row, offset in zip(actual[own], expected_offsets):
            data = json.loads(row['record_json' if own == 'ingest' else 'decision_json'])
            assert data == ({'event_id': f'ingest-{offset}', 'value': offset} if own == 'ingest' else
                {'request_event_id': f'request-{offset}', 'outcome': 'accepted'})

SCENARIOS = [('outbox', 'before_commit'), ('outbox', 'after_return'),
    *[(kind, boundary) for kind in ('ingest', 'authority') for boundary in ('mid_batch', 'before_commit', 'after_return')]]


@pytest.mark.parametrize('kind,boundary', SCENARIOS)
def test_sigkill_recovers_exact_committed_state_and_retry(tmp_path, kind, boundary, record_property):
    path = tmp_path / 'owned.sqlite'
    fixture.seed(path, kind)  # The seeding connection is closed before spawning.
    seed = fixture.raw_recover(path, kind)  # This observer is closed before spawning too.
    killed = kill_at_checkpoint(path, kind, boundary)
    marker = killed['marker']
    assert_checkpoint(kind, boundary, marker, seed)
    # First observation after reap performs normal SQLite automatic recovery.
    # No product helper has reopened the database, and sidecars are untouched.
    recovered = fixture.raw_recover(path, kind)
    expected = marker['state'] if boundary == 'after_return' else seed
    fixture.check_recovered(kind, recovered, expected)
    conn = outbox.open_outbox(path) if kind == 'outbox' else projection.open_cached_projection(path, repo_id=fixture.REPO)
    try:
        result = fixture.apply(conn, kind)
        final = fixture.state(conn, kind)
        assert_manifest(kind, 'after_return', final, seed)
        if kind == 'outbox':
            assert [r['event_id'] for r in final['records']] == ['seed-event', 'new-event']
            assert final['stream'][0]['next_origin_seq'] == 3
            assert result['event_id'] == 'new-event' and result['origin_seq'] == 2
            assert fixture.apply(conn, kind) == result
            assert fixture.state(conn, kind) == final
            extra = fixture.append(conn, 'next-event', 'next')
            assert extra.origin_seq == 3
            next_state = fixture.state(conn, kind)
            assert [r['event_id'] for r in next_state['records']] == ['seed-event', 'new-event', 'next-event']
            assert [r['origin_seq'] for r in next_state['records']] == [1, 2, 3]
            assert next_state['stream'][0]['next_origin_seq'] == 4
        else:
            assert result['ingest_offset'] == (3 if kind == 'ingest' else 13)
            assert fixture.apply(conn, kind) == result
            assert fixture.state(conn, kind) == final
            for key in (('authority', 'authority_cursor') if kind == 'ingest' else ('ingest', 'ingest_cursor')):
                assert final[key] == seed[key]
        fixture.check_recovered(kind, final, final)
    finally:
        conn.close()
    record_property('state_history', json.dumps({'kind': kind, 'boundary': boundary, 'seed': seed,
        'actual_child': killed, 'first_raw_automatic_recovery': recovered,
        'retry_result': result, 'after_retry': final}, sort_keys=True))


@pytest.mark.parametrize('kind,message', [('outbox', 'outbox next sequence'),
    ('ingest', 'ingest watermark'), ('authority', 'authority watermark')])
def test_same_raw_checker_rejects_persisted_invariant_violation(tmp_path, kind, message, record_property):
    path = tmp_path / 'negative-control.sqlite'
    fixture.seed(path, kind)
    valid = fixture.raw_recover(path, kind)
    fixture.check_recovered(kind, valid, valid)  # Prove the valid fixture passes first.
    with sqlite3.connect(str(path)) as conn:
        if kind == 'outbox':
            conn.execute('UPDATE outbox_stream SET next_origin_seq=99')
        elif kind == 'ingest':
            conn.execute('UPDATE cached_ingest_watermark SET ingest_offset=99')
        else:
            conn.execute('UPDATE cached_authority_watermark SET decision_ingest_offset=99')
    conn.close()
    corrupt = fixture.raw_recover(path, kind)
    with pytest.raises(fixture.RecoveryInvariantFailure, match=message):
        fixture.check_recovered(kind, corrupt, valid)
    record_property('state_history', json.dumps({'negative_control': kind, 'valid': valid,
        'actual_corrupt_stored_state': corrupt, 'specific_detected_violation': message}, sort_keys=True))


def test_checker_rejects_logically_consistent_partial_new_batch(tmp_path, record_property):
    path = tmp_path / 'partial-negative-control.sqlite'
    fixture.seed(path, 'ingest')
    conn = projection.open_cached_projection(path, repo_id=fixture.REPO)
    fixture.apply(conn, 'ingest')
    conn.close()
    complete = fixture.raw_recover(path, 'ingest')
    fixture.check_recovered('ingest', complete, complete)
    with sqlite3.connect(str(path)) as conn:
        conn.execute('DELETE FROM cached_ingest_record WHERE ingest_offset=3')
        conn.execute('UPDATE cached_ingest_watermark SET ingest_offset=2')
    conn.close()
    partial = fixture.raw_recover(path, 'ingest')
    with pytest.raises(fixture.RecoveryInvariantFailure, match='exact committed manifest'):
        fixture.check_recovered('ingest', partial, complete)
    record_property('state_history', json.dumps({'negative_control': 'partial-new-batch',
        'complete': complete, 'actual_partial_stored_state': partial,
        'specific_detected_violation': 'exact committed manifest'}, sort_keys=True))


def test_trace_guard_exception_terminates_instead_of_silently_committing(tmp_path, record_property):
    path = tmp_path / 'guard-negative-control.sqlite'
    fixture.seed(path, 'outbox')
    seed = fixture.raw_recover(path, 'outbox')
    with pytest.raises(WorkerGuardFailure, match='checkpoint guard') as caught:
        kill_at_checkpoint(path, 'outbox', 'before_commit', guard='guard-error')
    assert caught.value.returncode == 73
    assert caught.value.marker['callback_fired'] and caught.value.marker['in_transaction']
    recovered = fixture.raw_recover(path, 'outbox')
    fixture.check_recovered('outbox', recovered, seed)
    record_property('state_history', json.dumps({'negative_control': 'trace-guard-error',
        'actual_error_marker': caught.value.marker, 'actual_exit': caught.value.returncode,
        'recovered': recovered, 'seed': seed}, sort_keys=True))
