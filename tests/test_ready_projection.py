from contextlib import contextmanager

import pytest
from sprintctl import db
from tests._ready_projection_contract import ReadyDriver, ReadyProjectionCases


@pytest.fixture
def ready_driver(conn, db_path, active_sprint):
    @contextmanager
    def independent():
        other = db.get_connection(db_path)
        try:
            yield other
        finally:
            other.close()
    def stamp(ident, value):
        conn.execute('UPDATE work_item SET created_at=? WHERE id=?', (value, ident))
        conn.commit()
    def write_description(ident, value):
        conn.execute('UPDATE work_item SET description=? WHERE id=?', (value, ident))
    return ReadyDriver(db, conn, active_sprint['id'], independent, stamp,
                       write_description, lambda: conn.in_transaction)


class TestSqliteReadyProjection(ReadyProjectionCases):
    pass
