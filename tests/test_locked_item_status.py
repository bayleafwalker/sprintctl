from contextlib import contextmanager

import pytest
from sprintctl import db
from tests._locked_item_status_contract import LockedStatusCases, StatusDriver


@pytest.fixture
def status_driver(conn, db_path):
    sid = db.create_sprint(conn, 'locked status SQLite oracle', status='active')
    tid = db.get_or_create_track(conn, sid, 'status')

    @contextmanager
    def independent():
        other = db.get_connection(db_path)
        try:
            yield other
        finally:
            other.close()

    return StatusDriver(db, conn, sid, tid, independent, db._WorkItemSqlite, False)


class TestSqliteLockedStatus(LockedStatusCases):
    pass
