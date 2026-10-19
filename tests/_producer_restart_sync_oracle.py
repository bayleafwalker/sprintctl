"""Independent persisted-state checks for bounded producer restart fixtures."""
from datetime import datetime, timezone
import hashlib
import math
import os
import select
import subprocess
import sys
from uuid import uuid4
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID

OUTBOX_TABLES = ('outbox_stream', 'outbox_record')
CACHE_TABLES = ('cached_projection_meta', 'cached_ingest_record', 'cached_ingest_watermark',
                'cached_authority_decision', 'cached_authority_watermark')
AT = '2026-10-18T12:00:00Z'
ACTOR = 'restart-verifier'
SESSION = 'restart-fixture'


class RestartInvariantFailure(AssertionError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def raw_sqlite(path, tables):
    # Normal opening may perform automatic recovery; never immutable/sidecar deletion.
    conn = sqlite3.connect(str(path), timeout=0.2)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + 2
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return {table: [dict(row) for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')]
                for table in tables}
    finally:
        conn.close()


def check_outbox(actual, expected):
    rows = actual['outbox_record']
    streams = actual['outbox_stream']
    if len(streams) != 1 or [r['origin_seq'] for r in rows] != list(range(1, len(rows)+1)):
        raise RestartInvariantFailure('retained sequence')
    stream = streams[0]
    if stream['singleton'] != 1:
        raise RestartInvariantFailure('stream singleton')
    if stream['next_origin_seq'] != len(rows)+1:
        raise RestartInvariantFailure('next sequence')
    UUID(stream['origin_stream_id'])
    for row in rows:
        if row['origin_stream_id'] != stream['origin_stream_id']:
            raise RestartInvariantFailure('origin identity')
        if digest(json.loads(row['payload'])) != row['payload_sha256']:
            raise RestartInvariantFailure('payload digest')
        fingerprint_fields = ('schema_version','record_class','event_type','actor','runtime_session_id',
                              'occurred_at','basis_revision','correlation_id','causation_id','payload_sha256')
        if digest({k:row[k] for k in fingerprint_fields}) != row['record_sha256']:
            raise RestartInvariantFailure('record digest')
    if actual != expected:
        raise RestartInvariantFailure('exact retained manifest')


def check_authored(rows, case, command=None, *, count=None):
    count = count if count is not None else (3 if case == 'observations' else 2)
    if len(rows) != count:
        raise RestartInvariantFailure('authored population')
    for i, row in enumerate(rows):
        if (row['actor'],row['runtime_session_id'],row['occurred_at']) != (ACTOR,SESSION,AT):
            raise RestartInvariantFailure('authored provenance')
        if i == 0 and case == 'local-prefix':
            expected = ('authority-command','item.transition', command['event_id'],command)
        else:
            n=i+1
            expected = ('observation','work.completed',f'restart-event-{n}',{'index':n,'text':'résumé'})
        if (row['record_class'],row['event_type'],row['event_id'],json.loads(row['payload'])) != expected:
            raise RestartInvariantFailure('authored envelope')
        expected_basis=command['basis_revision'] if case=='local-prefix' and i==0 else None
        if row['basis_revision']!=expected_basis or row['correlation_id'] is not None or row['causation_id'] is not None:
            raise RestartInvariantFailure('authored basis/correlation')
        if row['payload'] != canonical(expected[3]):
            raise RestartInvariantFailure('authored canonical payload text')
        if row['schema_version'] != 1:
            raise RestartInvariantFailure('authored schema')


def expected_cached(row):
    fields = ('origin_stream_id','origin_seq','event_id','schema_version','record_class','event_type',
              'actor','runtime_session_id','occurred_at','basis_revision','correlation_id','causation_id',
              'payload_sha256','created_at')
    value = {k: row[k] for k in fields}
    value['payload'] = json.loads(row['payload'])
    return value


def check_cache(actual, repo_id, records, offsets):
    if actual['cached_projection_meta'] != [{'singleton':1,'schema_version':2,'repo_id':repo_id}]:
        raise RestartInvariantFailure('cache identity/schema')
    rows = actual['cached_ingest_record']
    if [r['ingest_offset'] for r in rows] != offsets:
        raise RestartInvariantFailure('cache complete population')
    for row, record in zip(rows, records):
        text=canonical(expected_cached(record))
        if row['repo_id'] != repo_id or row['record_json'] != text or row['record_sha256'] != hashlib.sha256(text.encode()).hexdigest():
            raise RestartInvariantFailure('cache exact envelope')
    watermarks=actual['cached_ingest_watermark']
    if len(watermarks)!=1 or watermarks[0]['repo_id']!=repo_id or watermarks[0]['ingest_offset']!=max(offsets,default=0):
        raise RestartInvariantFailure('cache watermark')
    if (watermarks[0]['advanced_at'] is not None) != bool(offsets):
        raise RestartInvariantFailure('cache advancement timestamp')
    if actual['cached_authority_decision']:
        raise RestartInvariantFailure('unexpected authority decision')
    authority=actual['cached_authority_watermark']
    if len(authority)!=1 or authority[0]['repo_id']!=repo_id or authority[0]['decision_ingest_offset']!=0 or authority[0]['advanced_at'] is not None:
        raise RestartInvariantFailure('authority watermark')


def validate_marker(marker, *, pid, nonce, role, phase):
    if (marker.get('pid'),marker.get('nonce'),marker.get('role'),marker.get('phase')) != (pid,nonce,role,phase):
        raise RestartInvariantFailure('worker checkpoint identity')


def check_time(value, lower, upper):
    # Existing producer clock truncates to whole UTC seconds; bound at that precision.
    stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.microsecond != 0 or not value.endswith('Z'):
        raise RestartInvariantFailure('generated timestamp representation')
    if stamp.utcoffset().total_seconds()!=0 or not math.floor(lower) <= stamp.timestamp() <= math.floor(upper):
        raise RestartInvariantFailure('generated timestamp interval')


ROOT = Path(__file__).resolve().parents[1]

def start_worker(cfg):
    env={**os.environ,'PYTHONPATH':str(ROOT)}
    proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name('_producer_restart_sync_worker.py'))],cwd=ROOT,env=env,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        proc.stdin.write(json.dumps(cfg).encode())
        proc.stdin.close()
        return proc
    except BaseException:
        cleanup(proc)
        raise

