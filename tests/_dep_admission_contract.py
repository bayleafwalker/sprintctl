"""Before-extraction actual backend oracle for dependency admission and ownership."""
from contextlib import contextmanager
from dataclasses import dataclass
import json

import pytest


class LookupFailure(RuntimeError):
    pass


class FinalReadFailure(RuntimeError):
    pass


class NoCommit:
    """Test-owned forwarding mutant: actual SQL runs, but its commit is omitted."""
    def __init__(self, conn):
        self.conn = conn
    def commit(self):
        pass
    def __getattr__(self, name):
        return getattr(self.conn, name)


@dataclass
class DepDriver:
    api: object
    handle: object
    items: tuple
    independent: object
    insert_fault: object
    proxy_handle: object
    is_pg: bool

    @property
    def conn(self):
        return self.handle.conn if self.is_pg else self.handle

    @property
    def adapter(self):
        return self.api._DepPg if self.is_pg else self.api._DepSqlite

    def tx(self):
        return self.conn.info.transaction_status.name if self.is_pg else ('ACTIVE' if self.conn.in_transaction else 'IDLE')

    def add(self, a=None, b=None, *, handle=None):
        return self.api.add_dep(self.handle if handle is None else handle,
                               self.items[0] if a is None else a,
                               self.items[1] if b is None else b)

    def pending(self):
        self.conn.execute('BEGIN')
        ph = '%s' if self.is_pg else '?'
        self.conn.execute(f'UPDATE work_item SET assignee={ph} WHERE id={ph}', ('pending caller', self.items[2]))
        self.conn.execute('SAVEPOINT caller_boundary')

    def snapshot(self, *, own=False):
        @contextmanager
        def local():
            yield self.handle
        with (local() if own else self.independent()) as handle:
            conn = handle.conn if self.is_pg else handle
            result = {}
            for table in ('dep', 'work_item', 'event', 'reservation'):
                where, args = (' WHERE repo_id=%s', (handle.repo_id,)) if self.is_pg else ('', ())
                rows = conn.execute(f'SELECT * FROM {table}{where} ORDER BY id', args).fetchall()
                result[table] = [dict(row) for row in rows]
            return json.loads(json.dumps(result, default=str, sort_keys=True))

    def caller_assignee(self):
        return next(x['assignee'] for x in self.snapshot(own=True)['work_item'] if x['id'] == self.items[2])


def retained(record_property, name, **values):
    record_property('dep_admission_history', json.dumps({'case': name, **values}, default=str, sort_keys=True))


def assert_pending(d):
    assert d.tx() == ('INTRANS' if d.is_pg else 'ACTIVE'), 'caller transaction lost'
    assert d.caller_assignee() == 'pending caller', 'caller pending write lost'


def assert_durable_edge(d, result):
    state = d.snapshot()
    assert any(r['id'] == result and (r['item_id'], r['blocked_item_id']) == d.items[:2]
               for r in state['dep']), 'edge was not committed'
    assert next(r['assignee'] for r in state['work_item'] if r['id'] == d.items[2]) == 'pending caller', 'caller write was not committed'
    return state


