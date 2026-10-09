"""Closed offline proposal intent and conservative native confirmation."""
import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sprintctl import effect_intent, outbox, proposal_intake as intake, served


def payload(repo_id='repo'):
    binding={'repo_id':repo_id,'run_id':'run_'+'A'*26,'principal_id':'principal','workspace_id':'workspace','client_id':None,'grant_id':None}
    request={'schema_version':intake.SCHEMA,'operation':intake.OPERATION,'arguments':{'run_id':binding['run_id'],'item_id':1,'repository':'example','base_commit':'b'*40,'title':' title ','rationale':' why\n','unified_diff':'--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b \n','idempotency_key':'proposal-key'}}
    return request,binding


def capture(path,request,binding):
    return intake.capture(path,(json.dumps(request,indent=2)+'\n').encode(),json.dumps(binding).encode(),repo_id=binding['repo_id'])


def receipt(request,binding,**overrides):
    args=request['arguments']; intent={k:v for k,v in args.items() if k!='idempotency_key'}
    intent.update(intent_id='intent_'+'B'*26,revision=1,state='proposed',proposer_principal=binding['principal_id'],created_at='2026-10-09T00:00:00Z',acceptance=None,rejection=None,application=None)
    intent['canonical_intent_digest']=effect_intent.canonical_intent_digest(intent)
    intent.update(overrides)
    return {'repo_id':binding['repo_id'],'intent':intent}


class Refused(Exception):
    code='permission-denied'
    status_code=403


def test_original_bytes_whitespace_and_append_only(tmp_path):
    request,binding=payload();path=tmp_path/'producer.db'; first=capture(path,request,binding)
    raw=(json.dumps(request,indent=2)+'\n').encode()
    second=intake.capture(path,json.dumps(request).encode(),json.dumps(binding).encode(),repo_id='repo')
    assert first['request_id']==second['request_id'] and second['duplicate']
    assert first['source_sha256']==hashlib.sha256(raw).hexdigest()
    conn=outbox.open_outbox(path)
    assert bytes(conn.execute('SELECT source FROM native_proposal_request').fetchone()[0])==raw
    assert conn.execute('SELECT count(*) FROM outbox_record').fetchone()[0]==0
    intake._attempt(conn,first['request_id'],'started-attempt',intake.RESOLVE,'started')
    for table in intake.TABLES:
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):conn.execute(f'DELETE FROM {table}')
    conn.close()
    changed=copy.deepcopy(request);changed['arguments']['unified_diff']=changed['arguments']['unified_diff'].rstrip()
    with pytest.raises(ValueError,match='already captured'):capture(path,changed,binding)
    changed_binding={**binding,'grant_id':'different-grant'}
    with pytest.raises(ValueError,match='already captured'):capture(path,request,changed_binding)


@pytest.mark.parametrize('change',[{'item_id':True},{'item_id':0},{'title':''},{'title':'x'*201},{'repository':'x'*201},{'rationale':'x'*8001},{'unified_diff':'x'*1_000_001},{'unified_diff':'x\0y'},{'title':'\ud800'},{'base_commit':'A'*40},{'base_commit':'b'*41},{'idempotency_key':'short'},{'run_id':'run_'+'B'*26},{'extra':1}])
def test_invalid_capture_has_no_producer_effect(tmp_path,change):
    request,binding=payload();request['arguments'].update(change);path=tmp_path/'producer.db'
    with pytest.raises(ValueError):capture(path,request,binding)
    assert not path.exists()


@pytest.mark.parametrize('raw',[b'{"x":NaN}',b'{"x":1,"x":2}',b'{"access_token":"secret"}'])
def test_invalid_json_never_persisted(tmp_path,raw):
    _,binding=payload();path=tmp_path/'producer.db'
    with pytest.raises(ValueError):intake.capture(path,raw,json.dumps(binding).encode(),repo_id='repo')
    assert not path.exists()


@pytest.mark.parametrize('state,revision',[('proposed',1),('accepted',2),('rejected',2),('applied',3)])
def test_replay_confirms_current_lifecycle_without_rewriting_content(tmp_path,state,revision):
    request,binding=payload();path=tmp_path/'producer.db';first=capture(path,request,binding);calls=[]
    def invoke(op,args):
        calls.append((op,args))
        return binding if op==intake.RESOLVE else receipt(request,binding,state=state,revision=revision)
    result=intake.synchronize(path,repo_id='repo',invoke=invoke,rejection_type=Refused)
    assert result['confirmed_proposal_request_ids']==[first['request_id']]
    assert calls==[(intake.RESOLVE,{'run_id':binding['run_id']}),(intake.OPERATION,request['arguments'])]
    assert intake.synchronize(path,repo_id='repo',invoke=lambda *a:pytest.fail('already confirmed'),rejection_type=Refused)['proposal_attempts']==[]


@pytest.mark.parametrize('failure',['binding','content','principal','digest','reply-loss','refusal'])
def test_unconfirmed_head_stops_fifo(tmp_path,failure):
    request,binding=payload();path=tmp_path/'producer.db';first=capture(path,request,binding)
    later=copy.deepcopy(request);later['arguments']['idempotency_key']='later-proposal';second=capture(path,later,binding);calls=[]
    def invoke(op,args):
        calls.append(op)
        if op==intake.RESOLVE:return {**binding,'grant_id':'wrong'} if failure=='binding' else binding
        if failure=='reply-loss':raise OSError('unknown')
        if failure=='refusal':raise Refused('untrusted message')
        mutations={'content':{'unified_diff':'wrong'},'principal':{'proposer_principal':'wrong'},'digest':{'canonical_intent_digest':'c'*64}}
        return receipt(request,binding,**mutations[failure])
    result=intake.synchronize(path,repo_id='repo',invoke=invoke,rejection_type=Refused)
    assert result['pending_proposal_request_ids']==[first['request_id'],second['request_id']]
    assert len(result['proposal_attempts'])==1
    assert result['proposal_attempts'][0]['phase']==('rejected' if failure=='refusal' else 'unknown')
    assert len(calls)==(1 if failure=='binding' else 2)


