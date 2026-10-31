"""Actual read-only owner currentness and narrow live delegation histories."""
from dataclasses import replace
import hashlib
import threading
from types import SimpleNamespace
import uuid

import jsonschema
import pytest

from sprintctl import pg, work_currentness
from sprintctl.application import ApplicationRejection
from sprintctl.delegation import DelegatedRunContext
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg
from tests.pg.test_outcome_current_basis import store
from tests.pg.test_work_lease import A, B, _items, _run, _claim, _invoke

pytestmark=PG_MARKS
OP=work_currentness.OPERATION
CONTRACT={entry.name:entry for entry in WORK_OPERATION_CONTRACTS}


def setup(store):
    item=_items(store)[0]
    pg.update_work_item_description(store,item,'Exact full fixture requirements\nSecond line.',actor='fixture')
    reserve=pg.reserve(store,item,actor='fixture',session_id=uuid.uuid4().hex,role='execution')
    pg.release_reservation(store,reserve['id'])
    run=_run(store,A)
    lease=_claim(store,A,item,run,uuid.uuid4().hex)['lease']
    basis=_invoke(store,'work.run.continuation-delta-v1',{'run_id':run,'item_id':item},A)['delta']['current']
    return item,{'run_id':run,'lease_id':lease['lease_id'],'checkpoint':basis}


def invoke(store,body,context=A):
    jsonschema.validate(body,CONTRACT[OP].input_schema)
    return _invoke(store,OP,body,context)['currentness']


def counts(store):
    with store.conn.cursor() as cur:
        result={}
        for table in ('work_idempotency_ledger','work_outcome_report','work_decision','work_lease','event'):
            cur.execute(f'SELECT count(*) AS n FROM {table} WHERE repo_id=%s',(store.repo_id,))
            result[table]=cur.fetchone()['n']
        cur.execute('SELECT lease_id,heartbeat_at,state FROM work_lease WHERE repo_id=%s ORDER BY lease_id',(store.repo_id,))
        result['lease_facts']=cur.fetchall()
    store.conn.rollback()
    return result


def test_read_is_not_a_grant_does_not_renew_or_write_and_lost_reply_reads_again(store):
    _,body=setup(store)
    before=counts(store)
    discarded=invoke(store,body)
    answer=invoke(store,body)
    assert answer['schema_version']==work_currentness.SCHEMA and answer['authorizes_effect'] is False
    assert answer['checkpoint']==body['checkpoint']
    assert answer['holder']=={name:getattr(A.identity,name) for name in ('principal_id','workspace_id','client_id','grant_id')}
    assert answer['requirements_sha256']=='sha256:'+hashlib.sha256(b'Exact full fixture requirements\nSecond line.').hexdigest()
    assert answer['observed_at']<=answer['checked_at']<answer['lease_expires_at']
    assert answer['delegation'] is None
    assert discarded['checkpoint']==answer['checkpoint']
    assert counts(store)==before


@pytest.mark.parametrize('fault',['reference','goal','blocked','released','expired','unbound','superseded-run','unknown'])
def test_stale_or_unknown_owner_facts_refuse_without_mutation(store,fault):
    item,body=setup(store)
    if fault=='reference': pg.add_ref(store,item,'doc','https://example.invalid/new')
    elif fault=='goal': pg.update_work_item_description(store,item,'Changed fixture requirements',actor='fixture')
    elif fault=='blocked': pg.set_work_item_status(store,item,'blocked',actor='fixture')
    elif fault=='released':
        with store.conn.cursor() as cur: cur.execute("UPDATE work_lease SET state='released',ended_at=clock_timestamp(),end_reason='fixture' WHERE repo_id=%s AND lease_id=%s",(store.repo_id,body['lease_id']))
        store.conn.commit()
    elif fault=='expired':
        with store.conn.cursor() as cur: cur.execute("UPDATE work_lease SET heartbeat_at=clock_timestamp()-interval '2 hours' WHERE repo_id=%s AND lease_id=%s",(store.repo_id,body['lease_id']))
        store.conn.commit()
    elif fault=='unbound':
        with store.conn.cursor() as cur: cur.execute('UPDATE work_lease SET basis_item_revision=NULL,basis_release_digest=NULL WHERE repo_id=%s AND lease_id=%s',(store.repo_id,body['lease_id']))
        store.conn.commit()
    elif fault=='superseded-run':
        _invoke(store,'work.run.register-v1',{'harness_id':'fixture','harness_build':'fixture','model_id':'fixture','recipe_id':'fixture',
            'observed_profile':{'instruction_digest':'sha256:'+'a'*64,'skill_digests':[]},'predecessor_run_id':body['run_id'],'idempotency_key':uuid.uuid4().hex},A)
    else: body['checkpoint']['current_reference_set_digest']=None
    before=counts(store)
    with pytest.raises(ApplicationRejection): invoke(store,body)
    assert counts(store)==before


@pytest.mark.parametrize('field,value',[('principal_id','other'),('workspace_id','other'),('client_id','other'),('grant_id','other')])
def test_exact_holder_binding_cannot_be_assumed_from_caller(store,field,value):
    _,body=setup(store)
    identity=SimpleNamespace(**vars(A.identity)); setattr(identity,field,value)
    context=SimpleNamespace(**{**vars(A),'identity':identity})
    with pytest.raises(ApplicationRejection) as error: invoke(store,body,context)
    assert error.value.code in ('run-not-found','lease-not-found')


def test_authority_and_closed_schema_are_required(store):
    _,body=setup(store)
    identity=SimpleNamespace(**{**vars(A.identity),'authorities':frozenset({'work:claim'})})
    context=SimpleNamespace(**{**vars(A),'identity':identity})
    with pytest.raises(ApplicationRejection) as error: invoke(store,body,context)
    assert error.value.code=='authority-required'
    with pytest.raises(jsonschema.ValidationError): invoke(store,{**body,'authorizes_effect':True})