class DepAdmissionCases:
    @pytest.mark.parametrize('case', ['self-existing', 'self-missing', 'both-missing', 'missing-first', 'missing-second'])
    def test_guard_order_preserves_caller_and_all_durable_rows(self, dep_driver, monkeypatch, record_property, case):
        d = dep_driver
        a, b = d.items[:2]
        missing1, missing2 = -81001, -81002
        values = {'self-existing':(a,a,[],'An item cannot depend on itself'),
                  'self-missing':(missing1,missing1,[],'An item cannot depend on itself'),
                  'both-missing':(missing1,missing2,[missing1],f'Work item #{missing1} not found'),
                  'missing-first':(missing1,b,[missing1],f'Work item #{missing1} not found'),
                  'missing-second':(a,missing2,[a,missing2],f'Work item #{missing2} not found')}
        first, second, expected_calls, error = values[case]
        original = d.api.get_work_item
        calls=[]
        def lookup(handle, ident):
            calls.append(ident)
            return original(handle, ident)
        monkeypatch.setattr(d.api, 'get_work_item', lookup)
        monkeypatch.setattr(d.api, '_DepPg' if d.is_pg else '_DepSqlite', lambda *_: pytest.fail('guard reached storage adapter'))
        before=d.snapshot()
        d.pending()
        try:
            with pytest.raises(ValueError) as caught:
                d.add(first,second)
            assert type(caught.value) is ValueError and caught.value.args == (error,)
            assert calls == expected_calls, 'guard lookup order'
            assert_pending(d)
            assert d.snapshot() == before
            d.conn.execute('ROLLBACK TO SAVEPOINT caller_boundary')
            assert_pending(d)
            retained(record_property,case,before=before,after=d.snapshot(),calls=calls,tx=d.tx(),error=caught.value.args)
        finally:
            d.conn.rollback()
        assert d.snapshot()==before

    @pytest.mark.parametrize('outcome',['success','exception'])
    def test_second_lookup_resolves_current_backend_binding(self,dep_driver,monkeypatch,record_property,outcome):
        d=dep_driver;original=d.api.get_work_item;calls=[];sentinel=LookupFailure('actual second lookup')
        def second(handle,ident):
            calls.append(('second',ident))
            if outcome=='exception':raise sentinel
            return original(handle,ident)
        def first(handle,ident):
            calls.append(('first',ident));row=original(handle,ident)
            monkeypatch.setattr(d.api,'get_work_item',second)
            return row
        monkeypatch.setattr(d.api,'get_work_item',first)
        before=d.snapshot();d.pending()
        try:
            if outcome=='exception':
                with pytest.raises(LookupFailure) as caught:d.add()
                assert caught.value is sentinel
                assert_pending(d);assert d.snapshot()==before
                d.conn.execute('ROLLBACK TO SAVEPOINT caller_boundary')
            else:
                result=d.add();assert_durable_edge(d,result)
            assert calls==[('first',d.items[0]),('second',d.items[1])], 'dynamic second lookup'
            retained(record_property,'dynamic-'+outcome,calls=calls,durable=d.snapshot(),tx=d.tx())
        finally:d.conn.rollback()

    @pytest.mark.parametrize('duplicate',[False,True])
    def test_insert_and_duplicate_keep_commit_before_final_id_read(self,dep_driver,record_property,duplicate):
        d=dep_driver
        if duplicate: original_id=d.add();d.conn.commit()
        before=d.snapshot();d.pending()
        try:
            result=d.add()
            assert type(result) is int
            assert d.tx()==('INTRANS' if d.is_pg else 'IDLE'), 'final read transaction state'
            after=assert_durable_edge(d,result)
            if duplicate:
                assert result==original_id and after['dep']==before['dep']
            else:
                assert len(after['dep'])==len(before['dep'])+1
            assert after['event']==before['event'] and after['reservation']==before['reservation']
            retained(record_property,'duplicate' if duplicate else 'insert',before=before,after=after,result=result,tx=d.tx())
        finally:d.conn.rollback()

    def test_reverse_cross_sprint_and_active_endpoints_remain_legal(self,dep_driver,record_property):
        d=dep_driver;a,b,c=d.items
        first=d.add(a,b);reverse=d.add(b,a);cross=d.add(a,c)
        state=d.snapshot();edges={(r['item_id'],r['blocked_item_id']) for r in state['dep']}
        assert {(a,b),(b,a),(a,c)}<=edges
        assert len({first,reverse,cross})==3
        retained(record_property,'valid-edge-shapes',rows=state['dep'])
        d.conn.rollback()

    def test_real_insert_error_keeps_backend_failure_state_until_caller_rollback(self,dep_driver,record_property):
        d=dep_driver;before=d.snapshot()
        with d.insert_fault():
            d.pending()
            try:
                with pytest.raises(Exception) as caught:d.add()
                assert 'injected dependency insert' in str(caught.value)
                assert d.tx()==('INERROR' if d.is_pg else 'ACTIVE')
                assert d.snapshot()==before
                if not d.is_pg:
                    assert_pending(d);d.conn.execute('ROLLBACK TO SAVEPOINT caller_boundary')
                retained(record_property,'actual-insert-error',exception=type(caught.value).__name__,tx=d.tx(),durable=d.snapshot())
            finally:d.conn.rollback()
            assert d.snapshot()==before

    def test_real_final_read_failure_does_not_undo_committed_effect(self,dep_driver,monkeypatch,record_property):
        d=dep_driver;adapter=d.adapter;original=adapter.query_one;sentinel=FinalReadFailure('after actual ID select');reads=[]
        def fail(owner,sql,params):
            row=original(owner,sql,params);reads.append(row);raise sentinel
        monkeypatch.setattr(adapter,'query_one',fail)
        before=d.snapshot();d.pending()
        try:
            with pytest.raises(FinalReadFailure) as caught:d.add()
            assert caught.value is sentinel and len(reads)==1
            assert d.tx()==('INTRANS' if d.is_pg else 'IDLE')
            state=assert_durable_edge(d,reads[0]['id'])
            assert state['event']==before['event'] and state['reservation']==before['reservation']
            d.conn.rollback();assert d.snapshot()==state
            retained(record_property,'after-commit-read-failure',durable=state,result=reads[0])
        finally:d.conn.rollback()

    def test_commit_omission_falsifier_fails_durable_edge_assertion(self,dep_driver,record_property):
        d=dep_driver;before=d.snapshot();d.pending()
        try:
            result=d.add(handle=d.proxy_handle(NoCommit(d.conn)))
            assert_pending(d)
            with pytest.raises(AssertionError,match='edge was not committed') as caught:assert_durable_edge(d,result)
            assert d.snapshot()==before
            retained(record_property,'falsifier-omit-commit',reason=str(caught.value),durable=d.snapshot())
        finally:d.conn.rollback()

    def test_refusal_rollback_falsifier_fails_caller_assertion(self,dep_driver,monkeypatch,record_property):
        d=dep_driver;original=d.api.get_work_item
        def rollback(handle,ident):
            row=original(handle,ident);d.conn.rollback();return row
        monkeypatch.setattr(d.api,'get_work_item',rollback)
        before=d.snapshot();d.pending()
        try:
            with pytest.raises(ValueError,match='not found'):d.add(d.items[0],-81001)
            with pytest.raises(AssertionError,match='caller transaction lost') as caught:assert_pending(d)
            assert d.snapshot()==before
            retained(record_property,'falsifier-refusal-rollback',reason=str(caught.value),durable=d.snapshot())
        finally:d.conn.rollback()

    @pytest.mark.parametrize('case', ['self-existing', 'self-missing', 'both-missing', 'missing-first', 'missing-second'])
    def test_idle_guard_lookup_transaction_consequence(self,dep_driver,monkeypatch,record_property,case):
        d=dep_driver;a,b=d.items[:2];missing1,missing2=-81001,-81002
        values={'self-existing':(a,a,[],'An item cannot depend on itself'),
                'self-missing':(missing1,missing1,[],'An item cannot depend on itself'),
                'both-missing':(missing1,missing2,[missing1],f'Work item #{missing1} not found'),
                'missing-first':(missing1,b,[missing1],f'Work item #{missing1} not found'),
                'missing-second':(a,missing2,[a,missing2],f'Work item #{missing2} not found')}
        first,second,expected_calls,error=values[case];calls=[];original=d.api.get_work_item
        def lookup(handle,ident):calls.append(ident);return original(handle,ident)
        monkeypatch.setattr(d.api,'get_work_item',lookup)
        monkeypatch.setattr(d.api,'_DepPg' if d.is_pg else '_DepSqlite',lambda *_:pytest.fail('guard reached storage adapter'))
        before=d.snapshot();assert d.tx()=='IDLE'
        try:
            with pytest.raises(ValueError) as caught:d.add(first,second)
            assert type(caught.value) is ValueError and caught.value.args==(error,)
            assert calls==expected_calls
            observed=d.tx()  # Measure before any test-owned same-connection reads.
            assert observed==('INTRANS' if d.is_pg and expected_calls else 'IDLE'), 'idle guard transaction consequence'
            assert d.snapshot()==before
            retained(record_property,'idle-'+case,tx=observed,calls=calls,durable=before)
        finally:d.conn.rollback()
