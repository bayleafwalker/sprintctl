"""SQLite half of the actual-backend unmet-obligations compatibility oracle."""
import pytest

from sprintctl import db
from tests._unmet_obligations_contract import ObligationsDriver, UnmetObligationsCases


@pytest.fixture
def obligations_driver(conn):
    sprint = db.create_sprint(conn, "obligation report", status="active")
    return ObligationsDriver(db, conn, sprint)


class TestSQLiteUnmetObligations(UnmetObligationsCases):
    pass


def test_report_does_not_mutate_database_or_commit_caller_write(obligations_driver, db_path):
    d = obligations_driver
    digest, row = d.accepted()
    d.store.execute("UPDATE work_item SET description='caller pending' WHERE id=?", (row["item_id"],))
    before = list(d.store.iterdump())
    assert db.unmet_obligations(d.store, digest) == [row]
    assert d.report() == {"count": 1, "items": [row]}
    assert list(d.store.iterdump()) == before
    assert d.store.in_transaction
    with db.get_connection(db_path) as other:
        assert db.get_work_item(other, row["item_id"])["description"] != "caller pending"
    d.store.rollback()


def test_successful_report_leaves_database_file_bytes_unchanged(obligations_driver, db_path):
    d = obligations_driver
    digest, row = d.accepted()
    before = db_path.read_bytes()
    assert db.unmet_obligations(d.store, digest) == [row]
    assert d.report() == {"count": 1, "items": [row]}
    assert db_path.read_bytes() == before
