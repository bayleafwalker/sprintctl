"""Actual killed producer -> fresh-process sync -> real disposable PG/cache."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from tests.pg._shared import PG_MARKS, pg, contracts, psycopg, dict_row, _PG_URL, assert_disposable_connection
from tests import _producer_restart_sync_worker as worker
from tests._producer_restart_sync_oracle import (
    ACTOR, AT, SESSION, OUTBOX_TABLES, CACHE_TABLES, RestartInvariantFailure,
    canonical, digest, expected_cached, check_authored, check_cache, check_outbox, check_time, raw_sqlite, validate_marker, start_worker, marker, cleanup, run_sync, observe_sqlite, check_sync_result,
)

pytestmark = [*PG_MARKS, pytest.mark.skipif(os.name!='posix' or not hasattr(signal,'SIGKILL'),
    reason='requires actual own-child POSIX SIGKILL')]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(pg_test_scope):
    conn=psycopg.connect(_PG_URL,row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id=pg_test_scope('restart-sync',canonical_uuid=True)
    value=pg.PgStore(conn,repo_id,authority_repo_uuid=repo_id)
    pg.init_db(value)
    try:yield value
    finally:conn.close()










def remote_state(conn,repo_id):
    from psycopg import sql
    # Query every repo-scoped authority table independently of product getters.
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND column_name='repo_id' ORDER BY table_name")
        tables=[r['table_name'] for r in cur.fetchall()]
        assert 1<len(tables)<100
        result={}
        for table in tables:
            cur.execute(sql.SQL('SELECT * FROM {} WHERE repo_id=%s').format(sql.Identifier(table)),(repo_id,))
            rows=cur.fetchall()
            result[table]=sorted(rows,key=(lambda r:r['ingest_offset']) if table=='ingest_record' else (lambda r:json.dumps(r,sort_keys=True,default=str)))
    conn.rollback()  # Observer owns its read transaction only.
    return result


def assert_runtime(value):
    runtime=value['runtime']
    assert Path(runtime['executable']).resolve()==Path(sys.executable).resolve()
    assert runtime['sqlite']==sqlite3.sqlite_version
    assert runtime['python']==sys.version
    assert set(runtime['imports'])=={'sprintctl.outbox','sprintctl.pg','sprintctl.projection','sprintctl.sync','sprintctl.contracts'}
    for name,entry in runtime['imports'].items():
        path=ROOT/(name.replace('.','/')+'.py')
        assert Path(entry['path'])==path
        assert entry['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()


def check_remote(raw,local,repo_id):
    rows=raw.get('ingest_record',[])
    assert [r['origin_seq'] for r in rows]==list(range(1,len(local)+1))
    assert [r['ingest_offset'] for r in rows]==list(range(1,len(local)+1))
    for row,known in zip(rows,local):
        for key in ('origin_stream_id','origin_seq','event_id','schema_version','record_class','event_type',
                    'actor','runtime_session_id','basis_revision','correlation_id','causation_id',
                    'payload_sha256'):
            assert row[key]==known[key],key
        assert row['record_sha256']==digest(expected_cached(known))
        assert row['repo_id']==repo_id and row['payload']==json.loads(known['payload'])
        assert row['occurred_at']==datetime.fromisoformat(known['occurred_at'].replace('Z','+00:00'))
        assert row['producer_created_at']==datetime.fromisoformat(known['created_at'].replace('Z','+00:00'))
    assert len(raw['ingest_stream'])==1
    assert raw['ingest_stream'][0]['repo_id']==repo_id
    assert raw['ingest_stream'][0]['origin_stream_id']==local[0]['origin_stream_id']
    assert raw['ingest_stream'][0]['highest_origin_seq']==len(local)
    assert raw['ingest_repo_cursor']==[{'repo_id':repo_id,'highest_offset':len(local)}]
    return [r['ingest_offset'] for r in rows]


@pytest.mark.parametrize('case',['observations','local-prefix'])
def test_sigkill_pending_producer_restarts_into_actual_sync(store,work_item_id,tmp_path,case,record_property):
    observer=psycopg.connect(_PG_URL,row_factory=dict_row)
    assert_disposable_connection(observer)
    try:
        # The command names this actual committed item; no authority operation is requested by the test runner.
        item=pg.get_work_item(store,work_item_id)
        command=contracts.AuthorityCommand(event_id=str(uuid4()),record_type='item.transition',schema_version='1',
            actor=ACTOR,authored_at=AT,refs={'repo_id':store.authority_repo_uuid,'aggregate_type':'item',
                'aggregate_uuid':item['aggregate_uuid'],'aggregate_id':work_item_id},payload={'to_status':'active'},
            basis_revision=f"item:{item['aggregate_uuid']}@status:pending").to_dict()
        baseline=remote_state(observer,store.repo_id)
        assert baseline.get('ingest_record',[])==[]
        cfg={'outbox':str(tmp_path/'producer.db'),'cache':str(tmp_path/'cache.db'),
             'repo_id':store.repo_id,'case':case,'role':'producer','nonce':str(uuid4()),'command':command}
        observers=[]
        begin=time.time();producer=start_worker(cfg)
        try:
            ready=marker(producer);validate_marker(ready,pid=producer.pid,nonce=cfg['nonce'],role='producer',phase='appends-returned')
            assert_runtime(ready);assert ready['in_transaction'] is False
            check_authored(ready['outbox']['outbox_record'],case,command)
            check_outbox(ready['outbox'],ready['outbox'])
            assert ready['returned']==[expected_cached(row) for row in ready['outbox']['outbox_record']]
            for row in ready['outbox']['outbox_record']:check_time(row['created_at'],begin,time.time())
            check_time(ready['outbox']['outbox_stream'][0]['created_at'],begin,time.time())
            independent_predeath=observe_sqlite(cfg['outbox'],OUTBOX_TABLES,history=observers)
            check_outbox(independent_predeath,ready['outbox'])
            assert remote_state(observer,store.repo_id)==baseline
            producer.kill();assert producer.wait(timeout=10)==-signal.SIGKILL
            killed_pid=producer.pid
        finally:cleanup(producer)
        recovered=observe_sqlite(cfg['outbox'],OUTBOX_TABLES,history=observers)
        check_outbox(recovered,ready['outbox'])
        first_begin=time.time()
        first=run_sync({**cfg,'role':'first-sync'});assert_runtime(first)
        first_end=time.time()
        first_state=first['phases'][0];check_outbox(first_state['outbox'],recovered)
        assert observe_sqlite(cfg['outbox'],OUTBOX_TABLES,history=observers)==first_state['outbox']
        assert observe_sqlite(cfg['cache'],CACHE_TABLES,history=observers)==first_state['cache']
        remote_first=remote_state(observer,store.repo_id)
        first_result=first_state['result'];rows=recovered['outbox_record']
        assert first_result['command_decisions']==[] and first_result['decision_applied_count']==0
        assert first_result['decision_watermark']=={'ingest_offset':0,'advanced_at':None}
        if case=='observations':
            offsets=check_remote(remote_first,rows,store.repo_id)
            assert [x['duplicate'] for x in first_result['uploaded']]==[False]*3
            assert [x['ingest_offset'] for x in first_result['uploaded']]==offsets
            assert first_result['applied_count']==3 and first_result['pending_command_event_ids']==[]
            for raw_row in remote_first['ingest_record']:
                assert type(raw_row['ingest_id']) is int and raw_row['ingest_id']>0
                assert isinstance(raw_row['ingested_at'],datetime) and raw_row['ingested_at'].tzinfo is not None
                assert first_begin <= raw_row['ingested_at'].timestamp() <= first_end
            assert first_begin <= remote_first['ingest_stream'][0]['created_at'].timestamp() <= first_end
        else:
            offsets=[]
            assert remote_first==baseline
            assert first_result['uploaded']==[] and first_result['applied_count']==0
            assert first_result['pending_command_event_ids']==[command['event_id']]
        check_cache(first_state['cache'],store.repo_id,rows if offsets else [],offsets)
        if offsets:
            for cached_row in first_state['cache']['cached_ingest_record']:
                check_time(cached_row['received_at'],first_begin,first_end)
            check_time(first_state['cache']['cached_ingest_watermark'][0]['advanced_at'],first_begin,first_end)
        check_sync_result(first_state,rows if offsets else [],offsets,duplicates=[False]*len(offsets),
            applied=len(offsets),pending=[] if offsets else [command['event_id']])
        replay_begin=time.time();replay=run_sync({**cfg,'role':'replay-sync'});assert_runtime(replay)
        last=replay['phases'][-1]
        assert observe_sqlite(cfg['outbox'],OUTBOX_TABLES,history=observers)==last['outbox']
        assert observe_sqlite(cfg['cache'],CACHE_TABLES,history=observers)==last['cache']
        old=replay['phases'][0];check_outbox(old['outbox'],recovered)
        assert old['cache']==first_state['cache']
        assert old['result']['applied_count']==0
        assert [x['duplicate'] for x in old['result']['uploaded']]==[True]*len(offsets)
        assert [x['ingest_offset'] for x in old['result']['uploaded']]==offsets
        check_sync_result(old,rows if offsets else [],offsets,duplicates=[True]*len(offsets),
            applied=0,pending=[] if offsets else [command['event_id']])
        if case=='observations':
            final=replay['phases'][1];new=final['outbox'];assert new['outbox_record'][:-1]==rows
            assert new['outbox_stream'][0]['origin_stream_id']==recovered['outbox_stream'][0]['origin_stream_id']
            assert new['outbox_stream'][0]['created_at']==recovered['outbox_stream'][0]['created_at']
            assert new['outbox_stream'][0]['next_origin_seq']==5
            check_authored(new['outbox_record'],'observations',count=4)
            check_outbox(new,new);check_time(new['outbox_record'][-1]['created_at'],replay_begin,time.time())
            assert new['outbox_record'][-1]['origin_seq']==4
            assert new['outbox_record'][-1]['event_id']=='restart-event-4'
            assert json.loads(new['outbox_record'][-1]['payload'])=={'index':4,'text':'résumé'}
            assert [x['duplicate'] for x in final['result']['uploaded']]==[True,True,True,False]
            assert final['result']['applied_count']==1
            remote_final=remote_state(observer,store.repo_id)
            assert remote_final['ingest_record'][:3]==remote_first['ingest_record']
            assert remote_final['ingest_stream'][0]['created_at']==remote_first['ingest_stream'][0]['created_at']
            assert final['cache']['cached_ingest_record'][:3]==first_state['cache']['cached_ingest_record']
            check_time(final['cache']['cached_ingest_record'][-1]['received_at'],replay_begin,time.time())
            check_time(final['cache']['cached_ingest_watermark'][0]['advanced_at'],replay_begin,time.time())
            final_offsets=check_remote(remote_final,new['outbox_record'],store.repo_id)
            check_cache(final['cache'],store.repo_id,new['outbox_record'],final_offsets)
            check_sync_result(final,new['outbox_record'],final_offsets,duplicates=[True,True,True,False],applied=1,pending=[])
            row4=remote_final['ingest_record'][-1]
            assert type(row4['ingest_id']) is int and row4['ingest_id']>0
            assert isinstance(row4['ingested_at'],datetime) and row4['ingested_at'].tzinfo is not None
            assert replay_begin <= row4['ingested_at'].timestamp() <= time.time()
        else:
            remote_final=remote_state(observer,store.repo_id);assert remote_final==baseline
            assert old['result']['pending_command_event_ids']==[command['event_id']]
        # Every repo-scoped table outside ingest remains byte/value equivalent.
        for table in baseline:
            if table not in ('ingest_record','ingest_stream','ingest_repo_cursor'):
                assert remote_final[table]==baseline[table],table
        assert len({killed_pid,first['pid'],replay['pid']})==3
        assert len(observers)==6
        for observation in observers:assert_runtime(observation)
        record_property('state_history',json.dumps({'case':case,'baseline':baseline,'producer':ready,
            'observers':observers,'signal_returncode':-9,'first_raw_recovery':recovered,'first_sync':first,'replay_sync':replay,
            'remote_after_first':remote_first,'remote_after_final':remote_final},sort_keys=True,default=str))
    finally:observer.close()