def test_final_owner_time_refuses_expiry_during_snapshot(store,monkeypatch):
    _,body=setup(store)
    with store.conn.cursor() as cur:
        cur.execute("UPDATE work_lease SET ttl_seconds=30,heartbeat_at=clock_timestamp()-interval '29 seconds' WHERE repo_id=%s AND lease_id=%s",(store.repo_id,body['lease_id']))
    store.conn.commit()
    original=pg.owner_read_facts
    def slow_snapshot(snapshot,*args,**kwargs):
        result=original(snapshot,*args,**kwargs)
        with snapshot.conn.cursor() as cur: cur.execute('SELECT pg_sleep(1.2)')
        return result
    monkeypatch.setattr(pg,'owner_read_facts',slow_snapshot)
    with pytest.raises(ApplicationRejection) as error: invoke(store,body)
    assert error.value.code=='lease-expired'


def test_snapshot_is_coherent_during_independent_edit_and_next_read_refuses(store,monkeypatch):
    item,body=setup(store)
    original=pg.owner_read_facts
    changed=[]
    def concurrent_edit(snapshot,*args,**kwargs):
        def writer():
            conn=psycopg.connect(_PG_URL,row_factory=dict_row)
            try:
                assert_disposable_connection(conn)
                other=pg.PgStore(conn=conn,repo_id=store.repo_id,authority_repo_uuid=store.authority_repo_uuid)
                pg.add_ref(other,item,'doc','https://example.invalid/during-snapshot')
                changed.append(True)
            finally: conn.close()
        thread=threading.Thread(target=writer); thread.start(); thread.join(timeout=5)
        assert not thread.is_alive() and changed
        return original(snapshot,*args,**kwargs)
    monkeypatch.setattr(pg,'owner_read_facts',concurrent_edit)
    observed=invoke(store,body)
    assert observed['checkpoint']==body['checkpoint']  # old, coherent RR snapshot
    monkeypatch.setattr(pg,'owner_read_facts',original)
    with pytest.raises(ApplicationRejection) as error: invoke(store,body)
    assert error.value.code=='work-basis-stale'


def delegated_scope(store,body,*,ttl=300,operations=None):
    from sprintctl.application import WorkApplication
    from sprintctl import delegation
    from tests.pg.test_run_delegation import _instance,_declaration
    app=WorkApplication.postgres(store)
    instance=_instance()
    _invoke(store,'work.agent.register-v1',{'instance_id':instance,'declaration':_declaration(),
        'idempotency_key':uuid.uuid4().hex},A)
    minted=_invoke(store,delegation.OP_DELEGATE,{'run_id':body['run_id'],'creator_instance_id':instance,
        'operations':operations or [OP],'ttl_seconds':ttl,'idempotency_key':uuid.uuid4().hex},A)
    resolved=app.resolve_run_delegation_digest(hashlib.sha256(minted['token'].encode('ascii')).hexdigest())
    identity=SimpleNamespace(**{**vars(A.identity),'authorities':resolved.authorities,'delegation':resolved.delegation})
    return minted,SimpleNamespace(**{**vars(A),'identity':identity})


def test_exact_read_only_delegate_rechecks_scope_revocation_and_cannot_write(store):
    _,body=setup(store)
    minted,context=delegated_scope(store,body)
    assert context.identity.authorities==frozenset({'work:read'})
    before=counts(store)
    result=invoke(store,body,context)
    assert result['delegation']['delegation_id']==minted['delegation_id']
    assert result['delegation']['operations']==[OP]
    assert counts(store)==before
    with pytest.raises(ApplicationRejection) as denied:
        _invoke(store,'work.lease.acquire-v1',{'run_id':body['run_id'],'item_id':body['checkpoint']['item_id'],
            'idempotency_key':uuid.uuid4().hex},context)
    assert denied.value.code=='delegation-operation-not-allowed'
    with pytest.raises(ApplicationRejection):
        _invoke(store,'work.session-note.write-v1',{'run_id':body['run_id'],'note':'forbidden write',
            'idempotency_key':uuid.uuid4().hex},context)
    assert counts(store)==before
    _invoke(store,'work.run.delegation-revoke-v1',{'delegation_id':minted['delegation_id'],'reason':'fixture-revoked',
        'idempotency_key':uuid.uuid4().hex},A)
    before=counts(store)
    # Reuse the previously resolved identity. The owner must re-read its live
    # scope; resolver/cache success is not current permission.
    with pytest.raises(ApplicationRejection) as revoked: invoke(store,body,context)
    assert revoked.value.code=='credential-invalid'
    assert counts(store)==before


def test_existing_delegate_does_not_gain_new_operation_implicitly(store):
    _,body=setup(store)
    _,context=delegated_scope(store,body,operations=['work.run.resolve-v1','work.run.predecessor-context-v1'])
    with pytest.raises(ApplicationRejection) as denied: invoke(store,body,context)
    assert denied.value.code=='delegation-operation-not-allowed'


def test_delegation_expiry_is_rechecked_at_final_owner_time(store,monkeypatch):
    _,body=setup(store)
    _,context=delegated_scope(store,body,ttl=1)
    original=pg.owner_read_facts
    def slow_snapshot(snapshot,*args,**kwargs):
        result=original(snapshot,*args,**kwargs)
        with snapshot.conn.cursor() as cur: cur.execute('SELECT pg_sleep(1.2)')
        return result
    monkeypatch.setattr(pg,'owner_read_facts',slow_snapshot)
    with pytest.raises(ApplicationRejection) as error: invoke(store,body,context)
    assert error.value.code=='credential-invalid'
