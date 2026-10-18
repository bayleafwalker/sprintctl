"""Registered disposable-PostgreSQL half of the explicit-close oracle."""
from contextlib import contextmanager
from dataclasses import replace
import time
from uuid import uuid4

import pytest

from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from tests._explicit_sprint_close_contract import ExplicitCloseDriver, ExplicitSprintCloseCases

pytestmark = PG_MARKS


@pytest.fixture
def close_driver(store):
    sprint = pg.create_sprint(store, "explicit close " + uuid4().hex, status="active")

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as connection:
            assert_disposable_connection(connection)
            assert connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "read committed"
            connection.commit()  # Only this fresh connection's safety preflight.
            yield replace(store, conn=connection)

    @contextmanager
    def abort_boundary():
        from psycopg import sql
        name = "explicit_close_fault_" + uuid4().hex
        conn = store.conn
        conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN RAISE EXCEPTION 'injected explicit boundary fault'; END $$").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE TRIGGER {} BEFORE INSERT ON event FOR EACH ROW "
                             "WHEN (NEW.repo_id={} AND NEW.sprint_id={} AND NEW.event_type='sprint-close-boundary') "
                             "EXECUTE FUNCTION {}()").format(sql.Identifier(name), sql.Literal(store.repo_id), sql.Literal(sprint), sql.Identifier(name)))
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

    isolation = store.conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
    assert isolation == "read committed"
    store.conn.commit()
    return ExplicitCloseDriver(pg, store, sprint, independent, abort_boundary,
                               lambda other: other.conn.info.backend_pid, await_wait, isolation)


class TestPostgresExplicitSprintClose(ExplicitSprintCloseCases):
    pass


def _pending_caller(d):
    protected = pg.create_sprint(d.store, "protected caller " + uuid4().hex, goal="durable original")
    d.store.conn.execute("UPDATE sprint SET goal='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, protected))
    return protected


def test_implicit_caller_write_commits_with_successful_close(close_driver):
    d = close_driver
    protected = _pending_caller(d)
    result = d.close()
    with d.independent() as other:
        assert pg.get_sprint(other, protected)["goal"] == "caller pending"
        assert d.snapshot(other)["sprint"]["status"] == "closed"
        assert [row["id"] for row in d.snapshot(other)["events"]] == [result]


@pytest.mark.parametrize("failure", ["stale-basis", "after-real-insert"])
def test_ordinary_owned_failure_rolls_back_implicit_caller_write(close_driver, monkeypatch, failure):
    d = close_driver
    basis = d.basis().replace("@status:active", "@status:planned")
    protected = _pending_caller(d)
    original = pg._insert_event

    def fail_after(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected caller boundary fault")

    with monkeypatch.context() as fault:
        if failure == "after-real-insert":
            fault.setattr(pg, "_insert_event", fail_after)
        expected = pg.StatusConflict if failure == "stale-basis" else RuntimeError
        with pytest.raises(expected):
            d.close(revision=basis if failure == "stale-basis" else None)
    assert pg.get_sprint(d.store, protected)["goal"] == "durable original"
    with d.independent() as other:
        assert pg.get_sprint(other, protected)["goal"] == "durable original"
        assert d.snapshot(other)["sprint"]["status"] == "active"
        assert d.snapshot(other)["events"] == []


@pytest.mark.parametrize("failure", ["actor", "basis"])
def test_prevalidation_keeps_implicit_caller_write_pending(close_driver, failure):
    d = close_driver
    protected = _pending_caller(d)
    try:
        with pytest.raises(ValueError, match=failure if failure == "actor" else "revision"):
            d.close(actor=" " if failure == "actor" else "operator", revision="bad")
        assert pg.get_sprint(d.store, protected)["goal"] == "caller pending"
        with d.independent() as other:
            assert pg.get_sprint(other, protected)["goal"] == "durable original"
            assert d.snapshot(other)["sprint"]["status"] == "active"
            assert d.snapshot(other)["events"] == []
    finally:
        d.store.conn.rollback()


def test_managed_transaction_error_leaves_close_pending_until_caller_rollback(close_driver, record_property, request):
    import json
    d = close_driver
    protected = pg.create_sprint(d.store, "managed caller", goal="durable original")
    observations = []
    with d.store.conn.transaction(force_rollback=True):
        d.store.conn.execute("UPDATE sprint SET goal='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, protected))
        with pytest.raises(psycopg.ProgrammingError, match="Explicit rollback") as caught:
            d.close()
        assert pg.get_sprint(d.store, protected)["goal"] == "caller pending"
        pending = d.snapshot(d.store)
        assert pending["sprint"]["status"] == "closed" and len(pending["events"]) == 1
        with d.independent() as other:
            durable = d.snapshot(other)
            assert durable["sprint"]["status"] == "active" and durable["events"] == []
            assert pg.get_sprint(other, protected)["goal"] == "durable original"
        observations.append({"step": "managed-refusal-before-caller-cleanup", "error": str(caught.value), "pending": pending, "independent": durable})
    assert d.snapshot(d.store)["sprint"]["status"] == "active"
    assert d.snapshot(d.store)["events"] == []
    assert pg.get_sprint(d.store, protected)["goal"] == "durable original"
    observations.append({"step": "caller-context-rollback-completed", "state": d.snapshot(d.store)})
    record_property("state_history", json.dumps({"history": "managed-caller-pending-close", "backend": "sprintctl.pg", "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))


def test_two_phase_error_leaves_close_pending_until_tpc_rollback(close_driver, record_property, request):
    import json
    d = close_driver
    protected = pg.create_sprint(d.store, "TPC caller", goal="durable original")
    conn, observations = d.store.conn, []
    conn.tpc_begin(conn.xid(2780, "close-" + str(d.sprint), "caller"))
    try:
        conn.execute("UPDATE sprint SET goal='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, protected))
        with pytest.raises(psycopg.ProgrammingError, match=r"rollback\(\).*two-phase") as caught:
            d.close()
        pending = d.snapshot(d.store)
        assert pending["sprint"]["status"] == "closed" and len(pending["events"]) == 1
        assert pg.get_sprint(d.store, protected)["goal"] == "caller pending"
        with d.independent() as other:
            durable = d.snapshot(other)
            assert durable["sprint"]["status"] == "active" and durable["events"] == []
            assert pg.get_sprint(other, protected)["goal"] == "durable original"
        observations.append({"step": "two-phase-refusal-before-caller-cleanup", "error": str(caught.value), "pending": pending, "independent": durable})
    finally:
        conn.tpc_rollback()
    assert d.snapshot(d.store)["sprint"]["status"] == "active"
    assert d.snapshot(d.store)["events"] == []
    assert pg.get_sprint(d.store, protected)["goal"] == "durable original"
    observations.append({"step": "caller-tpc-rollback-completed", "state": d.snapshot(d.store)})
    record_property("state_history", json.dumps({"history": "two-phase-caller-pending-close", "backend": "sprintctl.pg", "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))


def test_foreign_repository_sprint_is_missing(close_driver, pg_test_scope):
    d = close_driver
    foreign = replace(d.store, repo_id=pg_test_scope("close-foreign"), authority_repo_uuid=str(uuid4()))
    pg.init_db(foreign)
    sprint = pg.create_sprint(foreign, "foreign close", status="active")
    with pytest.raises(ValueError, match=f"Sprint #{sprint} not found"):
        d.close(sprint=sprint)
    assert pg.get_sprint(foreign, sprint)["status"] == "active"
    assert pg.list_events(foreign, sprint) == []
