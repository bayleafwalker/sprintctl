"""Owned fixture worker; no credentials in arguments, output or error details."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

from sprintctl import contracts, outbox, pg, projection, sync
from tests._producer_restart_sync_oracle import ACTOR, AT, SESSION, OUTBOX_TABLES, CACHE_TABLES, raw_sqlite


def emit(value):
    data = json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)
    if len(data.encode()) >= 65536:
        raise ValueError('worker output bound')
    print(data, flush=True)


def runtime():
    modules=(outbox,pg,projection,sync,contracts)
    return {'python':sys.version,'executable':sys.executable,'sqlite':sqlite3.sqlite_version,
            'imports':{m.__name__:{'path':str(Path(m.__file__).resolve()),
            'sha256':hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()} for m in modules}}


def append_observation(conn,n):
    return outbox.append_observation(conn,event_type='work.completed',actor=ACTOR,
        event_id=f'restart-event-{n}',occurred_at=AT,runtime_session_id=SESSION,
        payload={'index':n,'text':'résumé'})


def main(cfg):
    if cfg['role'] not in {'producer','first-sync','replay-sync','raw-observer','sqlite-copy'}:
        raise ValueError('unregistered worker role')
    path=Path(cfg['outbox']); cache=Path(cfg['cache'])
    identity={'pid':os.getpid(),'nonce':cfg['nonce'],'role':cfg['role'],'runtime':runtime()}
    if cfg.get('stall'):
        emit({**identity,'phase':'reached-stall'})
        while True: signal.pause()
    if cfg['role']=='raw-observer':
        tables=OUTBOX_TABLES if cfg['case']=='outbox' else CACHE_TABLES
        emit({**identity,'phase':'observed','state':raw_sqlite(path,tables)})
        return
    if cfg['role']=='sqlite-copy':
        if not path.is_file():raise ValueError('missing copy source')
        source=sqlite3.connect(str(path),timeout=0.2)
        destination=sqlite3.connect(str(cache),timeout=0.2)
        deadline=time.monotonic()+2
        try:
            def progress(status,remaining,total):
                if time.monotonic()>=deadline:raise TimeoutError('copy deadline')
            source.backup(destination,pages=1,progress=progress,sleep=0.001)
        finally:
            destination.close();source.close()
        emit({**identity,'phase':'copied','source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'destination_sha256':hashlib.sha256(cache.read_bytes()).hexdigest()})
        return
    conn=outbox.open_outbox(path)
    try:
        if cfg['role']=='producer':
            if cfg['case']=='observations':
                returned=[asdict(append_observation(conn,n)) for n in (1,2,3)]
            else:
                command=contracts.record_from_dict(cfg['command'])
                returned=[asdict(outbox.append_authority_command(conn,command,runtime_session_id=SESSION)),
                          asdict(append_observation(conn,2))]
            marker={**identity,'phase':'appends-returned','returned':returned,
                'in_transaction':conn.in_transaction,'journal_mode':conn.execute('PRAGMA journal_mode').fetchone()[0],
                'synchronous':conn.execute('PRAGMA synchronous').fetchone()[0],
                'outbox':raw_sqlite(path,OUTBOX_TABLES)}
            fault=cfg.get('marker_fault')
            if fault:
                marker['control']={'pid':os.getpid(),'nonce':cfg['nonce'],'fault':fault}
                if fault=='missing-nonce': marker.pop('nonce')
                elif fault=='wrong-pid': marker['pid']=os.getpid()+1000000
                elif fault=='wrong-role': marker['role']='unexecuted-role'
                elif fault=='wrong-phase': marker['phase']='unexecuted-phase'
                else: raise ValueError('unknown marker fault')
            emit(marker)
            while True: signal.pause()
        import psycopg
        from psycopg.rows import dict_row
        from sprintctl.pg_testing import assert_disposable_connection
        remote=psycopg.connect(os.environ['SPRINTCTL_TEST_PG_URL'],row_factory=dict_row)
        try:
            assert_disposable_connection(remote)
            store=pg.PgStore(remote,cfg['repo_id'])
            projection_conn=projection.open_cached_projection(cache,repo_id=cfg['repo_id'])
            try:
                def synchronize():
                    result=sync.synchronize_outbox(conn,store,projection_conn,batch_size=2)
                    return {'result':asdict(result),'outbox':raw_sqlite(path,OUTBOX_TABLES),
                            'cache':raw_sqlite(cache,CACHE_TABLES)}
                phases=[synchronize()]
                if cfg['role']=='replay-sync' and cfg['case']=='observations':
                    append_observation(conn,4)
                    phases.append(synchronize())
                emit({**identity,'phase':'synchronized','phases':phases})
            finally: projection_conn.close()
        finally: remote.close()
    finally: conn.close()


if __name__=='__main__':
    try:
        data=sys.stdin.buffer.read(16385)
        if len(data)>16384: raise ValueError('worker input bound')
        main(json.loads(data))
    except Exception as exc:
        # Avoid connection exception strings/tracebacks, which may contain DSNs.
        emit({'phase':'worker-error','pid':os.getpid(),'error_type':type(exc).__name__})
        raise SystemExit(73)
