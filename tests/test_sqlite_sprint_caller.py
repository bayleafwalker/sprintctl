"""Caller transactions survive refusal by SQLite-owned sprint mutations."""
from contextlib import closing

import pytest
from sprintctl import db


OPERATIONS = ('kind', 'status', 'close')


def prepare(conn, operation):
    target = db.create_sprint(conn, 'mutation target', goal='target unchanged',
                             status='active' if operation == 'close' else 'planned')
    protected = db.create_sprint(conn, 'caller work', goal='durable original')
    return target, protected


def invoke(conn, operation, target):
    sprint = db.get_sprint(conn, target)
    if operation == 'kind':
        return db.set_sprint_kind(conn, target, 'archive',
            expected_revision=db._sprintcore.sprint_kind_revision(sprint))
    revision = db.sprint_status_revision(sprint)
    if operation == 'status':
        return db.set_sprint_status(conn, target, 'active', expected_revision=revision)
    return db.close_sprint_with_boundary_event(conn, target, 'oracle', expected_revision=revision)


@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('caller', ('write', 'read', 'savepoint'))
def test_nested_refusal_leaves_caller_transaction_usable(conn, db_path, operation, caller):
    target, protected = prepare(conn, operation)
    target_before = db.get_sprint(conn, target)
    protected_before = db.get_sprint(conn, protected)
    if caller == 'read':
        conn.execute('BEGIN')
        conn.execute('SELECT goal FROM sprint WHERE id=?', (protected,)).fetchone()
        pending = 'durable original'
    else:
        conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('outer pending', protected))
        pending = 'outer pending'
        if caller == 'savepoint':
            conn.execute('SAVEPOINT caller_scope')
            conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('inner pending', protected))
            pending = 'inner pending'
    assert conn.in_transaction
    changes_before = conn.total_changes
    with pytest.raises(ValueError, match='idle SQLite connection'):
        invoke(conn, operation, target)
    assert conn.in_transaction
    assert conn.total_changes == changes_before
    assert conn.execute('SELECT 1').fetchone()[0] == 1
    assert db.get_sprint(conn, target) == target_before
    assert db.get_sprint(conn, protected) == {**protected_before, 'goal': pending}
    assert db.list_events(conn, target) == []
    with closing(db.get_connection(db_path)) as other:
        assert db.get_sprint(other, target) == target_before
        assert db.get_sprint(other, protected) == protected_before
        assert db.list_events(other, target) == []
    if caller == 'savepoint':
        conn.execute('ROLLBACK TO caller_scope')
        assert db.get_sprint(conn, protected)['goal'] == 'outer pending'
        conn.execute('RELEASE caller_scope')
        assert conn.in_transaction
    conn.rollback()
    assert not conn.in_transaction
    assert db.get_sprint(conn, protected) == protected_before
    assert db.get_sprint(conn, target) == target_before


@pytest.mark.parametrize('case,fragment', [
    ('kind-id', 'id'), ('kind-value', 'kind'), ('kind-revision', 'revision'),
    ('status-revision', 'revision'), ('close-actor', 'actor'), ('close-revision', 'revision'),
])
def test_existing_pure_validation_precedes_idle_refusal(conn, db_path, case, fragment):
    operation = case.split('-')[0]
    target, protected = prepare(conn, operation)
    before = db.get_sprint(conn, target)
    conn.execute('UPDATE sprint SET goal=? WHERE id=?', ('pending validation caller', protected))
    changes_before = conn.total_changes
    with pytest.raises(ValueError) as caught:
        if case == 'kind-id':
            db.set_sprint_kind(conn, 0, 'archive')
        elif case == 'kind-value':
            db.set_sprint_kind(conn, target, 'not-a-kind')
        elif case == 'kind-revision':
            db.set_sprint_kind(conn, target, 'archive', expected_revision='bad')
        elif case == 'status-revision':
            db.set_sprint_status(conn, target, 'active', expected_revision='bad')
        elif case == 'close-actor':
            db.close_sprint_with_boundary_event(conn, target, ' ')
        else:
            db.close_sprint_with_boundary_event(conn, target, 'oracle', expected_revision='bad')
    assert fragment in str(caught.value).lower()
    assert 'idle' not in str(caught.value).lower()
    assert conn.in_transaction and conn.total_changes == changes_before
    assert db.get_sprint(conn, protected)['goal'] == 'pending validation caller'
    assert db.get_sprint(conn, target) == before
    assert db.list_events(conn, target) == []
    with closing(db.get_connection(db_path)) as other:
        assert db.get_sprint(other, protected)['goal'] == 'durable original'
        assert db.get_sprint(other, target) == before
    conn.rollback()
    assert db.get_sprint(conn, protected)['goal'] == 'durable original'
