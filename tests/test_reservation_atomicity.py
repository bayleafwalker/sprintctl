from contextlib import contextmanager

import pytest

from sprintctl import db
from tests._reservation_atomicity_contract import ReservationAtomicityCases, ReservationDriver


@pytest.fixture
def reservation_driver(conn, db_path, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "atomicity")
    item = db.create_work_item(conn, active_sprint["id"], track, "atomic reservation")

    @contextmanager
    def independent():
        other = db.get_connection(db_path)
        try:
            yield other
        finally:
            other.close()

    return ReservationDriver(db, conn, active_sprint["id"], item, independent)


class TestSqliteReservationAtomicity(ReservationAtomicityCases):
    pass
