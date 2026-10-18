from contextlib import contextmanager

import pytest
import threading

from sprintctl import db
from tests._stale_item_atomicity_contract import StaleItemAtomicityCases, StaleItemDriver


@pytest.fixture
def stale_item_driver(conn, db_path):
    source = db.create_sprint(conn, 'stale source', status='active')
    target = db.create_sprint(conn, 'other sprint')
    track = db.get_or_create_track(conn, source, 'work')
    item = db.create_work_item(conn, source, track, 'stale item', description='retain fields', assignee='owner', priority=3)
    db.set_work_item_status(conn, item, 'active')

    @contextmanager
    def independent():
        other = db.get_connection(db_path)
        try:
            yield other
        finally:
            other.close()

    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
        return attempted

    driver = StaleItemDriver(db, conn, source, target, item, independent,
                            arm_wait, lambda owner, attempted: attempted.wait(5))
    driver.make_stale()
    return driver


class TestSQLiteStaleItemAtomicity(StaleItemAtomicityCases):
    pass


def test_caller_transaction_is_refused_without_rollback(stale_item_driver):
    d = stale_item_driver
    d.store.execute("UPDATE work_item SET description='caller-owned' WHERE id=?", (d.item,))
    with pytest.raises(ValueError, match='idle SQLite connection'):
        d.sweep()
    assert d.store.in_transaction
    assert db.get_work_item(d.store, d.item)['description'] == 'caller-owned'
    with d.independent() as other:
        assert db.get_work_item(other, d.item)['description'] == 'retain fields'
        assert db.get_work_item(other, d.item)['status'] == 'active'
    d.store.rollback()
