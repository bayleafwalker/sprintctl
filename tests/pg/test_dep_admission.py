"""Registered disposable PostgreSQL admission oracle and tenant boundaries."""
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from tests._dep_admission_contract import DepDriver, DepAdmissionCases
from tests.pg._shared import PG_MARKS,_PG_URL,pg,psycopg,dict_row,assert_disposable_connection

pytestmark=PG_MARKS


@pytest.fixture
def dep_driver(store,sprint_id,track_id):
    a=pg.create_work_item(store,sprint_id,track_id,'first')
    b=pg.create_work_item(store,sprint_id,track_id,'second')
    pg.set_work_item_status(store,a,'active')
    other=pg.create_sprint(store,'dep-other-'+uuid4().hex,'goal','2026-10-19','2026-10-20','planned')
    track=pg.get_or_create_track(store,other,'dep oracle')
    c=pg.create_work_item(store,other,track,'unrelated caller item',assignee='original')
    assert store.conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']=='read committed'
    store.conn.commit()
    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL,row_factory=dict_row,connect_timeout=5,options='-c statement_timeout=5000 -c lock_timeout=3000') as conn:
            assert_disposable_connection(conn)
            assert conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']=='read committed'
            conn.commit();yield replace(store,conn=conn)
    @contextmanager
    def fault():
        from psycopg import sql
        name='dep_admission_fault_'+uuid4().hex
        store.conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected dependency insert'; END $$").format(sql.Identifier(name)))
        store.conn.execute(sql.SQL('CREATE TRIGGER {} BEFORE INSERT ON dep FOR EACH ROW WHEN (NEW.repo_id={} AND NEW.item_id={}) EXECUTE FUNCTION {}()').format(sql.Identifier(name),sql.Literal(store.repo_id),sql.Literal(a),sql.Identifier(name)))
        store.conn.commit()
        try:yield
        finally:
            store.conn.rollback();store.conn.execute(sql.SQL('DROP TRIGGER {} ON dep').format(sql.Identifier(name)));store.conn.execute(sql.SQL('DROP FUNCTION {}()').format(sql.Identifier(name)));store.conn.commit()
    return DepDriver(pg,store,(a,b,c),independent,fault,lambda proxy:replace(store,conn=proxy),True)


class TestPostgresDepAdmission(DepAdmissionCases):
    pass


@pytest.mark.parametrize('position',['first','second'])
def test_foreign_repository_endpoint_is_missing(dep_driver,pg_test_scope,monkeypatch,position):
    d=dep_driver;foreign=replace(d.handle,repo_id=pg_test_scope('foreign-dep-endpoint'))
    sid=pg.create_sprint(foreign,'foreign-'+uuid4().hex,'goal','2026-10-19','2026-10-20','planned')
    track=pg.get_or_create_track(foreign,sid,'foreign');iid=pg.create_work_item(foreign,sid,track,'foreign')
    d.conn.commit();before=d.snapshot();calls=[];original=pg.get_work_item
    def lookup(owner,ident):calls.append(ident);return original(owner,ident)
    monkeypatch.setattr(pg,'get_work_item',lookup)
    args=(iid,d.items[1]) if position=='first' else (d.items[0],iid)
    try:
        with pytest.raises(ValueError) as caught:d.add(*args)
        assert caught.value.args==(f'Work item #{iid} not found',)
        assert calls==([iid] if position=='first' else [d.items[0],iid])
        assert d.snapshot()==before
    finally:d.conn.rollback()
