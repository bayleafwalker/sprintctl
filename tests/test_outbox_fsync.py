"""Owned Linux/x86_64 syscall histories, not power-loss durability proof."""
import hashlib
import json
from pathlib import Path
import platform
import signal
import time

import pytest

if platform.system() != 'Linux' or platform.machine() != 'x86_64':
    pytest.skip('owned ptrace syscall ABI campaign supports Linux x86_64 only',
                allow_module_level=True)

from sprintctl import outbox
from tests._outbox_fsync_probe import (
    ObserverRefused, StallMarkerRefused, bounded_regular, candidate, check_full,
    copy_state, decode, decode_mount_field,
    repair_checksums, run_observer, run_outbox,
)
from tests._outbox_fsync_worker import append
from tests._syscall_sync_probe import run

KINDS = ['observation', 'authority-command']
REFUSALS = ['wrong_pid','wrong_fd','wrong_inode','symlink','preexisting_thread',
    'fd_reopen_attempt','dup2_attempt','unexpected_signal','unexpected_fork',
    'timeout','wrong_arch','unmatched_exit','duplicate_entry','failed_sync_result',
    'malformed_marker','coalesced_marker','unterminated_marker','duplicate_arm',
    'done_before_arm','wrong_length','large_length','unknown_op','x32',
    'restarted_sync','missing_sync','kernel_sync_error','controller_arm',
    'controller_entry','controller_exit','cleanup_esrch','cleanup_eintr','early_exit']


REFUSAL_REASONS = {'wrong_pid': {'type': 'AssertionError', 'reason': 'wrong marker PID'},
 'wrong_fd': {'type': 'FileNotFoundError', 'reason': 'No such file or directory'},
 'wrong_inode': {'type': 'AssertionError', 'reason': 'child inode differs from parent binding'},
 'symlink': {'type': 'AssertionError', 'reason': 'expected path is not regular file'},
 'preexisting_thread': {'type': 'AssertionError',
                        'reason': 'unsupported preexisting/concurrent thread'},
 'fd_reopen_attempt': {'type': 'AssertionError', 'reason': 'watched descriptor lifetime change'},
 'dup2_attempt': {'type': 'AssertionError', 'reason': 'watched descriptor lifetime change'},
 'unexpected_signal': {'type': 'AssertionError', 'reason': 'unexpected signal/event stop'},
 'unexpected_fork': {'type': 'AssertionError', 'reason': 'fork/clone/exec is unsupported'},
 'timeout': {'type': 'TimeoutError', 'reason': 'bounded own-child wait expired'},
 'wrong_arch': {'type': 'AssertionError', 'reason': 'unsupported syscall ABI'},
 'unmatched_exit': {'type': 'AssertionError',
                    'reason': 'unmatched syscall exit; bootstrap allowance removed'},
 'duplicate_entry': {'type': 'AssertionError', 'reason': 'unmatched syscall entry'},
 'failed_sync_result': {'type': 'AssertionError', 'reason': 'sync did not return successfully'},
 'malformed_marker': {'type': 'JSONDecodeError',
                      'reason': 'Expecting value: line 1 column 1 (char 0)'},
 'coalesced_marker': {'type': 'AssertionError', 'reason': 'ambiguous marker frame'},
 'unterminated_marker': {'type': 'AssertionError', 'reason': 'ambiguous marker frame'},
 'duplicate_arm': {'type': 'AssertionError', 'reason': 'duplicate or out-of-phase ARM'},
 'done_before_arm': {'type': 'AssertionError', 'reason': 'duplicate or out-of-phase DONE'},
 'wrong_length': {'type': 'AssertionError', 'reason': 'unsupported syscall info length'},
 'large_length': {'type': 'AssertionError', 'reason': 'unsupported syscall info length'},
 'unknown_op': {'type': 'AssertionError', 'reason': 'unknown/seccomp syscall stop'},
 'x32': {'type': 'AssertionError', 'reason': 'x32/unsupported syscall ABI'},
 'restarted_sync': {'type': 'AssertionError', 'reason': 'sync did not return successfully'},
 'missing_sync': {'type': 'AssertionError', 'reason': 'expected sync sequence missing'},
 'kernel_sync_error': {'type': 'AssertionError', 'reason': 'sync did not return successfully'},
 'controller_arm': {'type': 'RuntimeError', 'reason': 'injected controller exception at ARM'},
 'controller_entry': {'type': 'RuntimeError', 'reason': 'injected controller exception at entry'},
 'controller_exit': {'type': 'RuntimeError', 'reason': 'injected controller exception at exit'},
 'cleanup_esrch': {'type': 'RuntimeError', 'reason': 'injected controller exception at ARM'},
 'cleanup_eintr': {'type': 'RuntimeError', 'reason': 'injected controller exception at ARM'},
 'early_exit': {'type': 'AssertionError', 'reason': 'missing initial SIGSTOP'}}

