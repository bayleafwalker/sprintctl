"""Coordinator-owned actual-backend Release policy examples, before extraction."""
from contextlib import contextmanager
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json

import pytest
from sprintctl import releases
from tests._locked_decision_writing_contract import owner as decision_owner, connection, assert_pending


@dataclass
class Primitive:
    driver: object
    handle: object

    def basis(self, iid, **kwargs):
        d = self.driver
        if d.is_pg:
            return d.backend._release_basis_locked(self.handle, d.store.repo_id, iid, **kwargs)
        assert not kwargs
        return d.backend._release_basis(self.handle, iid)

    def freeze(self, item, revision, count, *, actor='first owner', contract=None):
        contract = {'review_required': True} if contract is None else contract
        d = self.driver
        if d.is_pg:
            return d.backend._freeze_release_locked(self.handle, d.store.repo_id, item, revision, count,
                actor=actor, acceptance_contract=contract)
        return d.backend._freeze_release(self.handle, item, revision, count,
            actor=actor, acceptance_contract=contract)

    def current(self, iid):
        d = self.driver
        if d.is_pg:
            return d.backend._current_release_locked(self.handle, d.store.repo_id, iid)
        return d.backend._current_release_row(self.handle, iid)

    def resolve(self, iid, digest):
        d = self.driver
        if d.is_pg:
            return d.backend._decision_release_locked(self.handle, d.store.repo_id, iid, digest)
        return d.backend._decision_release(self.handle, iid, digest)


@contextmanager
def owning(d, iid):
    with decision_owner(d, iid) as (_, invoke):
        yield Primitive(d, invoke.cursor if d.is_pg else connection(d))


def expected_revision(item, edit_count=0, revise_count=0):
    # Independent literal grammar and stdlib hash, never the production helper.
    digest = hashlib.sha256((item['description'] or '').encode()).hexdigest()
    return f"item:{item['aggregate_uuid']}@description:v{edit_count}@sha256:{digest}@revise:{revise_count}"


def expected_digest(item, revision, contract_json='{"review_required":true}', refs_json='[]'):
    document = ('{"acceptance_contract":'+contract_json+',"aggregate_uuid":"'+
        item['aggregate_uuid'].lower()+'","context_refs":'+refs_json+',"item_revision":"'+revision+'"}')
    return hashlib.sha256(document.encode()).hexdigest(), document


def edit(d, iid, description):
    _, revision = d.backend.get_work_item_with_edit_revision(d.store, iid)
    d.backend.update_work_item_description(d.store, iid, description,
        expected_revision=revision, actor='literal revision oracle')


