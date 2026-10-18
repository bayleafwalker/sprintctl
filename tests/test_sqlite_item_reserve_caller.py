"""Owned SQLite admission/status boundaries must preserve caller transactions."""
from contextlib import closing

import pytest

from sprintctl import db


TABLES = ('sprint', 'track', 'work_item', 'reservation', 'work_release', 'work_decision', 'event')


def snapshot(conn):
    return {name: [dict(row) for row in conn.execute(f'SELECT * FROM {name} ORDER BY rowid')]
            for name in TABLES}


def prepare(conn):
    sid = db.create_sprint(conn, 'target sprint', status='active')
    tid = db.get_or_create_track(conn, sid, 'target track')
    iid = db.create_work_item(conn, sid, tid, 'target item')
    db.reserve(conn, iid, actor='predecessor', session_id='old-session')
    db.record_decision(conn, iid, 'revise', actor='reviewer', rationale='existing decision')
    protected = db.create_sprint(conn, 'caller sprint', goal='durable original')
    return sid, tid, iid, protected


def invoke(conn, operation, iid):
    if operation == 'reserve':
        return db.reserve(conn, iid, actor='incoming', session_id='new-session', interrupt_existing=True)
    return db.set_work_item_status(conn, iid, 'active', actor='incoming',
                                   expected_revision=db.item_status_revision(db.get_work_item(conn, iid)))


@pytest.mark.parametrize('operation', ('reserve', 'status'))
@pytest.mark.parametrize('caller', ('write', 'read', 'savepoint'))
def test_nested_refusal_preserves_caller_and_existing_ledgers(conn, db_path, operation, caller):
    _sid, _tid, iid, protected = prepare(conn)
    durable = snapshot(conn)
    if caller == 'read':
        conn.execute('BEGIN')
        conn.execute('SELECT goal FROM sprint WHERE id=?', (protected,)).fetchone()
    else:
        conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('outer pending', protected))
        if caller == 'savepoint':
            conn.execute('SAVEPOINT caller_scope')
            conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('inner pending', protected))
    pending = snapshot(conn)
    changes = conn.total_changes
    with pytest.raises(ValueError, match='idle SQLite connection'):
        invoke(conn, operation, iid)
    assert conn.in_transaction
    assert conn.total_changes == changes
    assert snapshot(conn) == pending
    assert conn.execute('SELECT 1').fetchone()[0] == 1
    with closing(db.get_connection(db_path)) as other:
        assert snapshot(other) == durable
    if caller == 'savepoint':
        conn.execute('ROLLBACK TO caller_scope')
        assert db.get_sprint(conn, protected)['goal'] == 'outer pending'
        conn.execute('RELEASE caller_scope')
        assert conn.in_transaction
    conn.rollback()
    assert not conn.in_transaction
    assert snapshot(conn) == durable


@pytest.mark.parametrize('savepoint', (False, True))
def test_reserve_lookup_preserves_caller_created_uncommitted_target(conn, db_path, savepoint):
    sid, tid, _iid, protected = prepare(conn)
    durable = snapshot(conn)
    conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('outer pending', protected))
    if savepoint:
        conn.execute('SAVEPOINT caller_scope')
    target = db._workitemcore.create_work_item(
        db._WorkItemSqlite(conn, commit_insert=False), sid, tid, 'uncommitted target')
    pending = snapshot(conn)
    changes = conn.total_changes
    with pytest.raises(ValueError, match='idle SQLite connection'):
        invoke(conn, 'reserve', target)
    assert conn.in_transaction and conn.total_changes == changes
    assert snapshot(conn) == pending
    with closing(db.get_connection(db_path)) as other:
        assert db.get_work_item(other, target) is None
        assert snapshot(other) == durable
    if savepoint:
        conn.execute('ROLLBACK TO caller_scope')
        assert db.get_work_item(conn, target) is None
        conn.execute('RELEASE caller_scope')
        assert conn.in_transaction
        assert db.get_sprint(conn, protected)['goal'] == 'outer pending'
    conn.rollback()
    assert snapshot(conn) == durable


@pytest.mark.parametrize('case,fragment', [
    ('role-before-basis', 'role'), ('basis-before-contract', 'revision'),
    ('contract-before-item', 'acceptance_contract'), ('missing-item', 'not found'),
    ('status-revision', 'revision'),
])
def test_existing_pre_handler_errors_precede_idle_guard(conn, db_path, case, fragment):
    _sid, _tid, iid, protected = prepare(conn)
    durable = snapshot(conn)
    conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('pending validation caller', protected))
    pending = snapshot(conn)
    changes = conn.total_changes
    with pytest.raises(ValueError) as caught:
        if case == 'status-revision':
            db.set_work_item_status(conn, iid, 'active', expected_revision='bad')
        else:
            options = {'actor': 'incoming', 'session_id': 'new-session'}
            target = iid
            if case == 'role-before-basis':
                options.update(role='bad-role', expected_revision='bad', acceptance_contract=[])
            elif case == 'basis-before-contract':
                options.update(expected_revision='bad', acceptance_contract=[])
            elif case == 'contract-before-item':
                options['acceptance_contract'] = []
                target = 999999
            else:
                target = 999999
            db.reserve(conn, target, **options)
    assert fragment in str(caught.value).lower()
    assert 'idle' not in str(caught.value).lower()
    assert conn.in_transaction and conn.total_changes == changes
    assert snapshot(conn) == pending
    with closing(db.get_connection(db_path)) as other:
        assert snapshot(other) == durable
    conn.rollback()
    assert snapshot(conn) == durable
