"""Disposable PostgreSQL public kind/status oracle, retaining caller differences."""
from contextlib import contextmanager
from dataclasses import replace
import json
import time
from uuid import uuid4

import pytest

from sprintctl import db, sprintcore
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from tests._sprint_kind_status_contract import SprintMutationDriver, SprintKindStatusCases

pytestmark = PG_MARKS


@pytest.fixture
def mutation_driver(store):
    sprint = pg.create_sprint(store, "kind/status baseline " + uuid4().hex)

    def open_store():
        connection = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(connection)
            assert connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "read committed"
            connection.commit()  # Only fresh connection safety/isolation preflight.
            return replace(store, conn=connection)
        except Exception:
            connection.close()
            raise

    @contextmanager
    def independent():
        other = open_store()
        try:
            yield other
        finally:
            other.conn.close()

    @contextmanager
    def abort_update(operation, timing):
        from psycopg import sql
        assert operation in {"kind", "status"} and timing in {"BEFORE", "AFTER"}
        name = "sprint_mutation_fault_" + uuid4().hex
        conn = store.conn
        conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN RAISE EXCEPTION 'injected sprint update fault'; END $$").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE TRIGGER {} {} UPDATE OF {} ON sprint FOR EACH ROW "
                             "WHEN (NEW.repo_id={} AND NEW.id={}) EXECUTE FUNCTION {}()").format(
                                 sql.Identifier(name), sql.SQL(timing), sql.Identifier(operation),
                                 sql.Literal(store.repo_id), sql.Literal(sprint), sql.Identifier(name)))
        conn.commit()
        try:
            yield
        finally:
            conn.rollback()
            conn.execute(sql.SQL("DROP TRIGGER {} ON sprint").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))
            conn.commit()

    def await_wait(owner, pid, first):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blockers = owner.conn.execute("SELECT pg_blocking_pids(%s) AS blockers", (pid,)).fetchone()["blockers"]
            if first.conn.info.backend_pid in blockers:
                return True
            time.sleep(0.005)
        return False

    isolation = store.conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
    assert isolation == "read committed"
    store.conn.commit()
    return SprintMutationDriver(pg, store, sprint, independent, open_store, lambda other: other.conn.close(),
                                abort_update, lambda other: other.conn.info.backend_pid, await_wait, isolation)


class TestPostgresSprintKindStatus(SprintKindStatusCases):
    pass


