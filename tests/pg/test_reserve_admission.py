"""Actual disposable PostgreSQL admission and noncommitting participant oracle."""
from contextlib import contextmanager
from dataclasses import replace
import time
from uuid import uuid4

import pytest

from tests._reserve_admission_contract import AdmissionCases, AdmissionDriver, retain
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, pg, psycopg

pytestmark = PG_MARKS


@pytest.fixture
def admission_driver(store, sprint_id, track_id):
    item = pg.create_work_item(store, sprint_id, track_id, 'admission oracle')
    assert store.conn.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'
    store.conn.commit()  # Fixture preflight before any caller-owned test writes.

    @contextmanager
    def independent():
        with psycopg.connect(_PG_URL, row_factory=dict_row) as connection:
            assert_disposable_connection(connection)
            assert connection.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'] == 'read committed'
            connection.commit()
            yield replace(store, conn=connection)

    @contextmanager
    def sql_fault(operation, when):
        from psycopg import sql
        name = 'reserve_admission_fault_' + uuid4().hex
        connection = store.conn
        connection.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected admission SQL fault'; END $$").format(sql.Identifier(name)))
        connection.execute(sql.SQL('CREATE TRIGGER {} {} {} ON reservation FOR EACH ROW '
            'WHEN (NEW.repo_id={} AND NEW.work_item_id={}) EXECUTE FUNCTION {}()').format(
                sql.Identifier(name), sql.SQL(when), sql.SQL(operation), sql.Literal(store.repo_id),
                sql.Literal(item), sql.Identifier(name)))
        connection.commit()
        try:
            yield
        finally:
            connection.rollback()
            connection.execute(sql.SQL('DROP TRIGGER {} ON reservation').format(sql.Identifier(name)))
            connection.execute(sql.SQL('DROP FUNCTION {}()').format(sql.Identifier(name)))
            connection.commit()

    def lock_admission(other):
        other.conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (store.repo_id,))

    def await_wait(blocker, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pids = blocker.conn.execute('SELECT pg_blocking_pids(%s) AS pids', (pid,)).fetchone()['pids']
            if blocker.conn.info.backend_pid in pids:
                return True
            time.sleep(.005)
        return False

    return AdmissionDriver(pg, store, item, sprint_id, independent, sql_fault, lock_admission,
        lambda other: other.conn.info.backend_pid, await_wait, 'pg')


class TestPostgresReserveAdmission(AdmissionCases):
    pass


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_direct_participant_leaves_all_provisional_effects_to_owner(admission_driver, finish, record_property):
    d = admission_driver
    first = d.reserve()
    before = d.snapshot()
    try:
        d.handle.conn.execute('UPDATE work_item SET assignee=%s WHERE repo_id=%s AND id=%s',
            ('owner pending', d.handle.repo_id, d.item))
        with d.handle.conn.cursor() as cur:
            row = pg.reserve_in_transaction(cur, d.handle, d.item, actor='participant',
                session_id='participant', interrupt_existing=True, acceptance_contract={'review_required': False})
        assert d.handle.conn.info.transaction_status.name == 'INTRANS'
        pending = d.snapshot(d.handle)
        assert len(pending['reservations']) == 2 and len(pending['releases']) == 2
        assert pending['reservations'][0]['id'] == first['id'] and pending['reservations'][0]['state'] == 'interrupted'
        assert d.snapshot() == before
        retain(record_property, 'pg-direct-participant-' + finish, [{'pending': pending, 'durable': before}])
        getattr(d.handle.conn, finish)()
        durable = d.snapshot()
        assert durable == (pending if finish == 'commit' else before)
        with d.independent() as other:
            assert pg.get_work_item(other, d.item)['assignee'] == ('owner pending' if finish == 'commit' else None)
    finally:
        d.handle.conn.rollback()


def test_participant_python_failure_preserves_pending_owner_and_effects_before_cleanup(admission_driver, monkeypatch, record_property):
    d = admission_driver
    d.reserve()
    before = d.snapshot()
    original = pg._reservation_event_in_transaction
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        if args[3] == 'reservation.reserved':
            raise RuntimeError('participant after-event fault')
    try:
        d.handle.conn.execute('UPDATE work_item SET assignee=%s WHERE repo_id=%s AND id=%s',
            ('owner pending', d.handle.repo_id, d.item))
        with monkeypatch.context() as fault:
            fault.setattr(pg, '_reservation_event_in_transaction', fail)
            with d.handle.conn.cursor() as cur:
                with pytest.raises(RuntimeError, match='participant after-event'):
                    pg.reserve_in_transaction(cur, d.handle, d.item, actor='participant',
                        session_id='participant', interrupt_existing=True)
        assert d.handle.conn.info.transaction_status.name == 'INTRANS'
        assert pg.get_work_item(d.handle, d.item)['assignee'] == 'owner pending'
        pending = d.snapshot(d.handle)
        assert len(pending['reservations']) == 2 and pending['reservations'][0]['state'] == 'interrupted'
        assert len(pending['events']) == len(before['events']) + 2
        assert d.snapshot() == before
        retain(record_property, 'pg-failed-participant-before-owner-cleanup', [{'pending': pending, 'durable': before}])
    finally:
        d.handle.conn.rollback()
    assert d.snapshot() == before


@pytest.mark.parametrize('succeed', [True, False])
def test_public_wrapper_owns_implicit_caller_work_even_validation_failure(admission_driver, succeed):
    d = admission_driver
    d.handle.conn.execute('UPDATE work_item SET assignee=%s WHERE repo_id=%s AND id=%s',
        ('owner pending', d.handle.repo_id, d.item))
    if succeed:
        d.reserve()
    else:
        with pytest.raises(ValueError, match='invalid reservation role'):
            d.reserve(role='invalid')
    d.assert_idle()
    with d.independent() as other:
        assert pg.get_work_item(other, d.item)['assignee'] == ('owner pending' if succeed else None)


def test_same_connection_guard_precedes_invalid_arguments_and_all_effects(admission_driver):
    d = admission_driver
    before = d.snapshot()
    with d.independent() as other:
        with other.conn.cursor() as cur:
            with pytest.raises(RuntimeError, match='share its event connection'):
                pg.reserve_in_transaction(cur, d.handle, d.item, actor='actor', session_id='session',
                    role='bad', expected_revision='bad', acceptance_contract={'review_required': 'bad'})
    d.assert_idle()
    assert d.snapshot() == before
