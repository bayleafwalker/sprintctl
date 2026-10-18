from tests.pg.test_locked_item_status import status_driver as release_driver
from tests.pg._shared import PG_MARKS
from tests._release_policy_contract import ReleasePolicyCases

pytestmark = PG_MARKS


class TestPostgresReleasePolicy(ReleasePolicyCases):
    pass

from dataclasses import replace
import json
import queue
import threading
import time

import pytest
from tests._release_policy_contract import owning, Primitive, expected_revision
from tests._locked_decision_writing_contract import connection


def test_actual_foreign_repository_basis_current_and_membership(release_driver, pg_test_scope):
    d = release_driver
    iid = d.new_item()
    foreign = replace(d.store,repo_id=pg_test_scope('release-policy-foreign'))
    sid = d.backend.create_sprint(foreign,'foreign Release policy',status='active')
    tid = d.backend.get_or_create_track(foreign,sid,'foreign')
    fid = d.backend.create_work_item(foreign,sid,tid,'foreign item')
    digest = d.backend.reserve(foreign,fid,actor='foreign owner',session_id='foreign policy')['release_digest']
    before = d.snapshot(iid)
    foreign_before = d.backend.list_releases(foreign,fid)
    with owning(d,iid) as p:
        with pytest.raises(ValueError,match=f'Work item #{fid} not found'): p.basis(fid)
        assert p.current(fid) is None
        for argument in [iid,fid]:
            with pytest.raises(d.backend._releases.ReleaseMismatch,match='not a release of item'):
                p.resolve(argument,digest)
        assert d.snapshot(iid)==before
        assert d.backend.list_releases(foreign,fid)==foreign_before


@pytest.mark.parametrize('route', ('direct','public-revision','proposal','current'))
def test_actual_readonly_basis_succeeds_beside_held_item_lock(release_driver, route, record_property):
    d = release_driver
    iid = d.new_item()
    original = d.backend.get_work_item(d.store,iid)
    connection(d).rollback()  # Only preflight reads, before readonly owner begins.
    with d.independent() as holder:
        holder_pid = holder.conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        holder.conn.execute('SELECT id FROM work_item WHERE repo_id=%s AND id=%s FOR UPDATE',(d.store.repo_id,iid))
        conn = connection(d)
        try:
            conn.execute('BEGIN READ ONLY')
            conn.execute("SET LOCAL statement_timeout='500ms'")
            if route=='direct':
                with conn.cursor() as cur:
                    item, revision, count = Primitive(d,cur).basis(iid,lock=False)
                assert item['id']==iid and count==0
            elif route=='public-revision': revision=d.backend.item_release_revision(d.store,iid)
            elif route=='current':
                assert d.backend.current_release(d.store,iid) is None
                revision=None
            else:
                item, revision, release = d.backend.effect_proposal_release_basis(d.store,iid)
                assert item['id']==iid and release is None
            if route!='current': assert revision==expected_revision(original)
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
            assert conn.info.transaction_status.name=='INTRANS'
        finally:
            conn.rollback()
            holder.conn.rollback()
    record_property('state_history',json.dumps({'history':'readonly-basis-'+route,'held_owner_pid':holder_pid,
        'item_id':iid,'revision':revision,'transaction_read_only':'on'},sort_keys=True))


def test_actual_default_basis_waits_on_exact_existing_owner(release_driver, record_property):
    d = release_driver
    iid = d.new_item()
    connection(d).rollback()
    started = queue.Queue()
    finished = queue.Queue()
    holder_pid = None
    pending_pids = []
    with d.independent() as holder:
        holder_pid=holder.conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        holder.conn.execute('SELECT id FROM work_item WHERE repo_id=%s AND id=%s FOR UPDATE',(d.store.repo_id,iid))
        def run():
            try:
                with d.independent() as contender:
                    pid=contender.conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
                    contender.conn.rollback()
                    contender.conn.execute("SET LOCAL statement_timeout='8s'")
                    started.put((pid,contender.conn))
                    with contender.conn.cursor() as cur:
                        result=d.backend._release_basis_locked(cur,d.store.repo_id,iid)
                    state=contender.conn.info.transaction_status.name
                    contender.conn.rollback()
                    finished.put(('result',result,state))
            except BaseException as exc: finished.put(('error',type(exc).__name__,str(exc)))
        thread=threading.Thread(target=run,daemon=True)
        thread.start()
        contender_conn=None
        try:
            pid,contender_conn=started.get(timeout=4)
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                pending_pids=connection(d).execute('SELECT pg_blocking_pids(%s) AS pids',(pid,)).fetchone()['pids']
                if holder_pid in pending_pids: break
                if not finished.empty(): break
                time.sleep(.01)
            assert holder_pid in pending_pids,'default basis did not wait for exact existing item owner'
            assert finished.empty()
            holder.conn.commit()
            result=finished.get(timeout=4)
            assert result[0]=='result',result
            item,revision,count=result[1]
            assert revision==expected_revision(item) and count==0 and result[2]=='INTRANS'
        finally:
            holder.conn.rollback()
            connection(d).rollback()
            thread.join(timeout=2)
            if thread.is_alive() and contender_conn is not None:
                contender_conn.cancel()
                thread.join(timeout=2)
            assert not thread.is_alive(),'own contender did not terminate'
    record_property('state_history',json.dumps({'history':'default-basis-existing-lock-wait',
        'holder_pid':holder_pid,'contender_pid':pid,'observed_blockers':pending_pids,
        'revision':revision},sort_keys=True))


@pytest.mark.parametrize('argument,locks', [(0,False),('truthy existing argument',True)])
def test_existing_lock_argument_truthiness_adds_no_type_guard(release_driver, argument, locks):
    d = release_driver
    iid=d.new_item()
    conn=connection(d)
    conn.rollback()
    try:
        conn.execute('BEGIN READ ONLY')
        with conn.cursor() as cur:
            if locks:
                with pytest.raises(d.backend.psycopg.errors.ReadOnlySqlTransaction):
                    Primitive(d,cur).basis(iid,lock=argument)
                assert conn.info.transaction_status.name=='INERROR'
            else:
                item,revision,count=Primitive(d,cur).basis(iid,lock=argument)
                assert item['id']==iid and revision==expected_revision(item) and count==0
                assert conn.info.transaction_status.name=='INTRANS'
    finally: conn.rollback()


def test_actual_pg_readonly_query_boundary_capture(release_driver, record_property):
    from tests._release_policy_contract import capture_query
    capture_query(release_driver,'basis-readonly',record_property)
