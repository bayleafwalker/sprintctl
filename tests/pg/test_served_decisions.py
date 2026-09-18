"""PostgreSQL: served Decision and Release operations (S3 PR3).

Runs ``tests.test_served_decisions.DecisionOperationContract`` against the
PostgreSQL authority composed the way the served runtime composes it.
"""
from __future__ import annotations

import pytest

from sprintctl.application import WorkApplication
from tests.pg._shared import PG_MARKS, _uid, pg
from tests.pg.test_decisions import _legacy_item
from tests.test_served_decisions import DecisionOperationContract, Env

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
