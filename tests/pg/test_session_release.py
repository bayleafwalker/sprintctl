"""Actual disposable PG owner/cursor half of the caller-session release oracle."""
from contextlib import contextmanager
from dataclasses import replace
import time
from uuid import uuid4

import pytest

from sprintctl import reservation
from tests._session_release_contract import ReleaseDriver, SessionReleaseCases, ConnectionProbe, SESSION, NOW
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg

pytestmark = PG_MARKS


@pytest.fixture
def release_driver(store, sprint_id, track_id):
    iid = pg.create_work_item(store, sprint_id, track_id, 'session release', assignee='original')
    pg.set_work_item_status(store, iid, 'active')
    item = pg.get_work_item(store, iid)
    assert store.conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'
    store.conn.commit()  # Fixture preflight before any caller-owned writes.

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
            assert_disposable_connection(conn)
            assert conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'
            conn.commit()
            yield replace(store, conn=conn)

    def raw_insert():
        return store.conn.execute("INSERT INTO reservation(repo_id,work_item_id,session_id,actor,role,state,created_at,last_activity_at,release_digest) "
            "VALUES (%s,%s,%s,%s,'observation','active',%s,%s,NULL) RETURNING id",
            (store.repo_id, iid, SESSION, 'late raw actor', NOW, NOW)).fetchone()['id']

    @contextmanager
    def fault():
        from psycopg import sql
        name = 'session_release_fault_' + uuid4().hex
        store.conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected session-release SQL fault'; END $$").format(sql.Identifier(name)))
        store.conn.execute(sql.SQL("CREATE TRIGGER {} AFTER UPDATE ON reservation FOR EACH ROW "
            "WHEN (NEW.repo_id={} AND NEW.work_item_id={} AND NEW.state='released') EXECUTE FUNCTION {}()").format(
                sql.Identifier(name), sql.Literal(store.repo_id), sql.Literal(iid), sql.Identifier(name)))
        store.conn.commit()
        try:
            yield
        finally:
            store.conn.rollback()
            store.conn.execute(sql.SQL('DROP TRIGGER {} ON reservation').format(sql.Identifier(name)))
            store.conn.execute(sql.SQL('DROP FUNCTION {}()').format(sql.Identifier(name)))
            store.conn.commit()

    def lock_item():
        store.conn.execute('SELECT id FROM work_item WHERE repo_id=%s AND id=%s FOR UPDATE', (store.repo_id, iid)).fetchone()

    def await_wait(pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pids = store.conn.execute('SELECT pg_blocking_pids(%s) AS pids', (pid,)).fetchone()['pids']
            if store.conn.info.backend_pid in pids:
                return True
            time.sleep(.005)
        return False

    return ReleaseDriver(pg, store, item, independent,
        lambda: store.conn.execute('BEGIN'),
        lambda: store.conn.execute("UPDATE work_item SET assignee='pending caller' WHERE repo_id=%s AND id=%s", (store.repo_id, iid)),
        raw_insert, fault, lock_item, lambda other: other.conn.info.backend_pid,
        await_wait, lambda proxy: replace(store, conn=proxy), True)


class TestPostgresSessionRelease(SessionReleaseCases):
    pass


def test_select_clock_update_and_event_cursor_boundaries(release_driver, monkeypatch):
    d = release_driver
    d.reserve(role='observation')
    log = []
    proxy = d.proxy_handle(ConnectionProbe(d.conn, log=log))
    original = pg._insert_event
    def event(*args, **kwargs):
        log.append(('event', None))
        return original(*args, **kwargs)
    monkeypatch.setattr(pg, '_insert_event', event)
    monkeypatch.setattr(reservation, 'now_text', lambda: log.append(('clock', None)) or NOW)
    d.begin()
    try:
        d.release(handle=proxy)
        assert log.index(('cursor-exit', 'select')) < log.index(('clock', None))
        assert log.index(('clock', None)) < log.index(('cursor-exit', 'update')) < log.index(('event', None))
        assert d.tx() == 'INTRANS'
    finally:
        d.conn.rollback()


def test_foreign_repository_item_is_not_released(release_driver, pg_test_scope, monkeypatch):
    d = release_driver
    own = d.reserve(role='observation')
    foreign = replace(d.handle, repo_id=pg_test_scope('session-release-foreign'))
    sid = pg.create_sprint(foreign, 'foreign session release', status='active')
    tid = pg.get_or_create_track(foreign, sid, 'foreign')
    iid = pg.create_work_item(foreign, sid, tid, 'foreign item')
    row = pg.reserve(foreign, iid, actor='foreign', session_id=SESSION, role='observation')
    foreign_item = pg.get_work_item(foreign, iid)
    d.conn.commit()  # Fixture-only read, before caller transaction.
    monkeypatch.setattr(reservation, 'now_text', lambda: pytest.fail('foreign empty selection reached clock'))
    d.begin()
    try:
        assert d.release(item=foreign_item) == []
        assert d.tx() == 'INTRANS'
        with d.independent() as other:
            records = other.conn.execute('SELECT id,state FROM reservation WHERE (repo_id=%s AND id=%s) OR (repo_id=%s AND id=%s)',
                (d.handle.repo_id, own['id'], foreign.repo_id, row['id'])).fetchall()
            assert {r['id']: r['state'] for r in records} == {own['id']: 'active', row['id']: 'active'}
    finally:
        d.conn.rollback()
