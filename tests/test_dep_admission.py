"""Real SQLite half and bounded wrapper-call characterization of admission."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from sprintctl import db, pg, depcore
from tests._dep_admission_contract import DepDriver, DepAdmissionCases


@pytest.fixture
def dep_driver(conn,db_path,active_sprint):
    sid=active_sprint['id'];tid=db.get_or_create_track(conn,sid,'dep oracle')
    a=db.create_work_item(conn,sid,tid,'first')
    b=db.create_work_item(conn,sid,tid,'second')
    db.set_work_item_status(conn,a,'active')
    other=db.create_sprint(conn,'other dep sprint','goal','2026-10-19','2026-10-20','planned')
    track=db.get_or_create_track(conn,other,'dep oracle')
    c=db.create_work_item(conn,other,track,'unrelated caller item',assignee='original')
    conn.commit()
    @contextmanager
    def independent():
        other=sqlite3.connect(db_path,timeout=.5);other.row_factory=sqlite3.Row
        try:yield other
        finally:other.close()
    @contextmanager
    def fault():
        conn.execute(f"CREATE TEMP TRIGGER dep_admission_fault BEFORE INSERT ON dep WHEN NEW.item_id={a} BEGIN SELECT RAISE(ABORT,'injected dependency insert'); END")
        conn.commit()
        try:yield
        finally:
            conn.rollback();conn.execute('DROP TRIGGER dep_admission_fault');conn.commit()
    return DepDriver(db,conn,(a,b,c),independent,fault,lambda proxy:proxy,False)


class TestSqliteDepAdmission(DepAdmissionCases):
    pass


@pytest.mark.parametrize('api',[db,pg],ids=['sqlite-wrapper','pg-wrapper'])
@pytest.mark.parametrize('ids',[(True,1),('010','020')])
def test_wrapper_preserves_raw_equality_ids_and_falsey_non_none(api,monkeypatch,ids):
    """Synthetic values characterize forwarding only, not legal persisted rows."""
    a,b=ids;handle=object();calls=[];sentinel=object()
    monkeypatch.setattr(api,'get_work_item',lambda h,i:calls.append(('lookup',i)) or {})
    monkeypatch.setattr(api,'_DepPg' if api is pg else '_DepSqlite',lambda h:calls.append(('adapter',h)) or sentinel)
    monkeypatch.setattr(depcore,'add_dep',lambda c,x,y:calls.append(('store',c,x,y)) or 73)
    if a==b:
        with pytest.raises(ValueError,match='An item cannot depend on itself'):api.add_dep(handle,a,b)
        assert calls==[], 'self guard precedes lookup'
    else:
        assert api.add_dep(handle,a,b)==73
        assert calls==[('lookup',a),('lookup',b),('adapter',handle),('store',sentinel,a,b)]


DISCRIMINATORS = ('self-first', 'endpoint-order', 'none-only', 'dynamic-lookup', 'raw-ids', 'adapter-after-guards')


def _wrong_wrapper(api, variant, handle, first, second):
    """Deliberate isolated mutants; never installed as product implementation."""
    adapter_type = api._DepPg if api is pg else api._DepSqlite
    adapter = adapter_type(handle) if variant == 'adapter-after-guards' else None
    getter = api.get_work_item if variant == 'dynamic-lookup' else lambda h,i: api.get_work_item(h,i)
    if variant == 'raw-ids':
        first,second=int(first),int(second)
    if variant == 'self-first':
        getter(handle,first)
    if first == second:
        raise ValueError('An item cannot depend on itself')
    order = (second,first) if variant == 'endpoint-order' else (first,second)
    for ident in order:
        row=getter(handle,ident)
        missing = not row if variant == 'none-only' else row is None
        if missing:
            raise ValueError(f'Work item #{ident} not found')
    return depcore.add_dep(adapter if adapter is not None else adapter_type(handle),first,second)


def _assert_wrapper_discriminator(api, call, name, monkeypatch):
    calls=[];handle=object();adapter=object()
    first,second=(True,1) if name=='self-first' else (('010','020') if name=='raw-ids' else (31,32))
    def replacement(h,ident):
        calls.append(('replacement',ident));return {}
    def lookup(h,ident):
        assert h is handle
        calls.append(('lookup',ident))
        if name=='dynamic-lookup':monkeypatch.setattr(api,'get_work_item',replacement)
        return None if name in ('endpoint-order','adapter-after-guards') else {}
    monkeypatch.setattr(api,'get_work_item',lookup)
    monkeypatch.setattr(api,'_DepPg' if api is pg else '_DepSqlite',lambda h:calls.append(('adapter',h)) or adapter)
    monkeypatch.setattr(depcore,'add_dep',lambda owner,a,b:calls.append(('store',owner,a,b)) or 73)
    try:
        value=call(handle,first,second);error=None
    except ValueError as exc:
        value=None;error=(type(exc),exc.args)
    if name=='self-first':
        assert calls==[], name
        assert error==(ValueError,('An item cannot depend on itself',)),name
    elif name in ('endpoint-order','adapter-after-guards'):
        assert error==(ValueError,(f'Work item #{first} not found',)),name
        assert calls==[('lookup',first)],name
    else:
        expected=[('lookup',first),('replacement' if name=='dynamic-lookup' else 'lookup',second),('adapter',handle),('store',adapter,first,second)]
        assert value==73 and error is None and calls==expected,name
    return name


@pytest.mark.parametrize('api',[db,pg],ids=['sqlite-wrapper','pg-wrapper'])
@pytest.mark.parametrize('name',DISCRIMINATORS)
def test_public_wrapper_passes_discriminator_and_seeded_mutant_fails(api,monkeypatch,name,record_property):
    with monkeypatch.context() as positive:
        assert _assert_wrapper_discriminator(api,lambda h,a,b:api.add_dep(h,a,b),name,positive)==name
    with monkeypatch.context() as negative:
        with pytest.raises(AssertionError) as caught:
            _assert_wrapper_discriminator(api,lambda h,a,b:_wrong_wrapper(api,name,h,a,b),name,negative)
        assert str(caught.value).splitlines()[0]==name, 'falsifier failed unrelated assertion'
    record_property('dep_admission_control', name)
