"""SQLite real-storage candidate seeding fault oracle."""
from contextlib import closing, contextmanager

import pytest
import threading

from sprintctl import db
from tests._backlog_seed_atomicity_contract import BacklogSeedAtomicityCases, BacklogSeedDriver


@pytest.fixture
def seed_driver(conn, db_path):
    source = db.create_sprint(conn, "seed source", status="active")
    target = db.create_sprint(conn, "seed target")
    track = db.get_or_create_track(conn, source, "source")
    item = db.create_work_item(conn, source, track, "source item")

    @contextmanager
    def independent():
        with closing(db.get_connection(db_path)) as other:
            yield other

    @contextmanager
    def abort_link():
        conn.execute("CREATE TEMP TRIGGER seed_link_fault BEFORE INSERT ON event "
                     "WHEN NEW.event_type='backlog-seeded' "
                     "BEGIN SELECT RAISE(ABORT,'injected backlog linkage fault'); END")
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute("DROP TRIGGER seed_link_fault")
            conn.commit()

    def arm_wait(other):
        attempted = threading.Event()
        other.set_trace_callback(lambda statement: attempted.set() if statement == "BEGIN IMMEDIATE" else None)
        return attempted

    @contextmanager
    def hold_sprints():
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def mutate_event(event, change):
        if change in {"source-sprint-deleted", "target-sprint-deleted"}:
            conn.execute("DELETE FROM sprint WHERE id=?", (source if change.startswith("source") else target,))
        elif change == "deleted":
            conn.execute("DELETE FROM event WHERE id=?", (event,))
        elif change == "moved":
            conn.execute("UPDATE event SET sprint_id=? WHERE id=?", (target, event))
        else:
            conn.execute("UPDATE event SET event_type='unrelated-note' WHERE id=?", (event,))

    return BacklogSeedDriver(db, conn, source, target, item, independent, abort_link,
                             arm_wait, lambda owner, wait, winner: wait.wait(timeout=5),
                             hold_sprints, mutate_event, "SQLite BEGIN IMMEDIATE writer serialization")


class TestSQLiteBacklogSeedAtomicity(BacklogSeedAtomicityCases):
    pass


@pytest.mark.parametrize("population", ["empty", "all-linked", "fresh"])
def test_caller_owned_write_policy(seed_driver, population):
    d = seed_driver
    if population != "empty":
        event = d.candidate()
        if population == "all-linked":
            d.backend.create_event(d.store, d.target, "historical", event_type="backlog-seeded", payload={"source_event_id": event})
    d.store.execute("UPDATE work_item SET description='caller pending' WHERE id=?", (d.item,))
    if population == "fresh":
        with pytest.raises(ValueError, match="idle SQLite"):
            d.seed()
    else:
        assert d.seed() == []
    assert d.store.in_transaction
    assert db.get_work_item(d.store, d.item)["description"] == "caller pending"
    with d.independent() as other:
        assert db.get_work_item(other, d.item)["description"] == ""
        assert db.list_tracks(other, d.target) == []
    d.store.rollback()