class ReleasePolicyCases:
    @pytest.mark.parametrize('change', ('none', 'unicode', 'empty', 'event', 'revise', 'reject', 'withdraw', 'accept'))
    def test_real_basis_metadata_and_literal_revision(self, release_driver, change):
        d = release_driver
        iid = d.new_item('active')
        edits, revisions = 0, 0
        if change == 'unicode':
            edit(d, iid, 'Päätös β')
            edits = 1
        elif change == 'empty':
            # Historical storage allows blank descriptions; the public editor
            # correctly refuses creating one. This fixture is not that API.
            if d.is_pg:
                connection(d).execute("UPDATE work_item SET description='' WHERE repo_id=%s AND id=%s", (d.store.repo_id,iid))
            else:
                connection(d).execute("UPDATE work_item SET description='' WHERE id=?", (iid,))
            connection(d).commit()
        elif change == 'event':
            d.backend.create_event(d.store, d.sprint, 'oracle', 'note', work_item_id=iid, payload={'note': 'unrelated'})
        elif change in ('revise', 'reject', 'withdraw', 'accept'):
            d.backend.record_decision(d.store, iid, change, actor='oracle', rationale='valid kind')
            revisions = int(change == 'revise')
        original = d.snapshot(iid)
        with owning(d, iid) as p:
            item, revision, count = p.basis(iid)
            assert type(item) is dict and type(revision) is str and type(count) is int
            assert 'edit_version' not in item and 'revise_count' not in item
            assert revision == expected_revision(item, edits, revisions) and count == revisions
            assert item['id'] == iid and item['description'] == original['item']['description']
            assert_pending(d)
            assert d.snapshot(iid) == original
            with d.independent() as other:
                assert d.snapshot(iid, other) == original

    def test_missing_basis_refuses_before_description_helper(self, release_driver, monkeypatch):
        d = release_driver
        iid = d.new_item()
        target = d.backend._workitemcore if d.is_pg else d.backend
        def unexpected(*args):
            raise AssertionError('description helper must not run for missing row')
        with owning(d, iid) as p, monkeypatch.context() as patch:
            patch.setattr(target, '_description_revision', unexpected)
            with pytest.raises(ValueError, match='Work item #2000000000 not found'):
                p.basis(2_000_000_000)
            assert_pending(d)

    def test_basis_dynamic_description_lookup_sees_popped_metadata(self, release_driver, monkeypatch):
        d = release_driver
        iid = d.new_item()
        target = d.backend._workitemcore if d.is_pg else d.backend
        original = target._description_revision
        seen = []
        def revision(item, count):
            assert 'edit_version' not in item and 'revise_count' not in item
            seen.append((item, count))
            return original(item, count)
        with owning(d, iid) as p, monkeypatch.context() as patch:
            patch.setattr(target, '_description_revision', revision)
            item, value, count = p.basis(iid)
            assert len(seen) == 1 and seen[0][0] is item and seen[0][1] == 0
            assert value == expected_revision(item) and count == 0

    def test_canonical_ref_bytes_digest_and_repeat_keep_first_actor(self, release_driver, monkeypatch, record_property):
        d = release_driver
        iid = d.new_item()
        for kind, url, label in [('issue','https://issues.invalid/x','Päätös'),('doc','docs/z.md',''),('doc','docs/z.md','')]:
            d.backend.add_ref(d.store, iid, kind, url, label)
        before = d.snapshot(iid)
        contract = {'review_required': True, 'evidence_obligations': ['β', 'a']}
        contract_json = '{"evidence_obligations":["β","a"],"review_required":true}'
        refs_json = '[{"label":"","ref_type":"doc","url":"docs/z.md"},{"label":"","ref_type":"doc","url":"docs/z.md"},{"label":"Päätös","ref_type":"issue","url":"https://issues.invalid/x"}]'
        seen = []
        original = releases.canonical_json
        def capture(value):
            result = original(value)
            seen.append(result)
            return result
        with owning(d, iid) as p, monkeypatch.context() as patch:
            item, revision, count = p.basis(iid)
            expected, document = expected_digest(item, revision, contract_json, refs_json)
            patch.setattr(releases, 'canonical_json', capture)
            digest = p.freeze(item, revision, count, contract=contract)
            assert digest == expected and seen == [document, contract_json, refs_json]
            [row] = d.backend.list_releases(d.store, iid)
            assert row['actor'] == 'first owner' and row['item_revision'] == revision
            assert row['context_refs'] == json.loads(refs_json) and row['acceptance_contract'] == contract
            assert p.freeze(item, revision, count, actor='later owner', contract=contract) == digest
            assert d.backend.list_releases(d.store, iid) == [row]
            assert seen == [document, contract_json, refs_json] * 2
            assert_pending(d)
            pending = d.snapshot(iid)
            assert pending['events'] == before['events']
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
            connection(d).commit()
        with d.independent() as other:
            assert d.snapshot(iid, other) == pending
        record_property('state_history', json.dumps({'history':'canonical-freeze-first-actor',
            'before':before,'pending':pending,'digest':digest,'canonical_inputs':seen},sort_keys=True,default=str))

    @pytest.mark.parametrize('change', ('description', 'revise', 'contract', 'refs'))
    def test_each_digest_component_changes_with_real_owner_state(self, release_driver, change):
        d = release_driver
        iid = d.new_item('active')
        with owning(d, iid) as p:
            item, revision, count = p.basis(iid)
            first = p.freeze(item, revision, count)
            connection(d).commit()
        if change == 'description': edit(d, iid, 'changed')
        elif change == 'revise': d.backend.record_decision(d.store, iid, 'revise', actor='oracle')
        elif change == 'refs': d.backend.add_ref(d.store, iid, 'doc', 'docs/other.md')
        contract = {'review_required': False} if change == 'contract' else {'review_required': True}
        with owning(d, iid) as p:
            item, revision, count = p.basis(iid)
            second = p.freeze(item, revision, count, contract=contract)
            assert first != second
            assert len(d.backend.list_releases(d.store, iid)) == 2
            assert_pending(d)

    @pytest.mark.parametrize('commit', (False, True))
    def test_private_freeze_and_unrelated_owner_write_stay_pending(self, release_driver, commit, record_property):
        d = release_driver
        iid = d.new_item()
        before = d.snapshot(iid)
        old_goal = d.backend.get_sprint(d.store, d.sprint)['goal']
        with owning(d, iid) as p:
            conn = connection(d)
            if d.is_pg:
                conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',('pending Release owner',d.store.repo_id,d.sprint))
            else:
                conn.execute('UPDATE sprint SET goal=? WHERE id=?',('pending Release owner',d.sprint))
            item, revision, count = p.basis(iid)
            digest = p.freeze(item, revision, count)
            assert_pending(d)
            pending = d.snapshot(iid)
            assert len(pending['releases']) == 1 and pending['events'] == before['events']
            with d.independent() as other:
                assert d.snapshot(iid, other) == before
                assert d.backend.get_sprint(other, d.sprint)['goal'] == old_goal
            (conn.commit if commit else conn.rollback)()
        with d.independent() as other:
            assert d.snapshot(iid, other) == (pending if commit else before)
            assert d.backend.get_sprint(other,d.sprint)['goal'] == ('pending Release owner' if commit else old_goal)
        record_property('state_history',json.dumps({'history':'private-freeze-owner-'+str(commit),
            'before':before,'pending':pending,'committed':commit,'digest':digest},sort_keys=True,default=str))

    @pytest.mark.parametrize('supplied', (None, '', 'f'*64))
    def test_missing_default_and_explicit_branch(self, release_driver, supplied, monkeypatch):
        d = release_driver
        iid = d.new_item()
        name = '_current_release_locked' if d.is_pg else '_current_release_row'
        original = getattr(d.backend,name)
        reads = []
        def current(*args):
            reads.append('current')
            return original(*args)
        with owning(d,iid) as p, monkeypatch.context() as patch:
            sqls=[];raw=p.handle
            class Proxy:
                def __getattr__(self,key):return getattr(raw,key)
                def execute(self,sql,params=()):
                    sqls.append((sql,params));return raw.execute(sql,params)
            subject=Primitive(d,Proxy())
            patch.setattr(d.backend,name,current)
            if supplied is None: assert subject.resolve(iid,supplied) is None
            else:
                with pytest.raises(releases.ReleaseMismatch,match=f'release {supplied} is not a release of item #{iid}'):
                    subject.resolve(iid,supplied)
            assert reads == (['current'] if supplied is None else [])
            assert len(sqls)==1
            sql,params=sqls[0]
            assert ' '.join(sql.split()).startswith('SELECT wr.*' if supplied is None else 'SELECT 1 FROM work_release')
            assert tuple(params)==((d.store.repo_id,) if d.is_pg else ())+((iid,) if supplied is None else (supplied,iid))
            assert_pending(d)

    def test_omitted_uses_current_and_explicit_historical_remains_member(self, release_driver, monkeypatch):
        d = release_driver
        iid = d.new_item('active')
        first = d.backend.reserve(d.store,iid,actor='freeze owner',session_id='release oracle')['release_digest']
        d.backend.record_decision(d.store,iid,'revise',actor='reviewer')
        with owning(d,iid) as p:
            assert p.current(iid) is None and p.resolve(iid,None) is None
            assert p.resolve(iid,first) == first
        second = d.backend.reserve(d.store,iid,actor='freeze owner',session_id='release oracle')['release_digest']
        assert second != first
        with owning(d,iid) as p:
            assert p.resolve(iid,None) == second
            assert p.resolve(iid,first) == first
            assert p.resolve(iid,second) == second

    def test_foreign_item_release_is_refused(self, release_driver):
        d = release_driver
        iid, other = d.new_item(), d.new_item()
        digest = d.backend.reserve(d.store,other,actor='other',session_id='foreign release')['release_digest']
        with owning(d,iid) as p:
            with pytest.raises(releases.ReleaseMismatch,match='not a release of item'):
                p.resolve(iid,digest)
            assert_pending(d)

    def test_current_prioritizes_referencing_reservation_ids_even_when_released(self, release_driver, record_property):
        d = release_driver
        iid = d.new_item()
        with owning(d,iid) as p:
            item, revision, count = p.basis(iid)
            first = p.freeze(item,revision,count)
            second = p.freeze(item,revision,count,contract={'review_required':False})
            assert p.current(iid)['release_digest'] == second
            assert p.current(iid)['id'] > d.backend.get_release(d.store,first)['id']
            connection(d).commit()
        a = d.backend.reserve(d.store,iid,actor='a',session_id='older digest')
        assert a['release_digest'] == first
        assert d.backend.current_release(d.store,iid)['release_digest'] == first
        b = d.backend.reserve(d.store,iid,actor='b',session_id='newer digest',acceptance_contract={'review_required':False})
        assert b['release_digest'] == second
        d.backend.release_reservation(d.store,b['id'],actor='b')
        assert d.backend.current_release(d.store,iid)['release_digest'] == second
        later = d.backend.reserve(d.store,iid,actor='a',session_id='old digest again')
        assert later['id'] > b['id'] and later['release_digest'] == first
        # Intentional test-owned timestamp perturbation on a valid referencing
        # row, not a product clock-fault or timestamp-order guarantee.
        if d.is_pg:
            connection(d).execute('UPDATE reservation SET created_at=%s WHERE repo_id=%s AND id=%s',('2001-02-03T04:05:06Z',d.store.repo_id,later['id']))
        else:
            connection(d).execute('UPDATE reservation SET created_at=? WHERE id=?',('2001-02-03T04:05:06Z',later['id']))
        connection(d).commit()
        before = d.snapshot(iid)
        with owning(d,iid) as p:
            assert p.current(iid)['release_digest'] == first
            assert_pending(d)
            assert d.snapshot(iid) == before
        record_property('state_history',json.dumps({'history':'current-reservation-id-not-time',
            'observed':before,'expected_digest':first,'first_release_id':d.backend.get_release(d.store,first)['id']},sort_keys=True,default=str))

    @pytest.mark.parametrize('rebind_at', ('document','contract','refs'))
    def test_actual_execute_binding_surrounds_late_values(self, release_driver, rebind_at, monkeypatch, record_property):
        d = release_driver
        iid = d.new_item()
        trace = []
        contract = {'review_required': True}
        original_json = releases.canonical_json
        with owning(d,iid) as p:
            raw_item, revision, count = p.basis(iid)
            raw = p.handle
            class Item(dict):
                def __getitem__(self,key):
                    trace.append('item:'+key)
                    return super().__getitem__(key)
            class Proxy:
                def __getattribute__(self,name):
                    if name == 'execute': trace.append('bind:execute')
                    return object.__getattribute__(self,name)
                def __getattr__(self,name): return getattr(raw,name)
                def execute(self,sql,params=()):
                    trace.append('original:'+sql.lstrip().split()[0])
                    return raw.execute(sql,params)
            proxy = Proxy()
            def alternate(sql,params=()):
                trace.append('alternate:'+sql.lstrip().split()[0])
                return raw.execute(sql,params)
            def canonical(value):
                phase = 'contract' if value is contract else 'refs' if isinstance(value,list) else 'document'
                trace.append('json:'+phase)
                if phase == rebind_at: proxy.execute = alternate
                return original_json(value)
            with monkeypatch.context() as patch:
                patch.setattr(releases,'canonical_json',canonical)
                value = Primitive(d,proxy).freeze(Item(raw_item),revision,count,contract=contract)
            assert trace == ['bind:execute','item:id','original:SELECT','item:aggregate_uuid',
                'json:document','bind:execute','item:id','json:contract','json:refs',
                ('alternate' if rebind_at=='document' else 'original')+':INSERT']
            assert value == expected_digest(raw_item,revision)[0]
            assert_pending(d)
            assert len(d.backend.list_releases(d.store,iid)) == 1
        record_property('state_history',json.dumps({'history':'execute-binding-'+rebind_at,'trace':trace,'digest':value},sort_keys=True))

    def test_insert_method_lookup_fault_prevents_late_values(self, release_driver, monkeypatch):
        d = release_driver
        iid = d.new_item()
        before = d.snapshot(iid)
        phases = []
        original_json = releases.canonical_json
        with owning(d,iid) as p:
            item, revision, count = p.basis(iid)
            raw = p.handle
            class Proxy:
                lookups = 0
                def __getattribute__(self,name):
                    if name == 'execute':
                        self.lookups += 1
                        if self.lookups == 2: raise RuntimeError('actual insertion method lookup')
                    return object.__getattribute__(self,name)
                def __getattr__(self,name): return getattr(raw,name)
            contract = {'review_required': True}
            def canonical(value):
                phases.append('contract' if value is contract else 'refs' if isinstance(value,list) else 'document')
                return original_json(value)
            with monkeypatch.context() as patch:
                patch.setattr(releases,'canonical_json',canonical)
                with pytest.raises(RuntimeError,match='actual insertion method lookup'):
                    Primitive(d,Proxy()).freeze(item,revision,count,contract=contract)
            assert phases == ['document']
            assert_pending(d)
            assert d.snapshot(iid) == before

    @pytest.mark.parametrize('fault', ('contract-json','refs-json','after-insert','sql-insert'))
    def test_actual_freeze_fault_keeps_native_owner_state(self, release_driver, fault, monkeypatch, record_property):
        d = release_driver
        iid = d.new_item()
        before = d.snapshot(iid)
        old_goal = d.backend.get_sprint(d.store,d.sprint)['goal']
        observed = {}
        original_json = releases.canonical_json
        with owning(d,iid) as p:
            conn = connection(d)
            if d.is_pg:
                conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',('pending freeze fault',d.store.repo_id,d.sprint))
            else:
                conn.execute('UPDATE sprint SET goal=? WHERE id=?',('pending freeze fault',d.sprint))
            item, revision, count = p.basis(iid)
            contract = {'review_required':True}
            raw = p.handle
            class Proxy:
                def __getattr__(self,name):return getattr(raw,name)
                def execute(self,sql,params=()):
                    result = raw.execute(sql,params)
                    if fault == 'after-insert' and sql.lstrip().startswith('INSERT'):
                        raise RuntimeError('after actual release insertion')
                    return result
            def canonical(value):
                result = original_json(value)
                if (fault=='contract-json' and value is contract) or (fault=='refs-json' and isinstance(value,list)):
                    raise RuntimeError('actual insertion serialization fault')
                return result
            if fault == 'sql-insert':
                if d.is_pg:
                    from psycopg import sql
                    raw.execute(sql.SQL("CREATE FUNCTION pg_temp.release_policy_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.work_item_id = {} AND NEW.repo_id = {} THEN RAISE EXCEPTION 'actual release INSERT trigger fault'; END IF; RETURN NEW; END $$").format(sql.Literal(iid),sql.Literal(d.store.repo_id)))
                    raw.execute('CREATE TRIGGER release_policy_fault AFTER INSERT ON work_release FOR EACH ROW EXECUTE FUNCTION pg_temp.release_policy_fault()')
                else:
                    raw.execute("CREATE TEMP TRIGGER release_policy_fault AFTER INSERT ON work_release BEGIN SELECT RAISE(ABORT,'actual release INSERT trigger fault'); END")
            with monkeypatch.context() as patch:
                patch.setattr(releases,'canonical_json',canonical)
                error = d.backend.psycopg.errors.RaiseException if d.is_pg and fault=='sql-insert' else __import__('sqlite3').IntegrityError if fault=='sql-insert' else RuntimeError
                with pytest.raises(error,match='actual .*'):
                    Primitive(d,Proxy()).freeze(item,revision,count,contract=contract)
            state = d.transaction()
            with d.independent() as other:
                assert d.snapshot(iid,other)==before
                assert d.backend.get_sprint(other,d.sprint)['goal']==old_goal
            if d.is_pg and fault=='sql-insert':
                assert state=='INERROR'
                with pytest.raises(d.backend.psycopg.errors.InFailedSqlTransaction):
                    conn.execute('SELECT 1')
                observed={'transaction':state,'primary_read':'InFailedSqlTransaction'}
            else:
                assert_pending(d)
                pending=d.snapshot(iid)
                assert len(pending['releases'])==int(fault=='after-insert')
                assert pending['item']==before['item'] and pending['events']==before['events']
                assert d.backend.get_sprint(d.store,d.sprint)['goal']=='pending freeze fault'
                observed={'transaction':state,'pending':pending}
        with d.independent() as other:
            assert d.snapshot(iid,other)==before
            assert d.backend.get_sprint(other,d.sprint)['goal']==old_goal
        record_property('state_history',json.dumps({'history':'freeze-fault-'+fault,
            'before':before,'observed_before_owner_cleanup':observed},sort_keys=True,default=str))

    @pytest.mark.parametrize('fault', ('edit','revise'))
    def test_metadata_conversion_order_after_actual_query(self, release_driver, fault, monkeypatch):
        # Actual SQL supplies the row; only metadata conversion is deliberately
        # faulted afterward. This is not a state producible by COUNT(*).
        d = release_driver
        iid = d.new_item()
        before = d.snapshot(iid)
        order=[]
        target = d.backend._workitemcore if d.is_pg else d.backend
        def unexpected(*args):raise AssertionError('description lookup preceded metadata conversion')
        with owning(d,iid) as p:
            raw=p.handle
            class Number:
                def __init__(self,name,value):self.name,self.value=name,value
                def __int__(self):
                    order.append(self.name)
                    if self.name==fault:raise ValueError('test-owned '+self.name+' conversion')
                    return int(self.value)
            class Proxy:
                result=None
                def __getattr__(self,name):return getattr(raw,name)
                def execute(self,sql,params=()):
                    self.result=raw.execute(sql,params)
                    return self.result if d.is_pg else self
                def fetchone(self):
                    result=raw.fetchone() if d.is_pg else self.result.fetchone()
                    row=dict(result)
                    row['edit_version']=Number('edit',row['edit_version'])
                    row['revise_count']=Number('revise',row['revise_count'])
                    return row
            with monkeypatch.context() as patch:
                patch.setattr(target,'_description_revision',unexpected)
                with pytest.raises(ValueError,match='test-owned '+fault+' conversion'):
                    Primitive(d,Proxy()).basis(iid)
            assert order==(['edit'] if fault=='edit' else ['edit','revise'])
            assert_pending(d)
            assert d.snapshot(iid)==before

    @pytest.mark.parametrize('shape', ('none-description','empty-mapping'))
    def test_test_owned_row_shape_keeps_existing_missing_distinction(self, release_driver, shape):
        # Deliberate fetch-result characterization, not a claim that an empty
        # metadata row or SQL NULL description is legal persisted item state.
        d=release_driver
        iid=d.new_item()
        with owning(d,iid) as p:
            raw=p.handle
            class Proxy:
                result=None
                def __getattr__(self,name):return getattr(raw,name)
                def execute(self,sql,params=()):
                    self.result=raw.execute(sql,params)
                    return self.result if d.is_pg else self
                def fetchone(self):
                    result=raw.fetchone() if d.is_pg else self.result.fetchone()
                    if shape=='empty-mapping':return {}
                    row=dict(result);row['description']=None;return row
            if shape=='empty-mapping':
                with pytest.raises(KeyError,match='edit_version'):Primitive(d,Proxy()).basis(iid)
            else:
                item,revision,count=Primitive(d,Proxy()).basis(iid)
                assert item['description'] is None and revision==expected_revision(item) and count==0
            assert_pending(d)

    @pytest.mark.parametrize('route', ('basis','current'))
    def test_actual_query_boundary_capture(self, release_driver, route, record_property):
        capture_query(release_driver,route,record_property)


    def test_current_unreferenced_fallback_uses_id_for_equal_times(self, release_driver):
        d=release_driver
        iid=d.new_item()
        with owning(d,iid) as p:
            item,revision,count=p.basis(iid)
            expected=None
            # Deliberate schema/domain-valid provisional rows: identical fixed
            # timestamps, correct canonical digest/basis, no reservation yet.
            for contract_json in ['{"review_required":true}','{"review_required":false}']:
                digest,_=expected_digest(item,revision,contract_json)
                values=(iid,revision,count,digest,contract_json,'[]','query fixture','2001-02-03T04:05:06Z')
                if d.is_pg:
                    connection(d).execute('INSERT INTO work_release(repo_id,work_item_id,item_revision,revise_count,release_digest,acceptance_contract,context_refs,actor,created_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)',(d.store.repo_id,)+values)
                else:
                    connection(d).execute('INSERT INTO work_release(work_item_id,item_revision,revise_count,release_digest,acceptance_contract,context_refs,actor,created_at) VALUES (?,?,?,?,?,?,?,?)',values)
                expected=d.backend.get_release(d.store,digest)
            assert p.current(iid)==expected
            rows=d.backend.list_releases(d.store,iid)
            assert rows[0]['created_at']==rows[1]['created_at']
            assert rows[0]['id']<rows[1]['id'] and expected==rows[1]
            assert_pending(d)

    def test_refs_conversion_preserves_backend_fetch_boundary(self, release_driver, monkeypatch):
        d=release_driver
        iid=d.new_item()
        d.backend.add_ref(d.store,iid,'doc','docs/scope.md','')
        fetched=[];received=[]
        original=releases.canonical_context_refs
        with owning(d,iid) as p:
            item,revision,count=p.basis(iid)
            raw=p.handle
            class Proxy:
                result=None
                def __getattr__(self,name):return getattr(raw,name)
                def execute(self,sql,params=()):
                    self.result=raw.execute(sql,params)
                    return self.result if d.is_pg else self
                def fetchall(self):
                    rows=raw.fetchall() if d.is_pg else self.result.fetchall()
                    fetched.append(rows)
                    return rows
            def refs(rows):
                received.append(rows)
                return original(rows)
            with monkeypatch.context() as patch:
                patch.setattr(releases,'canonical_context_refs',refs)
                digest=Primitive(d,Proxy()).freeze(item,revision,count)
            assert len(fetched)==len(received)==1
            assert all(type(row) is dict for row in received[0])
            assert (received[0] is fetched[0]) is d.is_pg
            assert [dict(row) for row in fetched[0]]==[{'ref_type':'doc','url':'docs/scope.md','label':''}]
            refs_json='[{"label":"","ref_type":"doc","url":"docs/scope.md"}]'
            assert digest==expected_digest(item,revision,refs_json=refs_json)[0]
            assert_pending(d)


    @pytest.mark.parametrize('target_change', ('pristine','own-edit','own-revise'))
    def test_item_correlations_exclude_other_items(self, release_driver, target_change, record_property):
        d=release_driver
        iid,other=d.new_item('active'),d.new_item('active')
        expected=d.backend.reserve(d.store,iid,actor='target',session_id='target Release')['release_digest']
        edit(d,other,'other edit one');edit(d,other,'other edit two')
        d.backend.record_decision(d.store,other,'revise',actor='other reviewer')
        d.backend.record_decision(d.store,other,'revise',actor='other reviewer')
        edits=revisions=0
        if target_change=='own-edit':edit(d,iid,'target edit');edits=1
        elif target_change=='own-revise':
            d.backend.record_decision(d.store,iid,'revise',actor='target reviewer');revisions=1
            expected=d.backend.reserve(d.store,iid,actor='target',session_id='new target Release')['release_digest']
        before=d.snapshot(iid)
        with owning(d,iid) as p:
            item,revision,count=p.basis(iid)
            assert revision==expected_revision(item,edits,revisions) and count==revisions
            current=p.current(iid)
            assert current is not None
            assert current['release_digest']==expected
            assert d.snapshot(iid)==before
            assert_pending(d)
        record_property('state_history',json.dumps({'history':'two-item-counts-'+target_change,
            'target':iid,'other':other,'other_edits':2,'other_revise':2,
            'expected_target_edit_count':edits,'expected_target_revise_count':revisions,
            'revision':revision,'current_digest':expected},sort_keys=True))

    @pytest.mark.parametrize('commit', (False,True))
    def test_caller_savepoint_survives_private_release_helpers(self, release_driver, commit, record_property):
        d=release_driver
        iid=d.new_item()
        before=d.snapshot(iid)
        original_goal=d.backend.get_sprint(d.store,d.sprint)['goal']
        with owning(d,iid) as p:
            conn=connection(d)
            def goal(value):
                if d.is_pg:conn.execute('UPDATE sprint SET goal=%s WHERE repo_id=%s AND id=%s',(value,d.store.repo_id,d.sprint))
                else:conn.execute('UPDATE sprint SET goal=? WHERE id=?',(value,d.sprint))
            goal('outer pending Release owner')
            conn.execute('SAVEPOINT caller_release_scope')
            item,revision,count=p.basis(iid)
            digest=p.freeze(item,revision,count)
            goal('inside caller savepoint')
            assert_pending(d)
            assert p.current(iid)['release_digest']==digest
            with d.independent() as observer:
                assert d.snapshot(iid,observer)==before
                assert d.backend.get_sprint(observer,d.sprint)['goal']==original_goal
            conn.execute('ROLLBACK TO SAVEPOINT caller_release_scope')
            conn.execute('RELEASE SAVEPOINT caller_release_scope')
            assert_pending(d)
            assert d.snapshot(iid)==before
            assert d.backend.get_sprint(d.store,d.sprint)['goal']=='outer pending Release owner'
            (conn.commit if commit else conn.rollback)()
        with d.independent() as observer:
            assert d.snapshot(iid,observer)==before
            assert d.backend.get_sprint(observer,d.sprint)['goal']==('outer pending Release owner' if commit else original_goal)
        record_property('state_history',json.dumps({'history':'caller-savepoint-'+str(commit),
            'item_id':iid,'rolled_back_private_digest':digest,'outer_committed':commit},sort_keys=True))

    def test_test_owned_falsey_current_row_avoids_decoder(self, release_driver, monkeypatch):
        d=release_driver
        iid=d.new_item()
        with owning(d,iid) as p:
            item,revision,count=p.basis(iid)
            digest=p.freeze(item,revision,count)
            raw=p.handle
            fetched=[]
            class Proxy:
                result=None
                def __getattr__(self,name):return getattr(raw,name)
                def execute(self,sql,params=()):
                    self.result=raw.execute(sql,params)
                    return self.result if d.is_pg else self
                def fetchone(self):
                    row=raw.fetchone() if d.is_pg else self.result.fetchone()
                    assert row is not None
                    fetched.append(dict(row))
                    return {}  # Deliberate empty mapping, not legal persisted row.
            target=d.backend if d.is_pg else releases
            name='_release_row' if d.is_pg else 'release_row'
            def unexpected(*args):raise AssertionError('falsey current row reached decoder')
            with monkeypatch.context() as patch:
                patch.setattr(target,name,unexpected)
                assert Primitive(d,Proxy()).current(iid) is None
            assert len(fetched)==1 and fetched[0]['release_digest']==digest
            assert p.current(iid)['release_digest']==digest
            assert_pending(d)


    def test_ref_canonicalizer_binding_keeps_backend_fetch_timing(self, release_driver, monkeypatch, record_property):
        # Transparent test-owned fetch rebinding distinguishes the original
        # PG nested argument expression from SQLite's already-materialized list.
        d=release_driver
        iid=d.new_item()
        trace=[]
        original=releases.canonical_context_refs
        with owning(d,iid) as p:
            item,revision,count=p.basis(iid)
            raw=p.handle
            with monkeypatch.context() as patch:
                def initial(rows):trace.append('initial-canonicalizer');return original(rows)
                def selected(rows):trace.append('select-rebound-canonicalizer');return original(rows)
                def rebound(rows):trace.append('fetch-rebound-canonicalizer');return original(rows)
                class Proxy:
                    result=None
                    def __getattr__(self,name):return getattr(raw,name)
                    def execute(self,sql,params=()):
                        self.result=raw.execute(sql,params)
                        if 'FROM ref' in sql:
                            trace.append('actual-ref-select')
                            patch.setattr(releases,'canonical_context_refs',selected)
                        return self.result if d.is_pg else self
                    def fetchall(self):
                        result=raw.fetchall() if d.is_pg else self.result.fetchall()
                        trace.append('actual-fetch')
                        patch.setattr(releases,'canonical_context_refs',rebound)
                        return result
                patch.setattr(releases,'canonical_context_refs',initial)
                digest=Primitive(d,Proxy()).freeze(item,revision,count)
            assert trace==['actual-ref-select','actual-fetch','select-rebound-canonicalizer' if d.is_pg else 'fetch-rebound-canonicalizer']
            assert digest==expected_digest(item,revision)[0]
            assert_pending(d)
        record_property('state_history',json.dumps({'history':'ref-canonicalizer-binding',
            'backend':'postgresql' if d.is_pg else 'sqlite','trace':trace,'digest':digest},sort_keys=True))


