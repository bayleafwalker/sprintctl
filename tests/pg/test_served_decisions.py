"""PostgreSQL: served Decision and Release operations (S3 PR3).

Runs ``tests.test_served_decisions.DecisionOperationContract`` against the
PostgreSQL authority composed the way the served runtime composes it.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import replace

import pytest

from sprintctl.application import ApplicationRejection, WorkApplication
from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    _uid,
    assert_disposable_connection,
    dict_row,
    pg,
    psycopg,
)
from tests.pg.test_decisions import _legacy_item
from tests.test_served_decisions import DecisionOperationContract, Env, _context

pytestmark = PG_MARKS


def _item(store, status="active"):
    sprint_id = pg.create_sprint(store, f"Decide-{_uid()}", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "decisions")
    item_id = pg.create_work_item(store, sprint_id, track_id, f"decide {_uid()}")
    if status != "pending":
        pg.set_work_item_status(store, item_id, status)
    return item_id


@pytest.fixture
def env(store):
    return Env(
        app=WorkApplication.postgres(store),
        backend=pg,
        store=store,
        new_item=lambda status="active": _item(store, status),
        legacy_item=lambda status: _legacy_item(store, status)[2],
    )


class TestPostgresDecisionOperations(DecisionOperationContract):
    pass


def _second_store(store):
    connection = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    return replace(store, conn=connection)


def test_concurrent_same_key_on_different_items_records_one_decision(store):
    """The key is locked, not just the item: exactly one request wins."""
    for _attempt in range(5):
        items = [_item(store), _item(store)]
        stores = [_second_store(store), _second_store(store)]
        key = uuid.uuid4().hex
        barrier = threading.Barrier(2)
        outcomes: list[object] = [None, None]

        def decide(index):
            app = WorkApplication.postgres(stores[index])
            barrier.wait()
            try:
                outcomes[index] = app.invoke(
                    "work.decision.record",
                    {"item_id": items[index], "kind": "revise", "rationale": "same",
                     "evidence_digests": []},
                    _context("alice", key),
                )
            except ApplicationRejection as exc:
                outcomes[index] = exc

        threads = [threading.Thread(target=decide, args=(i,)) for i in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            for extra in stores:
                extra.conn.close()
        rejected = [o for o in outcomes if isinstance(o, ApplicationRejection)]
        recorded = [o for o in outcomes if isinstance(o, dict)]
        assert len(recorded) == 1 and len(rejected) == 1, outcomes
        assert (rejected[0].code, rejected[0].http_status) == ("idempotency-conflict", 409)
        decided = sum(len(pg.list_decisions(store, item_id)) for item_id in items)
        assert decided == 1


def test_a_value_postgres_cannot_store_is_a_422_not_a_500(store):
    item_id = _item(store)
    app = WorkApplication.postgres(store)
    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke(
            "work.item.note",
            {"item_id": item_id, "note_type": "update", "summary": "bad\x00summary"},
            _context(),
        )
    assert (rejected.value.code, rejected.value.http_status) == ("invalid-value", 422)
    # The connection is usable afterwards.
    assert app.invoke("work.read.item", {"item_id": item_id}, _context())["item"]["id"] == item_id
