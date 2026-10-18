"""Run the same fresh-admission and historical-replay contract on PostgreSQL."""
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from sprintctl.application import WorkApplication
from tests.pg._shared import PG_MARKS, _PG_URL, _append_authority_command, assert_disposable_connection, authority, dict_row, outbox, pg, psycopg
from tests.test_retirement_rationale import RetirementRationaleContract, new_item

pytestmark = PG_MARKS


@pytest.fixture
def owner(store):
    return pg, store


@pytest.fixture
def owner_factory(store):
    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as connection:
            assert_disposable_connection(connection)
            yield replace(store, conn=connection)
    return independent


@pytest.fixture
def owner_application(store):
    return WorkApplication.postgres(store)


class TestPostgresRetirementRationale(RetirementRationaleContract):
    pass


@pytest.mark.parametrize("kind", ["reject", "withdraw", "supersede"])
def test_authority_decision_writer_refuses_blank_retirement(store, tmp_path, kind):
    _sprint, item_id, successor = new_item(pg, store)
    item = pg.get_work_item(store, item_id)
    payload = {"kind": kind, "rationale": " \t"}
    if kind == "supersede":
        payload["superseded_by_aggregate_uuid"] = pg.get_work_item(store, successor)["aggregate_uuid"]
    with closing(outbox.open_outbox(tmp_path / "retirement-producer.db")) as producer:
        command = _append_authority_command(
            producer, store, record_type="decision.record", aggregate_type="item",
            aggregate_uuid=item["aggregate_uuid"], basis_revision=authority.item_revision(item), payload=payload,
        )
        refusal = authority.arbitrate_command(store, command)
    assert refusal.accepted is False
    assert refusal.reason_code == "invalid-transition"
    assert "non-empty rationale" in refusal.reason_detail
    assert pg.get_work_item(store, item_id)["status"] == "pending"
    assert pg.list_decisions(store, item_id) == []
