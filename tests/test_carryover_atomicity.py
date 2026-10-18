from contextlib import closing, contextmanager
import threading

import pytest

from sprintctl import db, maintain
from tests._carryover_atomicity_contract import CarryoverAtomicityCases, CarryoverDriver


@pytest.fixture
def carryover_driver(conn, db_path):
    source = db.create_sprint(conn, "carry source", status="active")
    target = db.create_sprint(conn, "carry target")
    track = db.get_or_create_track(conn, source, "carry track")
    item = db.create_work_item(conn, source, track, "Carry once", description="Keep detail", assignee="owner")

    @contextmanager
    def independent():
        with closing(db.get_connection(db_path)) as other:
            yield other

    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None)
        return attempted

    return CarryoverDriver(db, conn, source, target, item, independent,
                          arm_wait, lambda store, attempted: attempted.wait(timeout=5))


class TestSQLiteCarryoverAtomicity(CarryoverAtomicityCases):
    pass


def test_existing_caller_transaction_is_refused_without_commit_or_rollback(carryover_driver):
    d = carryover_driver
    d.store.execute("UPDATE work_item SET description='caller-owned' WHERE id=?", (d.item,))
    with pytest.raises(ValueError, match="idle SQLite connection"):
        maintain.carryover(d.store, d.source, d.target)
    assert d.store.in_transaction
    assert db.get_work_item(d.store, d.item)["description"] == "caller-owned"
    with d.independent() as other:
        assert db.get_work_item(other, d.item)["description"] == "Keep detail"
        assert db.list_work_items(other, sprint_id=d.target) == []
    d.store.rollback()