def retain(path, result):
    path.write_text(json.dumps(result, indent=2) + '\n')


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('mode', ['observe', 'before', 'after'])
def test_kernel_boundary(tmp_path, kind, mode, record_property):
    result = run_outbox(tmp_path, kind, mode)
    retain(tmp_path / 'history.json', result)
    assert result['trace']['returncode'] == (0 if mode == 'observe' else -signal.SIGKILL)
    assert len([e for e in result['trace']['events'] if e.get('eligible')]) == 1
    record_property('history_path', str(tmp_path / 'history.json'))


@pytest.mark.parametrize('variant', ['seed_sync', 'repeat_sync'])
def test_old_or_repeated_sync_is_not_a_new_target(tmp_path, variant):
    result = run_outbox(tmp_path, 'observation', 'observe', variant)
    retain(tmp_path / 'history.json', result)


def test_valid_unrelated_regular_file_is_refused(tmp_path):
    cleanup = []
    with pytest.raises(AssertionError, match='unexpected sync FD'):
        run_outbox(tmp_path, 'observation', 'observe', 'wrong_fd', cleanup)
    assert len(cleanup) == 1 and cleanup[0]['actual_sigkill']
    raw = cleanup[0]['raw_sync_entries']
    assert len(raw) == 1 and raw[0]['regular']
    assert raw[0]['path'] == str(tmp_path / 'other-regular')
    retain(tmp_path / 'refusal.json', cleanup)


@pytest.mark.parametrize('number', [74, 75])
@pytest.mark.parametrize('mode', ['observe', 'before', 'after'])
def test_generic_kernel_capability(tmp_path, number, mode):
    result = run(mode, target=number)
    assert result['returncode'] == (0 if mode == 'observe' else -signal.SIGKILL)
    retain(tmp_path / 'history.json', result)


@pytest.mark.parametrize('variant', REFUSALS)
def test_controller_refuses_invalid_history(tmp_path, variant):
    cleanup = []
    expected = REFUSAL_REASONS[variant]
    with pytest.raises((AssertionError, TimeoutError, RuntimeError, OSError, ValueError)) as caught:
        run('observe', variant, cleanup)
    assert type(caught.value).__name__ == expected['type']
    assert expected['reason'] in str(caught.value), 'unrelated guard refusal'
    assert len(cleanup) == 1 and not cleanup[0]['cleanup_errors']
    assert cleanup[0]['cleanup_fault_injected'] is (variant in ('cleanup_esrch', 'cleanup_eintr'))
    if variant in ('early_exit', 'missing_sync'):
        assert cleanup[0]['returncode'] == (17 if variant == 'early_exit' else 0)
    else:
        assert cleanup[0]['actual_sigkill']
    if variant == 'kernel_sync_error':
        exits = [e for e in cleanup[0]['observed_sync_events'] if e['phase'] == 'exit']
        assert len(exits) == 1
        assert (exits[0]['return'], exits[0]['is_error'], exits[0]['observation_source']) == (-9, 1, 'kernel')
    retain(tmp_path / 'refusal.json', {'variant': variant, 'cleanup': cleanup})


@pytest.mark.parametrize('fault', ['stall-copy', 'stall-retry'])
def test_observer_deadline_does_not_steal_tracee_wait(tmp_path, fault):
    cleanup = []
    started = time.monotonic()
    with pytest.raises(ObserverRefused, match='deadline') as caught:
        run_outbox(tmp_path, 'observation', 'after', cleanup_records=cleanup,
                   observer_fault=fault)
    assert caught.value.evidence['returncode'] == -signal.SIGKILL
    assert caught.value.evidence['role'] == fault
    assert caught.value.evidence['stall_marker'] == caught.value.evidence['expected_stall_marker']
    assert caught.value.evidence['phase'] == (
        'candidate-copy' if fault == 'stall-copy' else 'post-terminal-post-recovery-retry')
    assert time.monotonic() - started < 10
    assert len(cleanup) == 1 and cleanup[0]['actual_sigkill']
    assert not cleanup[0]['cleanup_errors']
    retain(tmp_path / 'observer-refusal.json', {'fault': fault,
           'observer': caught.value.evidence, 'tracee': cleanup})


def test_startup_deadline_reaps_producer_before_boot(tmp_path):
    cleanup = []
    with pytest.raises(TimeoutError, match='bounded own-child wait expired'):
        run_outbox(tmp_path, 'observation', 'observe', 'startup-stall', cleanup)
    assert len(cleanup) == 1 and cleanup[0]['actual_sigkill']
    assert not cleanup[0]['cleanup_errors']
    assert cleanup[0]['observed_sync_events'] == []
    assert cleanup[0]['stall_marker'] == cleanup[0]['expected_stall_marker']
    retain(tmp_path / 'startup-refusal.json', cleanup)


@pytest.mark.parametrize('fault,reason', [
    ('missing', 'missing reached-stall marker'), ('wrong-pid', 'stall marker PID'),
    ('wrong-role', 'stall marker role'), ('wrong-nonce', 'stall marker nonce')])
