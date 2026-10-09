"""Actual native effect owner replay after producer response loss."""
import copy
from dataclasses import replace
import json

import pytest
from sprintctl import outbox,pg,proposal_intake as intake
from sprintctl.application import ApplicationRejection,WorkApplication
from tests.pg._shared import PG_MARKS
from tests.pg.test_effect_intent import PROPOSER,ACCEPTOR,_run,_items,_accept,_reject,_mark_applied
from tests.test_native_proposal_intake import payload,capture

pytestmark=PG_MARKS


def setup(store,tmp_path,pg_test_scope):
    store=replace(store,repo_id=pg_test_scope('proposal-carrier'))
    app=WorkApplication.postgres(store);run=_run(store,PROPOSER)
    binding=app.invoke(intake.RESOLVE,{'run_id':run},PROPOSER)
    (item,)=_items(store);request,_=payload(store.repo_id)
    request['arguments'].update(run_id=run,item_id=item)
    return store,app,binding,tmp_path/'producer.db',request


def counts(store):
    with store.conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM work_effect_intent WHERE repo_id=%s) AS intents,(SELECT count(*) FROM work_idempotency_ledger WHERE repo_id=%s AND tool='propose_effect') AS keys",(store.repo_id,store.repo_id))
        return dict(cur.fetchone())


def sync(path,binding,invoke):
    return intake.synchronize(path,repo_id=binding['repo_id'],invoke=invoke,rejection_type=ApplicationRejection)


@pytest.mark.parametrize('state',['proposed','accepted','rejected','applied'])
def test_actual_commit_lost_reply_replays_current_state(store,tmp_path,pg_test_scope,state):
    store,app,binding,path,request=setup(store,tmp_path,pg_test_scope)
    captured=capture(path,request,binding);assert counts(store)=={'intents':0,'keys':0}
    admitted=[]
    def lost(op,args):
        result=app.invoke(op,args,PROPOSER)
        if op==intake.OPERATION:
            admitted.append(result['intent']);raise OSError('reply lost after actual owner commit')
        return result
    first=sync(path,binding,lost)
    assert first['proposal_attempts'][0]['phase']=='unknown'
    assert counts(store)=={'intents':1,'keys':1}
    intent=admitted[0]
    if state in {'accepted','applied'}:intent=_accept(store,intent)
    elif state=='rejected':intent=_reject(store,intent)
    if state=='applied':intent=_mark_applied(store,intent)
    result=sync(path,binding,lambda op,args:app.invoke(op,args,PROPOSER))
    assert result['confirmed_proposal_request_ids']==[captured['request_id']]
    assert counts(store)=={'intents':1,'keys':1}
    conn=outbox.open_outbox(path)
    row=conn.execute("SELECT result_json,result_sha256 FROM native_proposal_attempt WHERE phase='confirmed'").fetchone();conn.close()
    receipt=json.loads(row[0]);assert intake._digest(row[0].encode())==row[1]
    assert receipt['intent']['state']==state
    assert receipt['intent']['revision']==intent['revision']
    assert receipt['intent']['unified_diff']==request['arguments']['unified_diff']


def test_foreign_run_binding_stops_before_proposal(store,tmp_path,pg_test_scope):
    store,app,binding,path,request=setup(store,tmp_path,pg_test_scope)
    forged={**binding,'grant_id':'incorrect-grant'};captured=capture(path,request,forged)
    result=sync(path,forged,lambda op,args:app.invoke(op,args,PROPOSER))
    assert result['proposal_attempts'][0]['operation']==intake.RESOLVE
    assert result['pending_proposal_request_ids']==[captured['request_id']]
    assert counts(store)=={'intents':0,'keys':0}


def test_unknown_item_and_fifo_are_not_silently_superseded(store,tmp_path,pg_test_scope):
    store,app,binding,path,request=setup(store,tmp_path,pg_test_scope)
    missing=copy.deepcopy(request);missing['arguments']['item_id']=2_000_000_000
    first=capture(path,missing,binding)
    later=copy.deepcopy(request);later['arguments']['idempotency_key']='later-proposal';second=capture(path,later,binding)
    result=sync(path,binding,lambda op,args:app.invoke(op,args,PROPOSER))
    assert result['proposal_attempts'][0]['phase']=='rejected'
    assert result['proposal_attempts'][0]['code']=='work-not-found'
    assert result['pending_proposal_request_ids']==[first['request_id'],second['request_id']]
    assert counts(store)=={'intents':0,'keys':0}


@pytest.mark.parametrize('change',['principal','workspace','grant','authority'])
def test_changed_authenticated_caller_cannot_admit_capture(store,tmp_path,pg_test_scope,change):
    store,app,binding,path,request=setup(store,tmp_path,pg_test_scope);capture(path,request,binding)
    changed=copy.deepcopy(PROPOSER)
    if change=='principal':changed.identity.principal_id='github:999:0'
    if change=='workspace':changed.identity.workspace_id='different-workspace'
    if change=='grant':changed.identity.grant_id='different-grant'
    if change=='authority':changed.identity.authorities=frozenset({'work:read'})
    result=sync(path,binding,lambda op,args:app.invoke(op,args,changed))
    assert result['pending_proposal_request_ids']
    assert counts(store)=={'intents':0,'keys':0}


def test_crash_after_owner_commit_before_local_confirmation(store,tmp_path,pg_test_scope,monkeypatch):
    store,app,binding,path,request=setup(store,tmp_path,pg_test_scope);first=capture(path,request,binding)
    original=intake._attempt
    def crash(conn,request_id,attempt,operation,phase,**kwargs):
        if phase=='confirmed':raise SystemExit('local process terminated before confirmation')
        return original(conn,request_id,attempt,operation,phase,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(intake,'_attempt',crash)
        with pytest.raises(SystemExit):sync(path,binding,lambda op,args:app.invoke(op,args,PROPOSER))
    assert counts(store)=={'intents':1,'keys':1}
    assert intake.status(path)['proposal_request_states'][0]['latest_attempt']['phase']=='started'
    assert sync(path,binding,lambda op,args:app.invoke(op,args,PROPOSER))['confirmed_proposal_request_ids']==[first['request_id']]
    assert counts(store)=={'intents':1,'keys':1}


def test_independent_producers_reconcile_one_owner_proposal(store,tmp_path,pg_test_scope):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tests.pg._shared import _PG_URL,psycopg,dict_row,assert_disposable_connection
    store,app,binding,path,request=setup(store,tmp_path,pg_test_scope)
    paths=[path,tmp_path/'second.db'];captures=[capture(p,request,binding) for p in paths]
    store.conn.commit();barrier=threading.Barrier(2)
    def run(path):
        conn=psycopg.connect(_PG_URL,row_factory=dict_row);assert_disposable_connection(conn)
        try:
            owner=WorkApplication.postgres(replace(store,conn=conn))
            def invoke(op,args):
                if op==intake.OPERATION:barrier.wait(timeout=10)
                return owner.invoke(op,args,PROPOSER)
            return sync(path,binding,invoke)
        finally:conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,paths))
    for result,captured in zip(results,captures,strict=True):assert result['confirmed_proposal_request_ids']==[captured['request_id']]
    assert counts(store)=={'intents':1,'keys':1}