def capture_query(d,route,record_property):
    iid=d.new_item()
    before=d.snapshot(iid)
    seen=[]
    with owning(d,iid) as p:
        raw=p.handle
        class Proxy:
            def __getattr__(self,name):return getattr(raw,name)
            def execute(self,sql,params=()):
                seen.append((sql,params))
                return raw.execute(sql,params)
        subject=Primitive(d,Proxy())
        if route=='current':assert subject.current(iid) is None
        else:
            item,revision,count=subject.basis(iid,**({'lock':False} if route=='basis-readonly' else {}))
            assert revision==expected_revision(item) and count==0
        assert len(seen)==1
        sql,params=seen[0]
        assert tuple(params)==((('item-edited',) if route!='current' else ())+
            ((d.store.repo_id,) if d.is_pg else ())+(iid,))
        assert_pending(d)
        assert d.snapshot(iid)==before
    # Captured SQL tokens supply before/after structural qualification where
    # an inner tenant predicate is redundant for valid native relationships.
    # No expected SQL is generated from the future builder under test.
    record_property('state_history',json.dumps({'history':'query-boundary-'+route,
        'backend':'postgresql' if d.is_pg else 'sqlite','sql_tokens':' '.join(sql.split()),
        'parameters':list(params),'parameter_types':[type(v).__name__ for v in params],
        'bound_item_id':iid,'bound_repo_id':d.store.repo_id if d.is_pg else None},sort_keys=True))
