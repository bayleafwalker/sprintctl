"""Disposable PostgreSQL real-storage candidate seeding fault oracle."""
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
import time

from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from tests._backlog_seed_atomicity_contract import BacklogSeedAtomicityCases, BacklogSeedDriver

pytestmark = PG_MARKS


@pytest.fixture
def seed_driver(store):
    source = pg.create_sprint(store, "seed source " + uuid4().hex, status="active")
    target = pg.create_sprint(store, "seed target " + uuid4().hex)
    track = pg.get_or_create_track(store, source, "source")
    item = pg.create_work_item(store, source, track, "source item")

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as connection:
            assert_disposable_connection(connection)
            isolation = connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
            assert isolation == "read committed"
            # Finish only this freshly opened connection's safety/isolation
            # preflight; no caller work exists before yielding it.
            connection.commit()
            yield replace(store, conn=connection)

    @contextmanager
    def abort_link():
        from psycopg import sql
        name = "seed_link_fault_" + uuid4().hex
        conn = store.conn
        conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN RAISE EXCEPTION 'injected backlog linkage fault'; END $$").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE TRIGGER {} BEFORE INSERT ON event FOR EACH ROW "
                             "WHEN (NEW.repo_id={} AND NEW.sprint_id={} AND NEW.event_type='backlog-seeded') "
                             "EXECUTE FUNCTION {}()").format(sql.Identifier(name), sql.Literal(store.repo_id),
                                                               sql.Literal(target), sql.Identifier(name)))
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute(sql.SQL("DROP TRIGGER {} ON event").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))
            conn.commit()

    def await_wait(owner, pid, winner):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blockers = owner.conn.execute("SELECT pg_blocking_pids(%s) AS blockers", (pid,)).fetchone()["blockers"]
            if winner.conn.info.backend_pid in blockers:
                return True
            time.sleep(0.005)
        return False

    @contextmanager
    def hold_sprints():
        store.conn.commit()
        with store.conn.transaction():
            store.conn.execute("SELECT id FROM sprint WHERE repo_id=%s AND id IN (%s,%s) ORDER BY id FOR UPDATE", (store.repo_id, source, target)).fetchall()
            yield

    def mutate_event(event, change):
        if change in {"source-sprint-deleted", "target-sprint-deleted"}:
            # PG deliberately RESTRICTs referenced sprint deletion; remove only
            # this fixture's selected source dependents in the same holder txn.
            if change.startswith("source"):
                store.conn.execute("DELETE FROM event WHERE repo_id=%s AND sprint_id=%s", (store.repo_id, source))
                store.conn.execute("DELETE FROM work_item WHERE repo_id=%s AND sprint_id=%s", (store.repo_id, source))
                store.conn.execute("DELETE FROM track WHERE repo_id=%s AND sprint_id=%s", (store.repo_id, source))
            store.conn.execute("DELETE FROM sprint WHERE repo_id=%s AND id=%s", (store.repo_id, source if change.startswith("source") else target))
        elif change == "deleted":
            store.conn.execute("DELETE FROM event WHERE repo_id=%s AND id=%s", (store.repo_id, event))
        elif change == "moved":
            store.conn.execute("UPDATE event SET sprint_id=%s WHERE repo_id=%s AND id=%s", (target, store.repo_id, event))
        else:
            store.conn.execute("UPDATE event SET event_type='unrelated-note' WHERE repo_id=%s AND id=%s", (store.repo_id, event))

    isolation = store.conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
    store.conn.commit()
    assert isolation == "read committed"

    return BacklogSeedDriver(pg, store, source, target, item, independent, abort_link,
                             lambda other: other.conn.info.backend_pid, await_wait,
                             hold_sprints, mutate_event, isolation)


class TestPostgresBacklogSeedAtomicity(BacklogSeedAtomicityCases):
    pass


@pytest.fixture(scope="module", autouse=True)
def _retain_requested_cleanup_report():
    """Keep disposable cleanup evidence even when an intentional RED exits early."""
    import os
    from pathlib import Path
    import shutil

    yield
    destination = os.environ.get("SPRINTCTL_TEST_PG_SEED_BASELINE_REPORT")
    report = os.environ.get("SPRINTCTL_TEST_PG_CLEANUP_REPORT")
    if destination and report:
        shutil.copyfile(Path(report), Path(destination))


@pytest.mark.parametrize("population", ["empty", "all-linked"])
def test_noop_does_not_commit_pending_caller_write(seed_driver, population):
    d = seed_driver
    if population == "all-linked":
        event = d.candidate()
        pg.create_event(d.store, d.target, "historical", event_type="backlog-seeded", payload={"source_event_id": event})
    d.store.conn.execute("UPDATE work_item SET description='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
    assert d.seed() == []
    assert pg.get_work_item(d.store, d.item)["description"] == "caller pending"
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)["description"] == ""
        assert pg.list_tracks(other, d.target) == []
    d.store.conn.rollback()


def test_fresh_path_precommits_caller_write_before_owned_failure(seed_driver, monkeypatch):
    d = seed_driver
    d.candidate()
    d.store.conn.execute("UPDATE work_item SET description='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
    original = pg._insert_event

    def fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == "backlog-seeded":
            raise RuntimeError("injected own transaction failure")
        return result

    with monkeypatch.context() as fault:
        fault.setattr(pg, "_insert_event", fail)
        with pytest.raises(RuntimeError, match="own transaction"):
            d.seed()
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)["description"] == "caller pending"
        assert d.snapshot(other) == {"tracks": [], "items": [], "events": []}


def test_managed_outer_transaction_refusal_preserves_caller_state(seed_driver):
    d = seed_driver
    d.candidate()
    d.store.conn.commit()
    with d.store.conn.transaction(force_rollback=True):
        d.store.conn.execute("UPDATE work_item SET description='managed caller' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match="(?i)transaction"):
            d.seed()
        assert pg.get_work_item(d.store, d.item)["description"] == "managed caller"
        with d.independent() as other:
            assert pg.get_work_item(other, d.item)["description"] == ""
            assert d.snapshot(other) == {"tracks": [], "items": [], "events": []}


def test_two_phase_refusal_keeps_caller_changes_pending(seed_driver):
    d = seed_driver
    d.candidate()
    conn = d.store.conn
    conn.commit()
    conn.tpc_begin(conn.xid(2778, "seed-" + str(d.item), "admission"))
    try:
        conn.execute("UPDATE work_item SET description='two-phase caller' WHERE repo_id=%s AND id=%s", (d.store.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match="two-phase"):
            d.seed()
        assert pg.get_work_item(d.store, d.item)["description"] == "two-phase caller"
        with d.independent() as other:
            assert pg.get_work_item(other, d.item)["description"] == ""
            assert d.snapshot(other) == {"tracks": [], "items": [], "events": []}
    finally:
        conn.tpc_rollback()


def test_foreign_repository_events_and_receipts_are_not_used(seed_driver, pg_test_scope):
    d = seed_driver
    event = d.candidate()
    foreign = replace(d.store, repo_id=pg_test_scope("seed-foreign"), authority_repo_uuid=str(uuid4()))
    pg.init_db(foreign)
    source = pg.create_sprint(foreign, "foreign source", status="active")
    pg.create_event(foreign, source, "foreign", event_type="backlog-seeded", payload={"source_event_id": event})
    assert len(d.seed()) == 1
    assert d.seed() == []
    with pytest.raises(ValueError, match="Source sprint"):
        pg.backlog_seed_from_candidates(d.store, source, d.target)
