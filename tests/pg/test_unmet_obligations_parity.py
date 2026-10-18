"""Registered disposable-PostgreSQL half of the report compatibility oracle."""
from dataclasses import replace
from uuid import uuid4

import pytest

from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg
from tests._unmet_obligations_contract import ObligationsDriver, UnmetObligationsCases

pytestmark = PG_MARKS


@pytest.fixture
def obligations_driver(store):
    sprint = pg.create_sprint(store, "obligation report " + uuid4().hex, status="active")
    return ObligationsDriver(pg, store, sprint)


class TestPostgresUnmetObligations(UnmetObligationsCases):
    pass


def test_report_keeps_pending_write_uncommitted(obligations_driver):
    d = obligations_driver
    digest, row = d.accepted()
    d.store.conn.execute("UPDATE work_item SET description='caller pending' WHERE repo_id=%s AND id=%s", (d.store.repo_id, row["item_id"]))
    assert pg.unmet_obligations(d.store, digest) == [row]
    assert d.report() == {"count": 1, "items": [row]}
    assert pg.get_work_item(d.store, row["item_id"])["description"] == "caller pending"
    with psycopg.connect(_PG_URL, row_factory=dict_row) as conn:
        assert_disposable_connection(conn)
        other = replace(d.store, conn=conn)
        assert pg.get_work_item(other, row["item_id"])["description"] != "caller pending"
    d.store.conn.rollback()


def test_other_repository_is_excluded(obligations_driver, pg_test_scope):
    d = obligations_driver
    foreign = replace(d.store, repo_id=pg_test_scope("foreign"), authority_repo_uuid=str(uuid4()))
    pg.init_db(foreign)
    foreign_sprint = pg.create_sprint(foreign, "foreign", status="active")
    foreign_driver = ObligationsDriver(pg, foreign, foreign_sprint)
    digest, row = foreign_driver.accepted()
    assert foreign_driver.report() == {"count": 1, "items": [row]}
    assert pg.unmet_obligations(d.store, digest) == []
    assert d.report() == {"count": 0, "items": []}
