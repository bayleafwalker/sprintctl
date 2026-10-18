"""Sprint kind is a scoped state-basis CAS, independent of status revision."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from sprintctl import db, sprintcore
from sprintctl.application import ApplicationRejection
from sprintctl.cli import cli
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.test_work_application import _application, _context


def exercise_kind_changes(backend, store):
    sid = backend.create_sprint(store, 'Kinds', status='planned')
    original = backend.get_sprint(store, sid)
    basis = sprintcore.sprint_kind_revision(original)
    first = backend.set_sprint_kind(store, sid, 'backlog', expected_revision=basis)
    assert first['kind'] == 'backlog'
    assert first['status'] == original['status']
    assert backend.sprint_status_revision(first) == backend.sprint_status_revision(original)
    with pytest.raises(db.StatusConflict):
        backend.set_sprint_kind(store, sid, 'archive', expected_revision=basis)
    with pytest.raises(db.StatusConflict):
        backend.set_sprint_kind(store, sid, 'backlog', expected_revision=basis)
    same = backend.set_sprint_kind(store, sid, 'backlog', expected_revision=first['kind_revision'])
    assert same == first
    archive = backend.set_sprint_kind(store, sid, 'archive', expected_revision=first['kind_revision'])
    assert backend.get_sprint(store, sid)['kind'] == 'archive'
    # All classifications remain reversible; lifecycle status does not change.
    last = backend.set_sprint_kind(store, sid, 'active_sprint', expected_revision=archive['kind_revision'])
    assert last['status'] == 'planned'
    # The existing direct API remains usable without the optional new CAS.
    backend.set_sprint_kind(store, sid, 'archive')
    assert backend.get_sprint(store, sid)['kind'] == 'archive'


def test_sqlite_kind_state_basis(conn):
    exercise_kind_changes(db, conn)


@pytest.mark.parametrize('revision', [None, '', 'sprint:bad@kind:archive@status:planned', 1])
def test_kind_schema_refuses_malformed_revision_before_mutation(conn, revision):
    sid = db.create_sprint(conn, 'No mutation')
    if revision is None:  # Legacy backend callers deliberately permit None.
        revision = db.sprint_status_revision(db.get_sprint(conn, sid))
    with pytest.raises(ValueError, match='kind revision'):
        db.set_sprint_kind(conn, sid, 'archive', expected_revision=revision)
    assert db.get_sprint(conn, sid)['kind'] == 'active_sprint'


def test_kind_cas_includes_status_and_identity(conn):
    sid = db.create_sprint(conn, 'Current')
    other = db.create_sprint(conn, 'Other')
    original = db.get_sprint(conn, sid)
    with pytest.raises(db.StatusConflict):
        db.set_sprint_kind(conn, other, 'archive', expected_revision=sprintcore.sprint_kind_revision(original))
    db.set_sprint_status(conn, sid, 'active', expected_revision=db.sprint_status_revision(original))
    with pytest.raises(db.StatusConflict):
        db.set_sprint_kind(conn, sid, 'archive', expected_revision=sprintcore.sprint_kind_revision(original))


def _authorized():
    context = _context()
    context.identity.authorities = frozenset({'work:sprint'})
    return context


def test_served_kind_requires_own_authority_and_cas(conn):
    app = _application(store=conn, backend=db)
    sid = db.create_sprint(conn, 'Served kind')
    basis = sprintcore.sprint_kind_revision(db.get_sprint(conn, sid))
    args = {'sprint_id': sid, 'kind': 'archive', 'expected_revision': basis}
    with pytest.raises(ApplicationRejection) as denied:
        app.invoke('work.sprint.kind', args, _context())
    assert denied.value.code == 'authority-required'
    with pytest.raises(ApplicationRejection) as missing:
        app.invoke('work.sprint.kind', {'sprint_id': sid, 'kind': 'archive'}, _authorized())
    assert missing.value.code == 'invalid-arguments'
    result = app.invoke('work.sprint.kind', args, _authorized())
    assert result['repo_id'] == 'test-repo'
    assert result['sprint']['kind'] == 'archive'
    assert app.invoke('work.read.sprint', {'sprint_id': sid}, _context())['sprint']['kind_revision'] == result['sprint']['kind_revision']
    with pytest.raises(ApplicationRejection) as stale:
        app.invoke('work.sprint.kind', args, _authorized())
    assert stale.value.code == 'sprint-kind-conflict'
    assert stale.value.http_status == 409


def test_kind_contract_has_sprint_authority_and_explicit_basis():
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == 'work.sprint.kind')
    assert contract.required_authority == 'work:sprint'
    assert set(contract.input_schema['required']) == {'sprint_id', 'kind', 'expected_revision'}
    assert contract.input_schema['additionalProperties'] is False


def test_sqlite_competing_kind_changes(db_path):
    conn = db.get_connection(db_path)
    db.init_db(conn)
    sid = db.create_sprint(conn, 'Concurrent classification')
    basis = sprintcore.sprint_kind_revision(db.get_sprint(conn, sid))
    conn.close()
    barrier = threading.Barrier(2)
    history = []
    def change(kind):
        actor = db.get_connection(db_path)
        try:
            barrier.wait(timeout=10)
            started = time.monotonic_ns()
            try:
                db.set_sprint_kind(actor, sid, kind, expected_revision=basis)
                outcome = 'accepted'
            except db.StatusConflict:
                outcome = 'conflict'
            history.append({'action': kind, 'started_ns': started, 'completed_ns': time.monotonic_ns(), 'outcome': outcome})
            return outcome
        finally:
            actor.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ['archive', 'backlog']))
    assert sorted(outcomes) == ['accepted', 'conflict']
    print('KIND_HISTORY ' + json.dumps({'backend': 'sqlite', 'case': 'competing-kinds', 'history': history}, sort_keys=True))


def test_cli_kind_returns_current_revision(runner, conn):
    sid = db.create_sprint(conn, 'CLI classification')
    result = runner.invoke(cli, ['sprint', 'kind', '--id', str(sid), '--kind', 'archive', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['kind_revision'] == sprintcore.sprint_kind_revision(db.get_sprint(conn, sid))


def exercise_archive_activation_race(backend, store_factory, close, sid, original):
    """One classification/activation commits; the competing old basis refuses."""
    barrier = threading.Barrier(2)
    history = []
    def change(action):
        store = store_factory()
        try:
            barrier.wait(timeout=10)
            started = time.monotonic_ns()
            try:
                if action == 'archive':
                    backend.set_sprint_kind(store, sid, 'archive', expected_revision=sprintcore.sprint_kind_revision(original))
                else:
                    backend.set_sprint_status(store, sid, 'active', expected_revision=backend.sprint_status_revision(original))
                outcome = 'accepted'
            except (db.StatusConflict, db.InvalidTransition):
                outcome = 'refused'
            history.append({'action': action, 'started_ns': started, 'completed_ns': time.monotonic_ns(), 'outcome': outcome})
            return outcome
        finally:
            close(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ['archive', 'activate']))
    assert sorted(results) == ['accepted', 'refused']
    print('KIND_HISTORY ' + json.dumps({'backend': backend.__name__, 'case': 'archive-versus-activate', 'history': history}, sort_keys=True))


def test_sqlite_archive_activation_race(db_path):
    conn = db.get_connection(db_path)
    db.init_db(conn)
    sid = db.create_sprint(conn, 'Activation race')
    original = db.get_sprint(conn, sid)
    conn.close()
    exercise_archive_activation_race(db, lambda: db.get_connection(db_path), lambda c: c.close(), sid, original)


def test_sqlite_archive_cannot_activate_but_active_can_archive(conn):
    sid = db.create_sprint(conn, 'Archive activation')
    db.set_sprint_kind(conn, sid, 'archive')
    with pytest.raises(db.InvalidTransition, match='archive'):
        db.set_sprint_status(conn, sid, 'active')
    other = db.create_sprint(conn, 'Already active', status='active')
    db.set_sprint_kind(conn, other, 'archive')
    assert db.get_sprint(conn, other)['status'] == 'active'


@pytest.mark.skipif(__import__('sys').version_info < (3, 12), reason='served mode requires Python3.12+')
@pytest.mark.parametrize('explicit', [False, True])
def test_served_kind_cli_never_opens_local_store(runner, tmp_path, monkeypatch, explicit):
    from tests.test_served_routes import _configure_served_repo
    import sprintctl.cli as module
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(module, '_get_store', lambda _: pytest.fail('opened local store'))
    row = {'id': 1, 'aggregate_uuid': '00000000-0000-4000-8000-000000000001', 'kind': 'backlog', 'status': 'planned'}
    basis = sprintcore.sprint_kind_revision(row)
    calls = []
    def read(*args, **kwargs):
        calls.append('read')
        assert kwargs['repo_id'] == 'served-guard-test'
        return {'sprint': row}
    def change(*args, **kwargs):
        calls.append('write')
        assert kwargs['expected_revision'] == basis
        assert kwargs['repo_id'] == 'served-guard-test'
        return {'sprint': {**row, 'kind': 'archive', 'kind_revision': sprintcore.sprint_kind_revision({**row, 'kind': 'archive'})}}
    monkeypatch.setattr(module._served, 'read_sprint', read)
    monkeypatch.setattr(module._served, 'sprint_kind', change)
    argv = ['sprint', 'kind', '--id', '1', '--kind', 'archive', '--json']
    if explicit:
        argv.extend(['--expected-revision', basis])
    result = runner.invoke(cli, argv)
    assert result.exit_code == 0, result.output
    assert calls == (['write'] if explicit else ['read', 'write'])
    assert json.loads(result.output)['kind'] == 'archive'
    assert json.loads(result.output)['resolved_context']['repo_id'] == 'served-guard-test'


@pytest.mark.parametrize('item_id', [True, 0, 1 << 63, 10 ** 100])
def test_kind_id_range_refuses_without_mutation(conn, item_id):
    import jsonschema
    sid = db.create_sprint(conn, 'Range refusal')
    basis = sprintcore.sprint_kind_revision(db.get_sprint(conn, sid))
    app = _application(store=conn, backend=db)
    arguments = {'sprint_id': item_id, 'kind': 'archive', 'expected_revision': basis}
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == 'work.sprint.kind')
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(arguments, contract.input_schema)
    with pytest.raises(ApplicationRejection) as refused:
        app.invoke('work.sprint.kind', arguments, _authorized())
    assert refused.value.code == 'invalid-arguments'
    with pytest.raises(ValueError):
        db.set_sprint_kind(conn, item_id, 'archive', expected_revision=basis)
    assert db.get_sprint(conn, sid)['kind'] == 'active_sprint'


@pytest.mark.parametrize('explicit', [False, True])
def test_cli_oversized_kind_id_refuses_before_basis_io(runner, monkeypatch, explicit):
    import sprintctl.cli as module
    monkeypatch.setattr(module, '_get_store', lambda _: pytest.fail('opened store'))
    monkeypatch.setattr(module, '_served_config_or_none', lambda _: pytest.fail('resolved served config'))
    arguments = ['sprint', 'kind', '--id', str(1 << 63), '--kind', 'archive', '--json']
    if explicit:
        arguments += ['--expected-revision', 'sprint:00000000-0000-4000-8000-000000000001@kind:backlog@status:planned']
    result = runner.invoke(cli, arguments)
    assert result.exit_code == 1
    assert 'positive signed 64-bit integer' in result.output
