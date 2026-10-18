"""SQLite public kind/status compatibility oracle; accepted guards stay separate."""
from contextlib import closing, contextmanager
import threading

import pytest

from sprintctl import db
from tests._sprint_kind_status_contract import SprintMutationDriver, SprintKindStatusCases


@pytest.fixture
def mutation_driver(conn, db_path):
    sprint = db.create_sprint(conn, "kind/status baseline")

    def open_store():
        return db.get_connection(db_path)

    @contextmanager
    def independent():
        with closing(open_store()) as other:
            yield other

    @contextmanager
    def abort_update(operation, timing):
        assert operation in {"kind", "status"} and timing in {"BEFORE", "AFTER"}
        conn.execute(f"CREATE TEMP TRIGGER sprint_mutation_fault {timing} UPDATE OF {operation} ON sprint "
                     f"WHEN NEW.id={sprint} BEGIN SELECT RAISE(ABORT,'injected sprint update fault'); END")
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute("DROP TRIGGER sprint_mutation_fault")
            conn.commit()

    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda statement: attempted.set() if statement == "BEGIN IMMEDIATE" else None)
        return attempted

    return SprintMutationDriver(db, conn, sprint, independent, open_store, lambda store: store.close(),
                                abort_update, arm_wait, lambda owner, wait, first: wait.wait(timeout=5),
                                "SQLite BEGIN IMMEDIATE writer serialization")


class TestSQLiteSprintKindStatus(SprintKindStatusCases):
    pass
