from contextlib import contextmanager
import threading
import pytest
from sprintctl import db
from tests._reservation_lifecycle_contract import Driver, LifecycleCases, invoke

@pytest.fixture
def lifecycle_driver(conn, db_path, active_sprint):
    track = db.get_or_create_track(conn, active_sprint['id'], 'lifecycle')
    item = db.create_work_item(conn, active_sprint['id'], track, 'lifecycle')
    @contextmanager
    def independent():
        other = db.get_connection(db_path)
        try: yield other
        finally: other.close()
    def backdate(ids, stamp):
        for ident in ids:
            conn.execute('UPDATE reservation SET last_activity_at=? WHERE id=?', (stamp, ident))
        conn.commit()
    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
        return attempted
    return Driver(db, conn, item, active_sprint['id'], track, independent, backdate,
                  arm_wait, lambda handle, attempted: attempted.wait(5))

class TestSqliteLifecycleContract(LifecycleCases):
    pass

@pytest.mark.parametrize('operation', ['touch', 'reassign', 'release', 'sweep'])
def test_caller_transaction_refusal_preserves_pending_sqlite_work(lifecycle_driver, operation):
    d = lifecycle_driver
    row = d.reserve()
    d.handle.execute("UPDATE work_item SET description='caller-owned' WHERE id=?", (d.item,))
    with pytest.raises(ValueError, match='idle SQLite connection'):
        invoke(d, operation, row['id'])
    assert d.handle.in_transaction
    assert db.get_work_item(d.handle, d.item)['description'] == 'caller-owned'
    with d.independent() as other:
        assert db.get_work_item(other, d.item)['description'] == ''
        assert db.get_reservation(other, row['id'])['state'] == 'active'
    d.handle.rollback()

