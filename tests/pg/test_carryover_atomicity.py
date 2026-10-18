from contextlib import contextmanager
from dataclasses import replace
import time

import pytest

from tests._carryover_atomicity_contract import CarryoverAtomicityCases, CarryoverDriver
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from sprintctl import maintain
from tests.pg.test_work_lease import A, _claim, _complete, _legacy_bar, _run
from tests._carryover_atomicity_contract import InjectedCarryoverFailure

pytestmark = PG_MARKS


@pytest.fixture
def carryover_driver(store):
    source = pg.create_sprint(store, "carry source", status="active")
    target = pg.create_sprint(store, "carry target")
    track = pg.get_or_create_track(store, source, "carry track")
    item = pg.create_work_item(store, source, track, "Carry once", description="Keep detail", assignee="owner")

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as connection:
            assert_disposable_connection(connection)
            yield replace(store, conn=connection)

    def await_wait(owner, other_pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = owner.conn.execute("SELECT pg_blocking_pids(%s) AS blockers", (other_pid,)).fetchone()
            if owner.conn.info.backend_pid in row["blockers"]:
                return True
            time.sleep(0.005)
        return False

    return CarryoverDriver(pg, store, source, target, item, independent,
                          lambda other: other.conn.info.backend_pid, await_wait)


class TestPostgresCarryoverAtomicity(CarryoverAtomicityCases):
    pass


@pytest.mark.parametrize("awaiting", [False, True])
def test_carryover_transaction_retains_lease_and_report_consequences(carryover_driver, monkeypatch, awaiting):
    d = carryover_driver
    run = _run(d.store, A)
    lease = _claim(d.store, A, d.item, run, f"carry-claim-{d.item}")["lease"]
    if awaiting:
        _legacy_bar(d.store, d.item, {"verification_profile": "human-authorized"}, lease["lease_id"])
        report = _complete(d.store, A, lease["lease_id"], run, f"carry-report-{d.item}")["report"]
        assert report["disposition"] == "awaiting-verification"

    def consequences(store):
        return {
            table: store.conn.execute(
                f"SELECT * FROM {table} WHERE repo_id=%s AND work_item_id=%s ORDER BY {key}",
                (store.repo_id, d.item),
            ).fetchall()
            for table, key in (("work_lease", "lease_id"), ("work_outcome_report", "report_id"))
        }

    before = consequences(d.store)
    original = pg._insert_event

    def fail_final(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == "carried-out":
            raise InjectedCarryoverFailure("after consequences")
        return result

    with monkeypatch.context() as injected:
        injected.setattr(pg, "_insert_event", fail_final)
        with pytest.raises(InjectedCarryoverFailure):
            maintain.carryover(d.store, d.source, d.target, _m=pg)
    with d.independent() as other:
        assert consequences(other) == before
        assert pg.get_work_item(other, d.item)["status"] == "active"
        assert pg.list_work_items(other, sprint_id=d.target) == []
    maintain.carryover(d.store, d.source, d.target, _m=pg)
    after = consequences(d.store)
    assert after["work_lease"][0]["state"] == "settled"
    assert after["work_lease"][0]["end_reason"] == "item-superseded"
    if awaiting:
        assert after["work_outcome_report"][0]["disposition"] == "rejected"
        assert after["work_outcome_report"][0]["reason_code"] == "decided-supersede"


def test_pending_pg_writes_keep_existing_committing_mutation_convention(carryover_driver, monkeypatch):
    d = carryover_driver
    d.store.conn.execute("UPDATE work_item SET description='caller-pending' WHERE repo_id=%s AND id=%s",
                         (d.store.repo_id, d.item))
    original = pg._insert_event

    def fail(*args, **kwargs):
        if args[3] == "carryover":
            raise InjectedCarryoverFailure("owned item")
        return original(*args, **kwargs)

    with monkeypatch.context() as injected:
        injected.setattr(pg, "_insert_event", fail)
        with pytest.raises(InjectedCarryoverFailure):
            maintain.carryover(d.store, d.source, d.target, _m=pg)
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)["description"] == "caller-pending"
        assert pg.get_work_item(other, d.item)["status"] == "pending"
        assert pg.list_work_items(other, sprint_id=d.target) == []


def test_managed_outer_pg_transaction_is_refused_before_copy_without_rollback(carryover_driver):
    d = carryover_driver
    with d.store.conn.transaction(force_rollback=True):
        d.store.conn.execute("UPDATE work_item SET description='outer-pending' WHERE repo_id=%s AND id=%s",
                             (d.store.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match="Explicit commit"):
            maintain.carryover(d.store, d.source, d.target, _m=pg)
        assert pg.get_work_item(d.store, d.item)["description"] == "outer-pending"
        assert pg.list_work_items(d.store, sprint_id=d.target) == []
        with d.independent() as other:
            assert pg.get_work_item(other, d.item)["description"] == "Keep detail"
            assert pg.list_work_items(other, sprint_id=d.target) == []
    assert pg.get_work_item(d.store, d.item)["description"] == "Keep detail"


def test_two_phase_pg_transaction_is_refused_before_copy(carryover_driver):
    d = carryover_driver
    connection = d.store.conn
    xid = connection.xid(2768, f"carry-{d.item}", "admission")
    connection.tpc_begin(xid)
    try:
        connection.execute("UPDATE work_item SET description='two-phase' WHERE repo_id=%s AND id=%s",
                           (d.store.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match="two-phase"):
            maintain.carryover(d.store, d.source, d.target, _m=pg)
        assert pg.get_work_item(d.store, d.item)["description"] == "two-phase"
        assert pg.list_work_items(d.store, sprint_id=d.target) == []
    finally:
        connection.tpc_rollback()
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)["description"] == "Keep detail"
