"""Finite persisted-state and real owned-worker falsifiers for F02 fixtures."""
from copy import deepcopy
import json
import signal
import sqlite3
import time
from uuid import uuid4

import pytest
from sprintctl import outbox, projection
from tests._producer_restart_sync_oracle import (
    AT, ACTOR, SESSION, OUTBOX_TABLES, CACHE_TABLES, RestartInvariantFailure,
    observe_sqlite as raw_sqlite, copy_sqlite, check_outbox, check_cache, expected_cached,
    start_worker, marker, cleanup, validate_marker,
)


@pytest.fixture
def valid(tmp_path):
    path=tmp_path/'original.db';conn=outbox.open_outbox(path)
    try:
        for i in (1,2,3):outbox.append_observation(conn,event_type='work.completed',actor=ACTOR,
            payload={'index':i,'text':'résumé'},event_id=f'restart-event-{i}',
            occurred_at=AT,runtime_session_id=SESSION)
    finally:conn.close()
    before=raw_sqlite(path,OUTBOX_TABLES);check_outbox(before,before)
    return path,before


@pytest.mark.parametrize('fault,message',[
    ('lost-first','retained sequence'),('next','next sequence'),('origin','origin identity'),
    ('changed-payload','payload digest'),('consistent-partial','exact retained manifest'),
    ('whole-origin','exact retained manifest'),
])
def test_same_checker_rejects_persisted_wrong_outbox(valid,tmp_path,fault,message,record_property):
    original,expected=valid;copy=tmp_path/'copy.db'
    copied=copy_sqlite(original,copy)
    assert copied['phase']=='copied'
    conn=sqlite3.connect(copy)
    try:
        # Only disposable verifier copies lose immutable guards to seed known wrong state.
        conn.execute('DROP TRIGGER outbox_record_is_immutable_update')
        conn.execute('DROP TRIGGER outbox_record_is_immutable_delete')
        if fault=='lost-first':conn.execute('DELETE FROM outbox_record WHERE origin_seq=1')
        elif fault=='next':conn.execute('UPDATE outbox_stream SET next_origin_seq=99')
        elif fault=='origin':conn.execute('UPDATE outbox_stream SET origin_stream_id=?',(str(uuid4()),))
        elif fault=='changed-payload':conn.execute("UPDATE outbox_record SET payload='{}' WHERE origin_seq=3")
        elif fault=='consistent-partial':
            conn.execute('DELETE FROM outbox_record WHERE origin_seq=3')
            conn.execute('UPDATE outbox_stream SET next_origin_seq=3')
        elif fault=='whole-origin':
            new=str(uuid4());conn.execute('UPDATE outbox_stream SET origin_stream_id=?',(new,))
            conn.execute('UPDATE outbox_record SET origin_stream_id=?',(new,))
        conn.commit()
    finally:conn.close()
    actual=raw_sqlite(copy,OUTBOX_TABLES)
    with pytest.raises(RestartInvariantFailure,match=message):check_outbox(actual,expected)
    check_outbox(raw_sqlite(original,OUTBOX_TABLES),expected)
    record_property('state_history',json.dumps({'control':fault,'expected':expected,'actual':actual,'refusal':message},sort_keys=True))


def test_same_checker_rejects_persisted_ahead_cache_watermark(valid,tmp_path,record_property):
    _,state=valid;path=tmp_path/'cache.db';repo='oracle-fixture'
    conn=projection.open_cached_projection(path,repo_id=repo)
    try:
        projection.apply_ingested_records(conn,[projection.CachedIngestRecord(i,expected_cached(row))
            for i,row in enumerate(state['outbox_record'],1)])
    finally:conn.close()
    positive=raw_sqlite(path,CACHE_TABLES);check_cache(positive,repo,state['outbox_record'],[1,2,3])
    conn=sqlite3.connect(path)
    try:conn.execute('UPDATE cached_ingest_watermark SET ingest_offset=4');conn.commit()
    finally:conn.close()
    actual=raw_sqlite(path,CACHE_TABLES)
    with pytest.raises(RestartInvariantFailure,match='cache watermark'):check_cache(actual,repo,state['outbox_record'],[1,2,3])
    record_property('state_history',json.dumps({'control':'ahead-cache','positive':positive,'actual':actual},sort_keys=True))


@pytest.mark.skipif(not hasattr(signal,'SIGKILL'),reason='own-child POSIX verification')
@pytest.mark.parametrize('fault',['missing-nonce','wrong-pid','wrong-role','wrong-phase'])
def test_real_bad_checkpoint_refuses_qualification_and_reaps_owned_pid(tmp_path,fault,record_property):
    cfg={'outbox':str(tmp_path/'producer.db'),'cache':str(tmp_path/'unused.db'),
         'nonce':str(uuid4()),'role':'producer','case':'observations','marker_fault':fault}
    proc=start_worker(cfg)
    try:
        value=marker(proc)
        assert value['control']=={'pid':proc.pid,'nonce':cfg['nonce'],'fault':fault}
        with pytest.raises(RestartInvariantFailure,match='worker checkpoint identity'):
            validate_marker(value,pid=proc.pid,nonce=cfg['nonce'],role='producer',phase='appends-returned')
    finally:cleanup(proc)
    assert proc.returncode==-signal.SIGKILL
    record_property('state_history',json.dumps({'control':fault,'actual_popen_pid':proc.pid,'marker':value,
        'cleanup_returncode':proc.returncode,'qualified_kill':False},sort_keys=True))


@pytest.mark.skipif(not hasattr(signal,'SIGKILL'),reason='own-child POSIX verification')
@pytest.mark.parametrize('role',['producer','first-sync','replay-sync','raw-observer','sqlite-copy'])
def test_deadline_control_proves_child_entered_stall(tmp_path,role,record_property,monkeypatch):
    cfg={'outbox':str(tmp_path/'unused.db'),'cache':str(tmp_path/'unused-cache.db'),
         'nonce':str(uuid4()),'role':role,'case':'observations','stall':True}
    proc=start_worker(cfg)
    try:
        value=marker(proc);validate_marker(value,pid=proc.pid,nonce=cfg['nonce'],role=role,phase='reached-stall')
        from tests import _producer_restart_sync_oracle as driver
        actual_select=driver.select.select
        requested=[]
        def bounded_select(read,write,error,timeout):
            requested.append(timeout)
            assert 0 <= timeout <= 0.05
            return actual_select(read,write,error,timeout)
        monkeypatch.setattr(driver.select,'select',bounded_select)
        with pytest.raises(RestartInvariantFailure,match='^worker marker deadline$'):
            marker(proc,timeout=0.05)
        assert requested
        assert proc.poll() is None
    finally:cleanup(proc)
    assert proc.returncode==-signal.SIGKILL
    record_property('state_history',json.dumps({'control':'reached-stall','marker':value,
        'cleanup_returncode':proc.returncode},sort_keys=True))