def test_stall_control_refuses_missing_or_mismatched_child_proof(tmp_path, fault, reason):
    with pytest.raises(StallMarkerRefused, match=reason) as caught:
        run_observer('stall-copy', tmp_path / 'copy.db', timeout=.5, marker_fault=fault,
                     phase='marker-falsifier')
    assert str(caught.value) == reason
    assert caught.value.evidence['returncode'] == -signal.SIGKILL
    retain(tmp_path / 'stall-marker-refusal.json', {'fault': fault, 'reason': reason,
           'observer': caught.value.evidence})


@pytest.mark.parametrize('fault', ['overflow', 'query-timeout'])
def test_observer_output_and_query_budgets(tmp_path, fault):
    path = tmp_path / 'outbox.db'
    conn = outbox.open_outbox(path)
    append(conn, 'observation', seed=True)
    conn.close()
    with pytest.raises(ObserverRefused) as caught:
        run_observer(fault, path)
    assert caught.value.evidence['returncode'] is not None
    if fault == 'overflow':
        assert str(caught.value) == 'observer output budget'
        assert caught.value.evidence['returncode'] == -signal.SIGKILL
    else:
        assert 'interrupted' in caught.value.evidence['stderr']


@pytest.fixture
def wal_fixture(tmp_path):
    path = tmp_path / 'original.db'
    conn = outbox.open_outbox(path)
    append(conn, 'observation', seed=True)
    seed = bounded_regular(Path(str(path) + '-wal'))[0]
    append(conn, 'observation')
    current = bounded_regular(Path(str(path) + '-wal'))[0]
    database = bounded_regular(path)[0]
    yield path, conn, seed, current, database
    conn.close()


@pytest.mark.parametrize('fault', ['old', 'header', 'repeat', 'truncated', 'frame-checksum',
    'header-checksum', 'salt', 'generation', 'uncommitted', 'two-commits'])
def test_wal_classifier_rejects_invalid_target(tmp_path, wal_fixture, fault):
    path, conn, seed, current, database = wal_fixture
    already = ()
    if fault == 'old': current = seed
    elif fault == 'header': current = current[:32]
    elif fault == 'repeat': already = (hashlib.sha256(current).hexdigest(),)
    elif fault == 'truncated': current = current[:-1]
    elif fault in ('frame-checksum', 'header-checksum', 'salt'):
        data = bytearray(current)
        offset = {'frame-checksum': len(data)-1, 'header-checksum': 24,
                  'salt': decode(current)['frames'][-1]['offset']+8}[fault]
        data[offset] ^= 1
        current = bytes(data)
    elif fault in ('generation', 'uncommitted'):
        current = repair_checksums(current, generation=fault == 'generation',
                                   uncommitted=fault == 'uncommitted')
        decode(current)
        copied, _ = copy_state(database, current, tmp_path / 'valid-mutated-copy')
        check_full(copied, 2 if fault == 'generation' else 1, 0, time.time())
    elif fault == 'two-commits':
        outbox.append_observation(conn, event_id='extra-event', event_type='work.completed',
                                  actor='verification', payload={'phase': 'extra'})
        current = bounded_regular(Path(str(path) + '-wal'))[0]
    reasons = {'old':'no new WAL prefix', 'header':'no new WAL prefix',
        'repeat':'already-synced prefix', 'truncated':'partial WAL frame',
        'frame-checksum':'WAL frame checksum', 'header-checksum':'WAL header checksum',
        'salt':'WAL frame salts', 'generation':'generation/header changed',
        'uncommitted':'new prefix lacks final commit', 'two-commits':'not exactly one new commit'}
    with pytest.raises(AssertionError, match=reasons[fault]):
        candidate(seed, current, already)


@pytest.mark.parametrize('fault', ['wrong-event', 'bad-record-hash', 'bad-next-sequence'])
def test_complete_manifest_refuses_persisted_corruption(tmp_path, fault):
    path = tmp_path / 'original.db'
    started = time.time()
    conn = outbox.open_outbox(path)
    append(conn, 'observation', seed=True)
    append(conn, 'observation')
    if fault == 'bad-next-sequence':
        conn.execute('UPDATE outbox_stream SET next_origin_seq=99')
    else:
        # Explicit fixture corruption, never a production repair or automatic product check.
        conn.execute('DROP TRIGGER outbox_record_is_immutable_update')
        column, value = ('event_id', 'unexpected-event') if fault == 'wrong-event' else ('record_sha256', '0'*64)
        conn.execute(f'UPDATE outbox_record SET {column}=? WHERE origin_seq=2', (value,))
    conn.commit()
    conn.close()
    state, _ = run_observer('recover', path)
    message = 'persisted next sequence' if fault == 'bad-next-sequence' else 'persisted authored record/provenance/hash differs'
    with pytest.raises(AssertionError, match=message):
        check_full(state, 2, started, time.time())


def test_mount_metadata_decodes_kernel_escapes_once():
    assert decode_mount_field(r'/space\040tab\011line\012slash\134040') == '/space tab\tline\nslash\\040'
