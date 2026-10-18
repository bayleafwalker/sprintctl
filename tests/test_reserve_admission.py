"""SQLite admission oracle over a private temporary database."""
from contextlib import closing, contextmanager
import threading

import pytest

from sprintctl import db
from tests._reserve_admission_contract import AdmissionCases, AdmissionDriver


@pytest.fixture
def admission_driver(conn, db_path, active_sprint):
    sprint = active_sprint['id']
    track = db.get_or_create_track(conn, sprint, 'admission oracle')
    item = db.create_work_item(conn, sprint, track, 'admission oracle')

    @contextmanager
    def independent():
        with closing(db.get_connection(db_path)) as other:
            yield other

    @contextmanager
    def sql_fault(operation, when):
        conn.execute(f"CREATE TEMP TRIGGER admission_fault {when} {operation} ON reservation "
            f"WHEN NEW.work_item_id={item} BEGIN SELECT RAISE(ABORT,'injected admission SQL fault'); END")
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute('DROP TRIGGER admission_fault')
            conn.commit()

    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
        return attempted

    return AdmissionDriver(db, conn, item, sprint, independent, sql_fault,
        lambda other: other.execute('BEGIN IMMEDIATE'), arm_wait,
        lambda blocker, attempted: attempted.wait(5), 'sqlite')


class TestSQLiteReserveAdmission(AdmissionCases):
    pass
