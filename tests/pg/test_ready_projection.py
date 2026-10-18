from contextlib import contextmanager

import pytest
from tests._ready_projection_contract import ReadyDriver, ReadyProjectionCases
from tests.pg._shared import PG_MARKS, pg, psycopg, dict_row, _PG_URL, assert_disposable_connection

pytestmark = PG_MARKS


@pytest.fixture
def ready_driver(pg_test_scope):
    connection = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    store = pg.PgStore(connection, pg_test_scope('ready-projection'))
    pg.init_db(store)
    sprint = pg.create_sprint(store, 'ready projection', status='active')
    @contextmanager
    def independent():
        other = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(other)
        try:
            yield pg.PgStore(other, store.repo_id)
        finally:
            other.close()
    def stamp(ident, value):
        connection.execute('UPDATE work_item SET created_at=%s WHERE repo_id=%s AND id=%s', (value, store.repo_id, ident))
        connection.commit()
    def write_description(ident, value):
        connection.execute('UPDATE work_item SET description=%s WHERE repo_id=%s AND id=%s', (value, store.repo_id, ident))
    try:
        yield ReadyDriver(pg, store, sprint, independent, stamp, write_description,
                          lambda: connection.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS)
    finally:
        connection.close()


class TestPostgresReadyProjection(ReadyProjectionCases):
    pass


def test_real_parked_item_is_filtered_before_blocker_reads(store, monkeypatch):
    from tests.pg import test_work_lease as lease_cases
    parked, *_ = lease_cases.TestParkedDisposition()._park(store, 'ready-projection-oracle')
    item = pg.get_work_item(store, parked)
    # Existing owner behavior: parking still excludes a row left pending.
    store.conn.execute("UPDATE work_item SET status='pending' WHERE repo_id=%s AND id=%s", (store.repo_id, parked))
    store.conn.commit()
    eligible = pg.create_work_item(store, item['sprint_id'], item['track_id'], 'eligible alongside parked')
    source = next(r for r in pg.list_work_items(store, sprint_id=item['sprint_id']) if r['id'] == eligible)
    events = []
    original_parking, original_blockers = pg._parked_items, pg.list_deps_blocking
    def parking(cursor, owner, ids):
        result = original_parking(cursor, owner, ids)
        events.append(('parking', list(ids), set(result)))
        return result
    def blockers(owner, ident):
        events.append(('blockers', ident))
        return original_blockers(owner, ident)
    monkeypatch.setattr(pg, '_parked_items', parking)
    monkeypatch.setattr(pg, 'list_deps_blocking', blockers)
    result = pg.get_ready_items(store, item['sprint_id'])
    assert result == [{**source, 'blockers_resolved': 0, 'unresolved_blockers': 0, 'effective_priority': None}]
    assert events[0][0] == 'parking' and set(events[0][1]) == {parked, eligible}
    assert parked in events[0][2]
    assert events[1:] == [('blockers', eligible)]
