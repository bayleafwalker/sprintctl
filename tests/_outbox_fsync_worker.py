"""Owned fixture operations. Authority commands here are local requests only."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sprintctl import contracts, outbox
from tests._outbox_fsync_probe import FIXTURE, MAX_BYTES, full_state


def mark_stall(role, request):
    """Child-authored proof that the deliberate stall branch was reached."""
    fault = request.get('fault')
    if fault == 'missing':
        return
    value = {'pid': os.getpid(), 'role': role, 'nonce': request['nonce']}
    if fault == 'wrong-pid': value['pid'] += 1
    if fault == 'wrong-role': value['role'] = 'unrequested-role'
    if fault == 'wrong-nonce': value['nonce'] = 'unrequested-nonce'
    encoded = json.dumps(value).encode()
    assert len(encoded) < 1024
    descriptor = os.open(request['path'], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        assert os.write(descriptor, encoded) == len(encoded)
    finally:
        os.close(descriptor)


def append(conn, kind, *, seed=False):
    if seed or kind == 'observation':
        return outbox.append_observation(
            conn, event_id='seed-event' if seed else 'target-event',
            event_type='work.completed', actor='verification',
            runtime_session_id='fsync-format-probe',
            occurred_at='2026-10-18T00:00:00Z',
            payload={'phase': 'seed' if seed else 'target'})
    assert kind == 'authority-command'
    values = {key: value for key, value in FIXTURE['command_envelope'].items()
              if key != 'record_class'}
    return outbox.append_authority_command(
        conn, contracts.AuthorityCommand(**values),
        runtime_session_id='fsync-format-probe')


def read_state(path, target_id, *, omission=False, expensive=False):
    conn = sqlite3.connect(path, timeout=.2)
    deadline = time.monotonic() + .5
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
    try:
        if expensive:
            conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'outbox_record' not in names:
            assert omission, 'WAL copy lost expected outbox schema'
            return {'status': 'no-outbox-schema', 'target_absent': True, 'tables': sorted(names)}
        result = full_state(conn)
        result['status'] = 'readable-outbox'
        result['target_absent'] = all(r['event_id'] != target_id for r in result['records'])
        return result
    finally:
        conn.close()


import hashlib
import signal
from tests._syscall_sync_probe import ptrace, emit

def producer(path, variant, record_kind, metadata, stall_marker=None):
    if variant == 'startup-stall':
        mark_stall('startup-stall', stall_marker)
        while True:
            time.sleep(1)
    assert variant in ('normal','seed_sync','repeat_sync','wrong_fd')
    other_fd=None
    conn=outbox.open_outbox(path)
    try:
        append(conn,record_kind,seed=True)
        assert not conn.in_transaction
        wal=Path(str(path)+'-wal')
        descriptors=[]
        for name in os.listdir('/proc/self/fd'):
            try:
                if os.readlink('/proc/self/fd/'+name)==str(wal): descriptors.append(int(name))
            except FileNotFoundError: pass
        assert len(descriptors)==1, descriptors
        fd=descriptors[0]
        info=os.fstat(fd)
        if variant=='wrong_fd':
            other_fd=os.open(path.parent/'other-regular',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
            os.write(other_fd,b'owned valid unrelated regular file')
        runtime={'record_kind':record_kind,'fixture_variant':variant,**metadata,
            'outbox_path':outbox.__file__,'outbox_sha256':hashlib.sha256(Path(outbox.__file__).read_bytes()).hexdigest(),
            'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,
            'source_id':conn.execute('SELECT sqlite_source_id()').fetchone()[0],
            'compile_options':[r[0] for r in conn.execute('PRAGMA compile_options')],
            'journal_mode':conn.execute('PRAGMA journal_mode').fetchone()[0],
            'synchronous':conn.execute('PRAGMA synchronous').fetchone()[0],
            'wal_autocheckpoint':conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0],
            'database_list':[list(r) for r in conn.execute('PRAGMA database_list')],
            'authored_manifest_sha256':hashlib.sha256((ROOT / 'verification/fixtures/outbox-fsync-expected-records.json').read_bytes()).hexdigest(),
            'vfs_name':{'status':'unknown','reason':'static builtin SQLite exposes no supported Python connection VFSNAME diagnostic; no private heap inspection or extension used'},
            'platform':list(os.uname())}
        runtime['executable'] = sys.executable
        runtime['sqlite_extension_path'] = getattr(__import__('_sqlite3'), '__file__', None)
        runtime['source_manifest'] = {}
        for name, module in tuple(sys.modules.items()):
            filename = getattr(module, '__file__', None)
            if filename is None:
                continue
            source = Path(filename).resolve()
            if source.is_relative_to(ROOT) and source.suffix == '.py':
                runtime['source_manifest'][name] = {
                    'path': str(source.relative_to(ROOT)),
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        runtime_path=path.parent/'runtime.json'
        runtime_path.write_text(json.dumps(runtime,indent=2)+'\n')
        ptrace(0,0)
        os.kill(os.getpid(),signal.SIGSTOP)
        emit({'phase':'BOOT','pid':os.getpid(),'fd':fd,'path':str(wal),
            'device':info.st_dev,'inode':info.st_ino,
            'runtime_sha256':hashlib.sha256(runtime_path.read_bytes()).hexdigest()})
        emit({'phase':'ARM','pid':os.getpid()})
        if variant=='wrong_fd': os.fsync(other_fd)
        if variant=='seed_sync': os.fsync(fd)
        append(conn,record_kind)
        if variant=='repeat_sync': os.fsync(fd)
        emit({'phase':'DONE','pid':os.getpid()})
    finally:
        conn.close()
        if other_fd is not None: os.close(other_fd)


def main(role, request):
    if role == 'producer':
        return producer(Path(request['path']),request['variant'],request['kind'],request['metadata'],
                        request.get('stall_marker'))
    if role in ('stall-copy', 'stall-retry'):
        mark_stall(role, request['stall_marker'])
        while True:
            time.sleep(1)
    if role == 'overflow':
        while True:
            os.write(1, b'x' * 4096)
    assert role in ('copy', 'recover', 'retry', 'query-timeout')
    path = Path(request['path'])
    kind = request['kind']
    target_id = FIXTURE['records'][kind]['event_id']
    if role == 'retry':
        # Product retains normal settings; parent wall-clock deadline bounds open/append.
        conn = outbox.open_outbox(path)
        try:
            record = append(conn, kind)
            result = {'origin_seq': record.origin_seq, 'origin_stream_id': record.origin_stream_id}
        finally:
            conn.close()
    else:
        result = read_state(path, target_id, omission=request.get('omission', False),
                            expensive=role == 'query-timeout')
    encoded = json.dumps(result)
    assert len(encoded.encode()) <= MAX_BYTES
    print(encoded)


if __name__ == '__main__':
    assert len(sys.argv) == 3
    main(sys.argv[1], json.loads(sys.argv[2]))
