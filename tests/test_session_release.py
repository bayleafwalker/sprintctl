"""Real SQLite half of the frozen caller-session release oracle."""
from contextlib import closing, contextmanager
import threading

import pytest

from sprintctl import db
from tests._session_release_contract import ReleaseDriver, SessionReleaseCases, SESSION, NOW


@pytest.fixture
def release_driver(conn, db_path, active_sprint):
    sid = active_sprint['id']
    tid = db.get_or_create_track(conn, sid, 'session release')
    iid = db.create_work_item(conn, sid, tid, 'session release', assignee='original')
    db.set_work_item_status(conn, iid, 'active')
    item = db.get_work_item(conn, iid)

    @contextmanager
    def independent():
        with closing(db.get_connection(db_path)) as other:
            yield other

    def raw_insert():
        return conn.execute("INSERT INTO reservation(work_item_id,session_id,actor,role,state,created_at,last_activity_at,release_digest) "
            "VALUES (?,?,?,'observation','active',?,?,NULL)", (iid, SESSION, 'late raw actor', NOW, NOW)).lastrowid

    @contextmanager
    def fault():
        conn.execute(f"CREATE TEMP TRIGGER session_release_fault AFTER UPDATE ON reservation "
            f"WHEN NEW.work_item_id={iid} AND NEW.state='released' "
            "BEGIN SELECT RAISE(ABORT,'injected session-release SQL fault'); END")
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute('DROP TRIGGER session_release_fault')
            conn.commit()

    def arm_wait(other):
        signal = threading.Event()
        other.set_trace_callback(lambda sql: signal.set() if sql == 'BEGIN IMMEDIATE' else None)
        return signal

    return ReleaseDriver(db, conn, item, independent,
        lambda: conn.execute('BEGIN IMMEDIATE'),
        lambda: conn.execute("UPDATE work_item SET assignee='pending caller' WHERE id=?", (iid,)),
        raw_insert, fault, lambda: None, arm_wait, lambda signal: signal.wait(5), lambda proxy: proxy, False)


class TestSQLiteSessionRelease(SessionReleaseCases):
    pass
