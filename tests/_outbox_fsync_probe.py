"""Independent WAL and persisted-record oracles for owned process tests."""

import hashlib
import json
from pathlib import Path
import struct
from datetime import datetime, timezone
import os
import re
import sqlite3
import stat
import time
from uuid import UUID
import selectors
import signal
import subprocess

def checksum(data, order, initial=(0, 0)):
    assert len(data) % 8 == 0, 'checksum input alignment'
    words = struct.unpack(order + str(len(data)//4) + 'I', data)
    a, b = initial
    for index in range(0, len(words), 2):
        a = (a + words[index] + b) & 0xffffffff
        b = (b + words[index+1] + a) & 0xffffffff
    return a, b


def decode(raw):
    assert len(raw) >= 32, 'incomplete WAL header'
    header = struct.unpack_from('>8I', raw)
    magic, version, page, checkpoint, salt1, salt2, c1, c2 = header
    assert magic in (0x377f0682,0x377f0683), 'WAL magic'
    assert version == 3007000, 'unsupported WAL version'
    assert 512 <= page <= 65536 and page & (page-1) == 0, 'WAL page size'
    order = '<' if magic == 0x377f0682 else '>'
    assert checksum(raw[:24],order) == (c1,c2), 'WAL header checksum'
    step = 24 + page
    assert (len(raw)-32) % step == 0, 'partial WAL frame'
    previous = c1,c2
    commits=[]
    frames=[]
    for offset in range(32,len(raw),step):
        frame=raw[offset:offset+step]
        number, pages, f1, f2, a, b=struct.unpack_from('>6I',frame)
        assert number > 0, 'invalid WAL page number'
        assert (f1,f2)==(salt1,salt2), 'WAL frame salts'
        previous=checksum(frame[:8]+frame[24:],order,previous)
        assert previous==(a,b), 'WAL frame checksum'
        index=(offset-32)//step+1
        frames.append({'index':index,'offset':offset,'page':number,'commit_db_pages':pages})
        if pages: commits.append(index)
    return {'bytes':len(raw),'magic':magic,'version':version,'page_size':page,
            'generation':[checkpoint,salt1,salt2], 'frames':frames,'commits':commits,
            'last_commit':max(commits,default=0),
            'sha256':hashlib.sha256(raw).hexdigest()}


def candidate(seed, current, already_synced=()):
    old,new=decode(seed),decode(current)
    assert old['last_commit']==len(old['frames']) and old['frames'], 'seed not fully committed'
    assert current[:32]==seed[:32], 'generation/header changed'
    assert len(current)>len(seed), 'no new WAL prefix'
    assert current.startswith(seed), 'seed prefix rewritten'
    assert new['last_commit']==len(new['frames']), 'new prefix lacks final commit'
    assert [i for i in new['commits'] if i>len(old['frames'])]==[new['last_commit']], 'not exactly one new commit'
    assert new['sha256'] not in already_synced, 'already-synced prefix'
    return new


def repair_checksums(raw, *, generation=False, uncommitted=False):
    data=bytearray(raw)
    page=struct.unpack_from('>I',data,8)[0]
    step=24+page
    if generation:
        checkpoint,s1,s2=struct.unpack_from('>3I',data,12)
        struct.pack_into('>3I',data,12,checkpoint+1,s1^1,s2^2)
    order='<' if struct.unpack_from('>I',data)[0]==0x377f0682 else '>'
    previous=checksum(bytes(data[:24]),order)
    struct.pack_into('>2I',data,24,*previous)
    salts=bytes(data[16:24])
    for offset in range(32,len(data),step):
        data[offset+8:offset+16]=salts
        if uncommitted and offset+step==len(data): struct.pack_into('>I',data,offset+4,0)
        previous=checksum(bytes(data[offset:offset+8]+data[offset+24:offset+step]),order,previous)
        struct.pack_into('>2I',data,offset+16,*previous)
    return bytes(data)




MAX_BYTES=2*1024*1024
FIXTURE_PATH = Path(__file__).resolve().parents[1] / 'verification/fixtures/outbox-fsync-expected-records.json'
FIXTURE = json.loads(FIXTURE_PATH.read_text())
EXPECTED = FIXTURE['records']


def bounded_regular(path):
    path=Path(path)
    assert stat.S_ISREG(path.lstat().st_mode), 'raw capture path is not regular'
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        assert stat.S_ISREG(before.st_mode) and 0<before.st_size<=MAX_BYTES, 'raw capture byte bound'
        raw=os.pread(fd,before.st_size,0)
        after=os.fstat(fd)
        assert len(raw)==before.st_size and (before.st_dev,before.st_ino,before.st_size)==(after.st_dev,after.st_ino,after.st_size), 'unstable raw capture'
        return raw,{'path':str(path),'device':before.st_dev,'inode':before.st_ino,
                    'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    finally: os.close(fd)


def full_state(conn):
    conn.row_factory=sqlite3.Row
    return {'records':[dict(r) for r in conn.execute('SELECT * FROM outbox_record ORDER BY origin_seq')],
            'streams':[dict(r) for r in conn.execute('SELECT * FROM outbox_stream ORDER BY singleton')]}


def check_full(state, count, earliest, latest, stream_id=None, record_kind="observation"):
    assert count in (1,2)
    assert len(state['records'])==count and len(state['streams'])==1, 'record/stream population'
    stream=state['streams'][0]
    assert set(stream)=={'singleton','origin_stream_id','next_origin_seq','created_at'}, 'stream schema'
    assert type(stream['singleton']) is int and stream['singleton']==1
    assert type(stream['next_origin_seq']) is int and stream['next_origin_seq']==count+1, 'persisted next sequence'
    identity=stream['origin_stream_id']
    assert type(identity) is str and str(UUID(identity))==identity and UUID(identity).version==4, 'stream UUID representation'
    if stream_id is not None: assert identity==stream_id, 'stream identity changed'
    for phase,row in zip(['seed',record_kind],state['records']):
        expected=EXPECTED[phase]
        assert set(row)==set(expected)|{'origin_stream_id','created_at'}, 'record schema'
        assert {key:row[key] for key in expected}==expected, 'persisted authored record/provenance/hash differs'
        assert type(row['origin_seq']) is int and type(row['schema_version']) is int
        assert row['origin_stream_id']==identity, 'record stream identity'
        stamp=row['created_at']
        assert type(stamp) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',stamp), 'created_at representation'
        instant=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
        assert earliest-1<=instant<=latest+1, 'created_at outside actual process interval'
    assert stream['created_at']==state['records'][0]['created_at'], 'seed stream provenance changed'
    return identity




class ObserverRefused(RuntimeError):
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


def observe(arguments, *, timeout=2, output_limit=65536, parse_json=True):
    """Own exactly this subprocess; never wait on another process's PID."""
    started = time.monotonic()
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    failure = None
    evidence = {"pid": process.pid, "timeout": timeout, "output_limit": output_limit}
    selector = selectors.DefaultSelector()
    try:
        for name in buffers:
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        deadline = started + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "observer wall-clock deadline"
                break
            for key, _ in selector.select(min(remaining, .05)):
                chunk = os.read(key.fd, min(8192, output_limit + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if sum(map(len, buffers.values())) + len(chunk) > output_limit:
                    failure = "observer output budget"
                    break
                buffers[key.data].extend(chunk)
            if failure:
                break
        if failure is None:
            try:
                process.wait(timeout=max(.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "observer terminal deadline"
    finally:
        # This owner alone waits for process.pid. No waitpid(-1), no tracee waits.
        try:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        evidence.update(returncode=process.returncode,
                        elapsed=time.monotonic()-started,
                        captured_bytes=sum(map(len, buffers.values())),
                        stdout=bytes(buffers['stdout']).decode(errors='replace'),
                        stderr=bytes(buffers['stderr']).decode(errors='replace'))
    if failure is not None or process.returncode != 0:
        raise ObserverRefused(failure or "observer nonzero exit", evidence)
    output = bytes(buffers['stdout'])
    return (json.loads(output) if parse_json else output.decode()), evidence


class StallMarkerRefused(AssertionError):
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


def stall_marker_request(path, role, fault=None):
    marker = Path(path).parent / ('.' + Path(path).name + '.' + role + '.reached.json')
    assert not marker.exists()
    return {'path': str(marker), 'nonce': os.urandom(16).hex(), 'fault': fault}


def verify_stall_marker(request, role, evidence):
    # Called only after the owner's actual terminal wait/reap, including refusals.
    assert evidence['returncode'] is not None, 'marker read before terminal reap'
    evidence['expected_stall_marker'] = {'pid': evidence['pid'], 'role': role, 'nonce': request['nonce']}
    try:
        raw, identity = bounded_regular(request['path'])
    except FileNotFoundError as error:
        raise StallMarkerRefused('missing reached-stall marker', evidence) from error
    assert len(raw) <= 1024, 'stall marker byte budget'
    value = json.loads(raw)
    evidence['stall_marker'] = value
    evidence['stall_marker_identity'] = identity
    if type(value) is not dict or set(value) != {'pid', 'role', 'nonce'}:
        raise StallMarkerRefused('stall marker schema', evidence)
    for key, label in [('pid', 'PID'), ('role', 'role'), ('nonce', 'nonce')]:
        expected = evidence['expected_stall_marker'][key]
        if type(value[key]) is not type(expected) or value[key] != expected:
            raise StallMarkerRefused('stall marker ' + label, evidence)


def run_observer(role, path, record_kind='observation', *, omission=False, timeout=3, phase=None,
                 marker_fault=None):
    import sys
    worker = Path(__file__).with_name('_outbox_fsync_worker.py')
    request = {'path': str(path), 'kind': record_kind, 'omission': omission}
    marker = None
    if role in ('stall-copy', 'stall-retry'):
        marker = stall_marker_request(path, role, marker_fault)
        request['stall_marker'] = marker
    try:
        value, evidence = observe([sys.executable, '-B', str(worker), role, json.dumps(request)],
                                  timeout=timeout, output_limit=65536)
    except ObserverRefused as error:
        error.evidence.update(role=role, path=str(path), phase=phase)
        if marker is not None:
            verify_stall_marker(marker, role, error.evidence)
        raise
    evidence.update(role=role, path=str(path), phase=phase)
    return value, evidence


def copy_state(database, wal, destination, record_kind='observation', *, role='copy'):
    assert 0 < len(database) <= MAX_BYTES
    assert wal is None or 0 < len(wal) <= MAX_BYTES
    destination = Path(destination)
    destination.mkdir()
    path = destination / 'copy.db'
    path.write_bytes(database)
    if wal is not None:
        Path(str(path) + '-wal').write_bytes(wal)
    state, evidence = run_observer(role, path, record_kind, omission=wal is None)
    return state, evidence


def decode_mount_field(value):
    # Decode each /proc mountinfo escape exactly once.
    return re.sub(r'\\(040|011|012|134)', lambda match: chr(int(match[1], 8)), value)


class OutboxPolicy:
    """Stopped-point content checks; has no process control or wait ownership."""
    boot_extra = {'runtime_sha256'}
    allow_post_done_sync = True

    def __init__(self, root, record_kind, variant='normal', observer_fault=None):
        self.root = Path(root)
        self.path = self.root / 'outbox.db'
        self.captures = self.root / 'captures'
        self.captures.mkdir()
        self.kind = record_kind
        self.variant = variant
        self.observer_fault = observer_fault
        self.started = time.time()
        self.synced = set()
        self.qualified = 0
        self.nonqualifying = []
        self.observers = []
        self.attribution = None

    def on_boot(self, marker):
        self.runtime_bytes = (self.root / 'runtime.json').read_bytes()
        assert hashlib.sha256(self.runtime_bytes).hexdigest() == marker['runtime_sha256']
        runtime = json.loads(self.runtime_bytes)
        root = Path(__file__).resolve().parents[1]
        for module in ('sprintctl.outbox', 'sprintctl.contracts', 'sprintctl.decisions',
                       'tests._outbox_fsync_probe', 'tests._syscall_sync_probe'):
            assert module in runtime['source_manifest'], 'missing transitive source pin'
        for description in runtime['source_manifest'].values():
            source = (root / description['path']).resolve()
            assert source.is_relative_to(root), 'import escaped source worktree'
            assert hashlib.sha256(source.read_bytes()).hexdigest() == description['sha256']

    def capture(self, name, data):
        (self.captures / name).write_bytes(data)

    def wal(self, read_bound):
        raw = read_bound()
        assert 32 <= len(raw) <= MAX_BYTES
        page = int.from_bytes(raw[8:12], 'big')
        assert 512 <= page <= 65536 and page & (page-1) == 0
        assert len(raw) <= 32 + 512 * (24 + page), 'WAL frame budget'
        return raw

    def copied(self, name, database, wal, remaining, *, role='copy'):
        # Observer is the sole owner of its Popen PID; controller retains the tracee.
        destination = self.root / name
        destination.mkdir()
        path = destination / 'copy.db'
        path.write_bytes(database)
        if wal is not None:
            Path(str(path) + '-wal').write_bytes(wal)
        state, evidence = run_observer(role, path, self.kind, omission=wal is None,
                                       timeout=min(3, remaining()), phase=name)
        self.observers.append(evidence)
        return state

    def on_arm(self, read_bound, remaining):
        self.seed_wal = self.wal(read_bound)
        descriptor = decode(self.seed_wal)
        assert descriptor['last_commit'] == len(descriptor['frames'])
        self.seed_db, self.db_identity = bounded_regular(self.path)
        self.capture('seed.db', self.seed_db)
        self.capture('seed.wal', self.seed_wal)
        self.seed = self.copied('seed-copy', self.seed_db, self.seed_wal, remaining)
        self.stream = check_full(self.seed, 1, self.started, time.time(), record_kind=self.kind)
        assert bounded_regular(self.path)[0] == self.seed_db
        assert read_bound() == self.seed_wal

    def on_entry(self, number, read_bound, remaining):
        current = self.wal(read_bound)
        decoded = decode(current)
        if current == self.seed_wal or decoded['sha256'] in self.synced:
            label = 'seed-prefix' if current == self.seed_wal else 'already-synced-target-prefix'
            assert len(self.nonqualifying) < 4
            self.capture('nonqualifying-' + str(len(self.nonqualifying)) + '.wal', current)
            self.nonqualifying.append(label)
            return {'eligible': False, 'kind': label, 'wal_sha256': decoded['sha256']}
        classified = candidate(self.seed_wal, current, self.synced)
        assert self.qualified == 0, 'multiple qualifying commit syncs'
        self.qualified += 1
        database, identity = bounded_regular(self.path)
        assert (identity['device'], identity['inode']) == (self.db_identity['device'], self.db_identity['inode'])
        assert database == self.seed_db, 'main DB changed before target sync'
        self.capture('candidate-entry.db', database)
        self.capture('candidate-entry.wal', current)
        full = self.copied('candidate-copy', database, current, remaining,
                           role='stall-copy' if self.observer_fault == 'stall-copy' else 'copy')
        check_full(full, 2, self.started, time.time(), self.stream, self.kind)
        seeded = self.copied('substituted-copy', database, self.seed_wal, remaining)
        check_full(seeded, 1, self.started, time.time(), self.stream, self.kind)
        omitted = self.copied('omitted-copy', database, None, remaining)
        assert omitted['target_absent'], 'main DB already contains target without WAL'
        assert bounded_regular(self.path)[0] == database and read_bound() == current
        self.attribution = {'candidate': full, 'same_main_with_seed_WAL': seeded,
                            'same_main_without_WAL': omitted}
        return {'eligible': True, 'kind': 'fresh-commit', 'wal_sha256': classified['sha256'],
                'candidate': classified}

    def on_exit(self, qualification, read_bound, remaining):
        current = self.wal(read_bound)
        digest = hashlib.sha256(current).hexdigest()
        assert digest == qualification['wal_sha256'], 'prefix changed during sync'
        if qualification['eligible']:
            self.synced.add(digest)
            self.capture('candidate-exit.wal', current)

    def finish(self, events, mode):
        assert self.qualified == 1, 'expected exactly one content-qualified target'
        if mode != 'before':
            assert len(self.synced) == 1
        if self.variant == 'seed_sync':
            assert self.nonqualifying == ['seed-prefix']
            assert [e for e in events if e.get('eligible')][0]['ordinal'] == 2
        if self.variant == 'repeat_sync':
            assert self.nonqualifying == ['already-synced-target-prefix']


def run_outbox(root, kind, mode, variant='normal', cleanup_records=None, observer_fault=None):
    import sys
    from tests._syscall_sync_probe import run_traced
    policy = OutboxPolicy(root, kind, variant, observer_fault)
    worker = Path(__file__).with_name('_outbox_fsync_worker.py')
    source_root = Path(__file__).resolve().parents[1]
    source_head, head_process = observe(['git', '-C', str(source_root), 'rev-parse', 'HEAD'],
                                        output_limit=4096, parse_json=False)
    source_tree, tree_process = observe(['git', '-C', str(source_root), 'rev-parse', 'HEAD^{tree}'],
                                        output_limit=4096, parse_json=False)
    with open('/proc/self/mountinfo') as stream:
        mountinfo = stream.read(MAX_BYTES + 1)
    assert len(mountinfo) <= MAX_BYTES
    # Retain the actual mount entry with the longest containing mount point.
    mounts = []
    for line in mountinfo.splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        mount = decode_mount_field(fields[4])
        if policy.path.resolve().is_relative_to(mount):
            mounts.append((len(mount), line))
    assert mounts
    metadata = {'source_head': source_head.strip(), 'source_tree': source_tree.strip(),
                'mountinfo': max(mounts)[1], 'kernel': os.uname().release,
                'architecture': os.uname().machine,
                'metadata_processes': [head_process, tree_process]}
    request = {'path': str(policy.path), 'variant': variant, 'kind': kind,
               'metadata': metadata}
    marker = None
    if variant == 'startup-stall':
        marker = stall_marker_request(policy.path, 'startup-stall')
        request['stall_marker'] = marker
    argv = [sys.executable, '-B', str(worker), 'producer', json.dumps(request)]
    cleanup = cleanup_records if cleanup_records is not None else []
    try:
        traced = run_traced(argv, Path(str(policy.path)+'-wal'), policy,
                            mode=mode, cleanup_records=cleanup,
                            deadline_seconds=3 if marker is not None else 10)
    except TimeoutError:
        if marker is not None:
            assert len(cleanup) == 1
            verify_stall_marker(marker, 'startup-stall', cleanup[0])
        raise
    # FIRST normal ORIGINAL open only after run_traced has terminally reaped producer.
    recovered, observation = run_observer('recover', policy.path, kind, phase='post-terminal-original-recovery')
    count = len(recovered['records'])
    assert count in (1, 2) if mode == 'before' else count == 2
    check_full(recovered, count, policy.started, time.time(), policy.stream, kind)
    expected = policy.seed if count == 1 else policy.attribution['candidate']
    assert {key: recovered[key] for key in ('records', 'streams')} == {
        key: expected[key] for key in ('records', 'streams')}, 'recovered immutable state differs from stopped candidate'
    retry, retry_observation = run_observer(
        'stall-retry' if observer_fault == 'stall-retry' else 'retry', policy.path, kind,
        phase='post-terminal-post-recovery-retry')
    assert retry == {'origin_seq': 2, 'origin_stream_id': policy.stream}
    final, final_observation = run_observer('recover', policy.path, kind)
    check_full(final, 2, policy.started, time.time(), policy.stream, kind)
    assert final['records'][0] == policy.seed['records'][0]
    if count == 2:
        assert final == recovered, 'retry altered immutable persisted state'
    return {'trace': traced, 'record_kind': kind, 'recovered': recovered, 'after_retry': final,
            'attribution': policy.attribution, 'observers': policy.observers +
            [observation, retry_observation, final_observation],
            'runtime': json.loads(policy.runtime_bytes),
            'captures': {p.name: {'bytes': p.stat().st_size,
                        'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in policy.captures.iterdir()}}
