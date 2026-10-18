from contextlib import contextmanager
from uuid import uuid4
from dataclasses import replace

import pytest
import time

from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from tests._stale_item_atomicity_contract import StaleItemAtomicityCases, StaleItemDriver

pytestmark = PG_MARKS


@pytest.fixture
def stale_item_driver(store):
    source = pg.create_sprint(store, 'stale source '+uuid4().hex, status='active')
    target = pg.create_sprint(store, 'other sprint '+uuid4().hex)
    track = pg.get_or_create_track(store, source, 'work')
    item = pg.create_work_item(store, source, track, 'stale item', description='retain fields', assignee='owner', priority=3)
    pg.set_work_item_status(store, item, 'active')

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as connection:
            assert_disposable_connection(connection)
            yield replace(store, conn=connection)

    def await_wait(owner, other_pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blockers = owner.conn.execute('SELECT pg_blocking_pids(%s) AS blockers', (other_pid,)).fetchone()['blockers']
            if owner.conn.info.backend_pid in blockers:
                return True
            time.sleep(0.005)
        return False

    driver = StaleItemDriver(pg, store, source, target, item, independent,
                            lambda other: other.conn.info.backend_pid, await_wait)
    driver.make_stale()
    return driver


class TestPostgresStaleItemAtomicity(StaleItemAtomicityCases):
    pass


def test_pending_writes_follow_committing_mutation_boundary(stale_item_driver, monkeypatch):
    from tests._stale_item_atomicity_contract import SweepFault
    d = stale_item_driver
    d.store.conn.execute("UPDATE work_item SET description='caller-pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
    original = pg._insert_event

    def fail(*args, **kwargs):
        if args[3] == 'auto-blocked-stale':
            raise SweepFault('owned item')
        return original(*args, **kwargs)

    monkeypatch.setattr(pg, '_insert_event', fail)
    with pytest.raises(SweepFault):
        d.sweep()
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == 'caller-pending'
        assert pg.get_work_item(other, d.item)['status'] == 'active'
        assert not pg.list_events(other, d.source)


def test_initially_fresh_noop_does_not_commit_pending_writes(stale_item_driver):
    from tests._stale_item_atomicity_contract import NOW
    d = stale_item_driver
    d.update(d.store, 'updated_at=%s', (NOW.strftime('%Y-%m-%dT%H:%M:%SZ'),))
    d.store.conn.execute("UPDATE work_item SET description='caller-pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
    assert d.sweep() == []
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == 'retain fields'
    assert pg.get_work_item(d.store, d.item)['description'] == 'caller-pending'
    d.store.conn.rollback()


def test_managed_outer_transaction_is_refused_before_block(stale_item_driver):
    d = stale_item_driver
    with d.store.conn.transaction(force_rollback=True):
        d.store.conn.execute("UPDATE work_item SET description='outer-pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match='Explicit commit'):
            d.sweep()
        assert pg.get_work_item(d.store, d.item)['description'] == 'outer-pending'
        assert pg.get_work_item(d.store, d.item)['status'] == 'active'
        with d.independent() as other:
            assert pg.get_work_item(other, d.item)['description'] == 'retain fields'
            assert not pg.list_events(other, d.source)
    assert pg.get_work_item(d.store, d.item)['description'] == 'retain fields'


def test_two_phase_transaction_is_refused_before_block(stale_item_driver):
    d = stale_item_driver
    conn = d.store.conn
    conn.tpc_begin(conn.xid(2771, 'stale-'+str(d.item), 'admission'))
    try:
        conn.execute("UPDATE work_item SET description='two-phase' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match='two-phase'):
            d.sweep()
        assert pg.get_work_item(d.store, d.item)['description'] == 'two-phase'
        assert pg.get_work_item(d.store, d.item)['status'] == 'active'
    finally:
        conn.tpc_rollback()
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == 'retain fields'


def test_existing_hosted_lease_is_preserved_on_failure_and_block(stale_item_driver, monkeypatch):
    from tests.pg.test_work_lease import A, _claim, _run
    from tests._stale_item_atomicity_contract import SweepFault
    d = stale_item_driver
    run = _run(d.store, A)
    lease = _claim(d.store, A, d.item, run, 'stale-lease-'+str(d.item))['lease']
    d.make_stale()

    def read(store):
        return store.conn.execute('SELECT * FROM work_lease WHERE repo_id=%s AND lease_id=%s', (store.repo_id, lease['lease_id'])).fetchone()

    before = read(d.store)
    original = pg._insert_event

    def fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == 'auto-blocked-stale':
            raise SweepFault('after event')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(pg, '_insert_event', fail)
        with pytest.raises(SweepFault):
            d.sweep()
    with d.independent() as other:
        assert read(other) == before
        assert pg.get_work_item(other, d.item)['status'] == 'active'
    assert len(d.sweep()) == 1
    with d.independent() as other:
        assert read(other) == before
        assert read(other)['state'] == 'active'
        assert pg.get_work_item(other, d.item)['status'] == 'blocked'