def marker(proc, *, timeout=20):
    deadline=time.monotonic()+timeout; data=b''
    while time.monotonic()<deadline:
        ready,_,_=select.select([proc.stdout],[],[],max(0,deadline-time.monotonic()))
        if not ready:break
        chunk=os.read(proc.stdout.fileno(),4096)
        if not chunk:raise RestartInvariantFailure('worker marker eof')
        data+=chunk
        if len(data)>65536:raise RestartInvariantFailure('worker output bound')
        if b'\n' in data:
            line,_,tail=data.partition(b'\n')
            if tail:raise RestartInvariantFailure('unexpected worker output')
            return json.loads(line)
    raise RestartInvariantFailure('worker marker deadline')

def cleanup(proc):
    if proc.poll() is None:proc.kill()
    proc.wait(timeout=10)
    for stream in (proc.stdin,proc.stdout,proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()

def run_sync(cfg):
    return call_worker(cfg,phase='synchronized')


def call_worker(cfg, *, phase, timeout=20):
    cfg={**cfg,'nonce':str(uuid4())}
    proc=start_worker(cfg)
    try:
        value=marker(proc,timeout=timeout)
        validate_marker(value,pid=proc.pid,nonce=cfg['nonce'],role=cfg['role'],phase=phase)
        if proc.wait(timeout=10)!=0 or proc.stderr.read(65537)!=b'' or proc.stdout.read(65537)!=b'':
            raise RestartInvariantFailure('worker completion')
        return value
    finally:cleanup(proc)


def observe_sqlite(path, tables, *, history=None):
    if tables not in (OUTBOX_TABLES,CACHE_TABLES):
        raise ValueError('unregistered observation tables')
    value=call_worker({'role':'raw-observer','case':'outbox' if tables==OUTBOX_TABLES else 'cache',
        'outbox':str(path),'cache':str(path)},phase='observed')
    if history is not None:history.append(value)
    return value['state']


def copy_sqlite(source, destination):
    return call_worker({'role':'sqlite-copy','case':'copy','outbox':str(source),
        'cache':str(destination)},phase='copied')


def check_sync_result(phase, records, offsets, *, duplicates, applied, pending):
    result=phase['result']
    expected_uploads=[{'duplicate':duplicate,'ingest_offset':offset,'record':expected_cached(record)}
        for record,offset,duplicate in zip(records,offsets,duplicates)]
    if len(records)!=len(offsets) or len(offsets)!=len(duplicates) or result['uploaded']!=expected_uploads:
        raise RestartInvariantFailure('returned upload identity/content')
    cursor=phase['cache']['cached_ingest_watermark'][0]
    if result['watermark']!={'ingest_offset':max(offsets,default=0),'advanced_at':cursor['advanced_at']}:
        raise RestartInvariantFailure('returned ingest watermark')
    if result['applied_count']!=applied or result['pending_command_event_ids']!=pending:
        raise RestartInvariantFailure('returned apply/pending')
    if result['command_decisions']!=[] or result['decision_applied_count']!=0 or result['decision_watermark']!={'ingest_offset':0,'advanced_at':None}:
        raise RestartInvariantFailure('returned authority separation')
    if result['release_harvest'] is not None:
        raise RestartInvariantFailure('unexpected harvest')
