from contextlib import contextmanager
import time
import pytest
from tests._reservation_lifecycle_contract import Driver, LifecycleCases
from tests.pg._shared import pg_test_scope, PG_MARKS, pg, psycopg, dict_row, _PG_URL, assert_disposable_connection
pytestmark = PG_MARKS

@pytest.fixture
def lifecycle_driver(pg_test_scope):
    connection = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    store = pg.PgStore(connection, pg_test_scope('lifecycle'))
    pg.init_db(store)
    sprint = pg.create_sprint(store, 'lifecycle', status='active')
    track = pg.get_or_create_track(store, sprint, 'lifecycle')
    item = pg.create_work_item(store, sprint, track, 'lifecycle')
    @contextmanager
    def independent():
        other = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(other)
        try: yield pg.PgStore(other, store.repo_id)
        finally: other.close()
    def backdate(ids, stamp):
        for ident in ids:
            connection.execute('UPDATE reservation SET last_activity_at=%s WHERE repo_id=%s AND id=%s', (stamp, store.repo_id, ident))
        connection.commit()
    def await_wait(owner, other_pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = owner.conn.execute('SELECT pg_blocking_pids(%s) AS blockers', (other_pid,)).fetchone()
            if owner.conn.info.backend_pid in row['blockers']:
                return True
            time.sleep(0.005)
        return False
    try: yield Driver(pg, store, item, sprint, track, independent, backdate,
                      lambda other: other.conn.info.backend_pid, await_wait)
    finally: connection.close()

class TestPostgresLifecycleContract(LifecycleCases):
    pass

from tests._reservation_lifecycle_contract import invoke

@pytest.mark.parametrize('operation', ['touch', 'reassign', 'release', 'sweep'])
def test_pending_writes_follow_public_pg_precommit_convention(lifecycle_driver, operation):
    d = lifecycle_driver
    row = d.reserve()
    d.handle.conn.execute("UPDATE work_item SET description='caller-owned' WHERE repo_id=%s AND id=%s", (d.handle.repo_id, d.item))
    invoke(d, operation, row['id'])
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == 'caller-owned'

@pytest.mark.parametrize('operation', ['touch', 'reassign', 'release', 'sweep'])
def test_managed_transaction_refusal_does_not_rollback_caller(lifecycle_driver, operation):
    d = lifecycle_driver
    row = d.reserve()
    with d.handle.conn.transaction(force_rollback=True):
        d.handle.conn.execute("UPDATE work_item SET description='caller-owned' WHERE repo_id=%s AND id=%s", (d.handle.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match='Explicit commit'):
            invoke(d, operation, row['id'])
        assert pg.get_work_item(d.handle, d.item)['description'] == 'caller-owned'
        with d.independent() as other:
            assert pg.get_work_item(other, d.item)['description'] == ''
            assert pg.get_reservation(other, row['id'])['state'] == 'active'

@pytest.mark.parametrize('operation', ['touch', 'reassign', 'release', 'sweep'])
def test_two_phase_transaction_refusal_preserves_caller(lifecycle_driver, operation):
    d = lifecycle_driver
    row = d.reserve()
    d.handle.conn.tpc_begin(d.handle.conn.xid(2772, f'lifecycle-{d.item}', operation))
    try:
        d.handle.conn.execute("UPDATE work_item SET description='caller-owned' WHERE repo_id=%s AND id=%s", (d.handle.repo_id, d.item))
        with pytest.raises(psycopg.ProgrammingError, match='two-phase'):
            invoke(d, operation, row['id'])
        assert pg.get_work_item(d.handle, d.item)['description'] == 'caller-owned'
    finally:
        d.handle.conn.tpc_rollback()
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == ''
        assert pg.get_reservation(other, row['id'])['state'] == 'active'

@pytest.mark.parametrize('operation', ['missing-touch', 'wrong-session', 'missing-reassign', 'missing-release', 'terminal-release'])
def test_invalid_or_terminal_peek_does_not_precommit(lifecycle_driver, operation):
    d = lifecycle_driver
    row = d.reserve()
    if operation == 'terminal-release':
        pg.release_reservation(d.handle, row['id'])
    d.handle.conn.execute("UPDATE work_item SET description='caller-pending' WHERE repo_id=%s AND id=%s", (d.handle.repo_id, d.item))
    if operation == 'terminal-release':
        assert pg.release_reservation(d.handle, row['id'])['state'] == 'released'
    else:
        with pytest.raises(ValueError):
            if operation == 'wrong-session':
                pg.touch_reservation(d.handle, row['id'], session_id='stranger')
            else:
                invoke(d, operation.split('-')[1], -1)
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == ''
    d.handle.conn.rollback()

@pytest.mark.parametrize('interleave', ['new-arrival', 'refresh', 'completion'])
def test_sweep_rechecks_frozen_candidates_after_independent_commit(lifecycle_driver, monkeypatch, interleave):
    d = lifecycle_driver
    row = d.reserve()
    d.backdate([row['id']], '2000-01-01T00:00:00Z')
    pg.set_work_item_status(d.handle, d.item, 'active')
    connection_type = type(d.handle.conn)
    original_cursor = connection_type.cursor
    observed = []
    additional = []

    class CursorProxy:
        def __init__(self, cursor):
            self.cursor = cursor
            self.candidates = False
        def __enter__(self):
            self.cursor.__enter__()
            return self
        def __exit__(self, *args):
            return self.cursor.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.cursor, name)
        def execute(self, query, *args, **kwargs):
            self.candidates = isinstance(query, str) and query.startswith('SELECT r.id, r.work_item_id FROM reservation r JOIN work_item i ')
            return self.cursor.execute(query, *args, **kwargs)
        def fetchall(self):
            rows = self.cursor.fetchall()
            if self.candidates and not observed:
                observed.extend(r['id'] for r in rows)
                with d.independent() as other:
                    if interleave == 'refresh':
                        pg.touch_reservation(other, row['id'], session_id='session')
                    elif interleave == 'completion':
                        pg.set_work_item_status(other, d.item, 'done')
                    else:
                        newcomer = pg.reserve(other, d.item, actor='new', session_id='new')
                        other.conn.execute("UPDATE reservation SET last_activity_at='2000-01-01T00:00:00Z' WHERE repo_id=%s AND id=%s", (other.repo_id, newcomer['id']))
                        other.conn.commit()
                        additional.append(newcomer['id'])
            return rows

    def cursor(connection, *args, **kwargs):
        value = original_cursor(connection, *args, **kwargs)
        return CursorProxy(value) if connection is d.handle.conn else value
    monkeypatch.setattr(connection_type, 'cursor', cursor)
    changed = pg.sweep_stale_reservations(d.handle)
    assert observed == [row['id']]
    assert [r['id'] for r in changed] == ([row['id']] if interleave == 'new-arrival' else [])
    if additional:
        assert pg.get_reservation(d.handle, additional[0])['state'] == 'active'
    events = d.snapshot()['events']
    interrupted = [e for e in events if e['event_type'] == 'reservation.interrupted']
    assert len(interrupted) == (1 if interleave == 'new-arrival' else 0)


def test_owned_pg_failure_does_not_undo_already_precommitted_caller_write(lifecycle_driver, monkeypatch):
    d = lifecycle_driver
    row = d.reserve()
    d.handle.conn.execute("UPDATE work_item SET description='caller-owned' WHERE repo_id=%s AND id=%s", (d.handle.repo_id, d.item))
    d.fail_event(monkeypatch, 'reservation.released')
    with pytest.raises(RuntimeError, match='injected lifecycle'):
        pg.release_reservation(d.handle, row['id'])
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['description'] == 'caller-owned'
        assert pg.get_reservation(other, row['id'])['state'] == 'active'
        assert [e['event_type'] for e in pg.list_events(other, d.sprint) if e['work_item_id'] == d.item] == ['reservation.reserved']

@pytest.mark.parametrize('operation', ['touch', 'reassign', 'release', 'sweep'])
def test_direct_pg_timestamp_response_types_stay_native(lifecycle_driver, operation):
    from datetime import datetime
    d = lifecycle_driver
    row = d.reserve()
    d.backdate([row['id']], '2000-01-01T00:00:00Z')
    result = invoke(d, operation, row['id'])
    updated = result[0] if operation == 'sweep' else result
    assert isinstance(updated['created_at'], datetime)
    assert isinstance(updated['last_activity_at'], datetime)
    if operation in ('release', 'sweep'):
        assert isinstance(updated['released_at'], datetime)
    else:
        assert updated['released_at'] is None
    if operation == 'release':
        assert isinstance(pg.release_reservation(d.handle, row['id'])['released_at'], datetime)