def test_interrupted_started_attempt_retries_exact_intent(tmp_path):
    request,binding=payload();path=tmp_path/'producer.db';first=capture(path,request,binding)
    conn=outbox.open_outbox(path);intake._attempt(conn,first['request_id'],'dead-process',intake.OPERATION,'started');conn.close()
    result=intake.synchronize(path,repo_id='repo',invoke=lambda op,args:binding if op==intake.RESOLVE else receipt(request,binding),rejection_type=Refused)
    assert result['confirmed_proposal_request_ids']==[first['request_id']]


def test_readonly_status_refuses_corrupted_confirmation(tmp_path):
    request,binding=payload();path=tmp_path/'producer.db';first=capture(path,request,binding)
    intake.synchronize(path,repo_id='repo',invoke=lambda op,args:binding if op==intake.RESOLVE else receipt(request,binding),rejection_type=Refused)
    conn=outbox.open_outbox(path);conn.execute('DROP TRIGGER native_proposal_attempt_update');conn.execute("UPDATE native_proposal_attempt SET result_sha256='wrong' WHERE phase='confirmed'");conn.commit();conn.close()
    with pytest.raises(ValueError,match='integrity'):intake.status(path)


@pytest.mark.parametrize("operation", [intake.OPERATION, intake.BOUND_OPERATION])
def test_proposal_transport_pins_credential_and_keeps_key_in_arguments(monkeypatch, operation):
    resolved,tokens,calls=[],[],[]
    def resolve(ref):resolved.append(ref);return 'identity-A' if len(resolved)==1 else 'identity-B'
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def invoke(self,operation,arguments,**kwargs):
            assert kwargs=={'repo_id':'repo'}
            tokens.append(self.resolver('reference'));calls.append((operation,arguments));return {}
    def client(profile,*,credential_resolver):obj=Client();obj.resolver=credential_resolver;return obj
    monkeypatch.setattr(served,'resolve_file_credential',resolve);monkeypatch.setattr(served,'_client',client)
    call=served.native_proposal_invoker(SimpleNamespace(credential_ref='reference'),repo_id='repo')
    assert resolved==[];call(intake.RESOLVE,{});call(operation,{'idempotency_key':'argument-key'})
    assert resolved==['reference'] and tokens==['identity-A','identity-A']
    assert calls[1][1]=={'idempotency_key':'argument-key'}
    with pytest.raises(ValueError,match='unsupported'):call('work.effect.accept-v1',{})


from sprintctl.cli import cli
from tests.test_served_authority_sync import _configure_served_repo,_requires_312,_rollout_paths


@_requires_312
@pytest.mark.parametrize("bound", [False, True])
def test_cli_capture_status_sync_and_batch_separation(runner,tmp_path,monkeypatch,bound):
    _configure_served_repo(tmp_path,monkeypatch);request,binding=payload(tmp_path.name)
    response = receipt
    if bound:
        from tests.test_bound_proposal_intake import bound_payload, bound_receipt
        request, binding = bound_payload(); binding['repo_id'] = tmp_path.name
        response = bound_receipt
    source=tmp_path/'request.json';source.write_text(json.dumps(request));run_binding=tmp_path/'binding.json';run_binding.write_text(json.dumps(binding))
    queued=runner.invoke(cli,['authority','proposal-queue','--request',str(source),'--run-binding',str(run_binding)])
    assert queued.exit_code==0,queued.output;identity=json.loads(queued.output)['request_id']
    status=runner.invoke(cli,['authority','proposal-status']);assert status.exit_code==0
    assert json.loads(status.output)['pending_proposal_request_ids']==[identity]
    monkeypatch.setattr(served,'native_proposal_invoker',lambda *a,**k:pytest.fail('batch silently dispatched proposal'))
    batch=runner.invoke(cli,['authority','sync','--json']);assert batch.exit_code==0,batch.output
    assert json.loads(batch.output)['pending_proposal_request_ids']==[identity]
    calls=[]
    def factory(profile,*,repo_id):
        assert repo_id==tmp_path.name
        def invoke(op,args):
            calls.append((op,args));return binding if op==intake.RESOLVE else response(request,binding)
        return invoke
    monkeypatch.setattr(served,'native_proposal_invoker',factory)
    result=runner.invoke(cli,['authority','proposal-sync']);assert result.exit_code==0,result.output
    assert json.loads(result.output)['confirmed_proposal_request_ids']==[identity]
    assert calls[1]==(request['operation'],request['arguments'])


@_requires_312
def test_batch_reports_corrupt_proposal_status_explicitly(runner,tmp_path,monkeypatch):
    _configure_served_repo(tmp_path,monkeypatch)
    def unavailable(path):raise ValueError('damaged history')
    monkeypatch.setattr(intake,'status',unavailable)
    result=runner.invoke(cli,['authority','sync','--json']);assert result.exit_code==0,result.output
    report=json.loads(result.output)
    assert report['pending_proposal_request_ids'] is None
    assert report['native_proposal_status_error']=='native-proposal-status-unavailable'
