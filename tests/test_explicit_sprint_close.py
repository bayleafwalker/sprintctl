"""SQLite half of the pre-extraction explicit-close oracle."""
from contextlib import closing, contextmanager
import threading

import pytest

from sprintctl import db
from tests._explicit_sprint_close_contract import ExplicitCloseDriver, ExplicitSprintCloseCases


@pytest.fixture
def close_driver(conn, db_path):
    sprint = db.create_sprint(conn, "explicit close oracle", status="active")

    @contextmanager
    def independent():
        with closing(db.get_connection(db_path)) as other:
            yield other

    @contextmanager
    def abort_boundary():
        conn.execute("CREATE TEMP TRIGGER explicit_close_fault BEFORE INSERT ON event "
                     f"WHEN NEW.sprint_id={sprint} AND NEW.event_type='sprint-close-boundary' "
                     "BEGIN SELECT RAISE(ABORT,'injected explicit boundary fault'); END")
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute("DROP TRIGGER explicit_close_fault")
            conn.commit()

    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda statement: attempted.set() if statement == "BEGIN IMMEDIATE" else None)
        return attempted

    return ExplicitCloseDriver(db, conn, sprint, independent, abort_boundary,
                               arm_wait, lambda owner, wait, winner: wait.wait(timeout=5),
                               "SQLite BEGIN IMMEDIATE writer serialization")


class TestSQLiteExplicitSprintClose(ExplicitSprintCloseCases):
    pass