def _pending_caller(d):
    protected = pg.create_sprint(d.store, "protected " + uuid4().hex, goal="durable original")
    d.store.conn.execute("UPDATE sprint SET goal='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, protected))
    return protected


@pytest.mark.parametrize("operation,value", [("kind", "backlog"), ("status", "active")])
def test_implicit_caller_success_commits_target_and_pending_write(mutation_driver, operation, value):
    d = mutation_driver
    protected = _pending_caller(d)
    d.invoke(operation, value)
    with d.independent() as other:
        assert pg.get_sprint(other, protected)["goal"] == "caller pending"
        assert d.snapshot(other)["sprint"][operation] == value
        assert d.snapshot(other)["events"] == []


@pytest.mark.parametrize("operation,value", [("kind", "backlog"), ("status", "active")])
def test_stale_basis_failure_rolls_back_implicit_caller_write(mutation_driver, operation, value):
    d = mutation_driver
    initial = d.snapshot(d.store)
    basis = d.basis(operation)
    stale = basis.replace("@kind:active_sprint", "@kind:archive") if operation == "kind" else basis.replace("@status:planned", "@status:active")
    protected = _pending_caller(d)
    with pytest.raises(db.StatusConflict):
        d.invoke(operation, value, basis=stale)
    assert pg.get_sprint(d.store, protected)["goal"] == "durable original"
    with d.independent() as other:
        assert d.snapshot(other) == initial
        assert pg.get_sprint(other, protected)["goal"] == "durable original"


@pytest.mark.parametrize("operation,value", [("kind", "backlog"), ("status", "active")])
def test_basis_prevalidation_keeps_caller_write_pending(mutation_driver, operation, value):
    d = mutation_driver
    initial = d.snapshot(d.store)
    protected = _pending_caller(d)
    try:
        with pytest.raises(ValueError, match="expected_revision"):
            d.invoke(operation, value, basis="bad")
        assert pg.get_sprint(d.store, protected)["goal"] == "caller pending"
        with d.independent() as other:
            assert d.snapshot(other) == initial
            assert pg.get_sprint(other, protected)["goal"] == "durable original"
    finally:
        d.store.conn.rollback()


def test_managed_kind_caller_keeps_target_pending_until_caller_rollback(mutation_driver, record_property, request):
    d = mutation_driver
    initial = d.snapshot(d.store)
    protected = pg.create_sprint(d.store, "managed kind caller", goal="durable original")
    observations = []
    with d.store.conn.transaction(force_rollback=True):
        d.store.conn.execute("UPDATE sprint SET goal='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, protected))
        with pytest.raises(psycopg.ProgrammingError, match="Explicit rollback") as caught:
            d.invoke("kind", "backlog")
        pending = d.snapshot(d.store)
        assert pending["sprint"]["kind"] == "backlog" and pending["events"] == []
        assert pg.get_sprint(d.store, protected)["goal"] == "caller pending"
        with d.independent() as other:
            durable = d.snapshot(other)
            assert durable == initial
            assert pg.get_sprint(other, protected)["goal"] == "durable original"
        observations.append({"step": "managed-kind-error-before-caller-cleanup", "error": str(caught.value), "pending": pending, "independent": durable})
    assert d.snapshot(d.store) == initial
    assert pg.get_sprint(d.store, protected)["goal"] == "durable original"
    observations.append({"step": "caller-context-rollback", "state": d.snapshot(d.store)})
    record_property("state_history", json.dumps({"history": "managed-kind-caller", "backend": "sprintctl.pg", "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))


def test_two_phase_status_caller_keeps_target_pending_until_caller_rollback(mutation_driver, record_property, request):
    d = mutation_driver
    initial = d.snapshot(d.store)
    protected = pg.create_sprint(d.store, "TPC status caller", goal="durable original")
    conn, observations = d.store.conn, []
    conn.tpc_begin(conn.xid(2785, "status-" + str(d.sprint), "caller"))
    try:
        conn.execute("UPDATE sprint SET goal='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, protected))
        with pytest.raises(psycopg.ProgrammingError, match=r"rollback\(\).*two-phase") as caught:
            d.invoke("status", "active")
        pending = d.snapshot(d.store)
        assert pending["sprint"]["status"] == "active" and pending["events"] == []
        assert pg.get_sprint(d.store, protected)["goal"] == "caller pending"
        with d.independent() as other:
            durable = d.snapshot(other)
            assert durable == initial
            assert pg.get_sprint(other, protected)["goal"] == "durable original"
        observations.append({"step": "two-phase-status-error-before-caller-cleanup", "error": str(caught.value), "pending": pending, "independent": durable})
    finally:
        conn.tpc_rollback()
    assert d.snapshot(d.store) == initial
    assert pg.get_sprint(d.store, protected)["goal"] == "durable original"
    observations.append({"step": "caller-tpc-rollback", "state": d.snapshot(d.store)})
    record_property("state_history", json.dumps({"history": "two-phase-status-caller", "backend": "sprintctl.pg", "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))


@pytest.mark.parametrize("operation,value", [("kind", "backlog"), ("status", "active")])
def test_foreign_repository_sprint_remains_missing(mutation_driver, pg_test_scope, operation, value):
    d = mutation_driver
    foreign = replace(d.store, repo_id=pg_test_scope("mutation-foreign"), authority_repo_uuid=str(uuid4()))
    pg.init_db(foreign)
    sprint = pg.create_sprint(foreign, "foreign kind/status")
    before = pg.get_sprint(foreign, sprint)
    with pytest.raises(ValueError, match=f"Sprint #{sprint} not found"):
        d.invoke(operation, value, sprint=sprint)
    assert pg.get_sprint(foreign, sprint) == before
    assert pg.list_events(foreign, sprint) == []
