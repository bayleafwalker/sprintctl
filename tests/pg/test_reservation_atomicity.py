from contextlib import contextmanager

import pytest

from tests._reservation_atomicity_contract import ReservationAtomicityCases, ReservationDriver
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg

pytestmark = PG_MARKS


@pytest.fixture
def reservation_driver(store, sprint_id, track_id):
    item = pg.create_work_item(store, sprint_id, track_id, "atomic reservation")

    @contextmanager
    def independent():
        connection = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(connection)
        try:
            yield pg.PgStore(connection, store.repo_id)
        finally:
            connection.close()

    return ReservationDriver(pg, store, sprint_id, item, independent)


class TestPostgresReservationAtomicity(ReservationAtomicityCases):
    pass
