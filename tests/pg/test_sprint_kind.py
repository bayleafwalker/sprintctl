"""Scoped native PostgreSQL kind changes and independent-connection races."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
import json

import pytest

from sprintctl import db, pg, sprintcore
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS, _PG_URL, psycopg, dict_row
from tests.test_sprint_kind import exercise_kind_changes, exercise_archive_activation_race, _authorized
from tests.test_work_application import _application

pytestmark = PG_MARKS


def test_pg_kind_state_basis(store):
    exercise_kind_changes(pg, store)


def test_pg_kind_scope_and_authority(store):
    sid = pg.create_sprint(store, 'Native served classification')
    basis = sprintcore.sprint_kind_revision(pg.get_sprint(store, sid))
    app = _application(repo_id=store.repo_id, store=store, backend=pg)
    args = {'sprint_id': sid, 'kind': 'archive', 'expected_revision': basis}
    result = app.invoke('work.sprint.kind', args, _authorized())
    assert result['sprint']['kind'] == 'archive'
    assert result['repo_id'] == store.repo_id
    other = replace(store, repo_id=store.repo_id + '-not-authorized')
    with pytest.raises(ValueError, match='not found'):
        pg.set_sprint_kind(other, sid, 'backlog', expected_revision=result['sprint']['kind_revision'])
    assert pg.get_sprint(store, sid)['kind'] == 'archive'
    with pytest.raises(db.InvalidTransition, match='archive'):
        pg.activate_sprint(store, sid, 'operator')
    with pytest.raises(db.InvalidTransition, match='archive'):
        pg.set_sprint_status(store, sid, 'active')


def test_pg_competing_kind_changes(store):
    sid = pg.create_sprint(store, 'Native kind race')
    basis = sprintcore.sprint_kind_revision(pg.get_sprint(store, sid))
    barrier = threading.Barrier(2)
    history = []
    def change(kind):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        actor = replace(store, conn=conn)
        try:
            barrier.wait(timeout=10)
            started = time.monotonic_ns()
            try:
                pg.set_sprint_kind(actor, sid, kind, expected_revision=basis)
                outcome = 'accepted'
            except db.StatusConflict:
                outcome = 'conflict'
            history.append({'action': kind, 'started_ns': started, 'completed_ns': time.monotonic_ns(), 'outcome': outcome})
            return outcome
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ['archive', 'backlog']))
    assert sorted(outcomes) == ['accepted', 'conflict']
    print('KIND_HISTORY ' + json.dumps({'backend': 'postgresql', 'case': 'competing-kinds', 'history': history}, sort_keys=True))


def test_pg_archive_activation_race(store):
    sid = pg.create_sprint(store, 'Native activation race')
    original = pg.get_sprint(store, sid)
    store.conn.commit()
    def factory():
        return replace(store, conn=psycopg.connect(_PG_URL, row_factory=dict_row))
    exercise_archive_activation_race(pg, factory, lambda s: s.conn.close(), sid, original)


def test_pg_served_lifecycle_cannot_activate_archive(store, tmp_path):
    from sprintctl import authority, outbox
    from tests.pg._shared import _append_authority_command
    sid = pg.create_sprint(store, 'Archived served lifecycle', kind='archive')
    original = pg.get_sprint(store, sid)
    producer = outbox.open_outbox(tmp_path / 'archive-activation.db')
    try:
        command = _append_authority_command(producer, store, record_type='sprint.activate',
            aggregate_type='sprint', aggregate_uuid=original['aggregate_uuid'],
            basis_revision=authority.sprint_revision(original), payload={})
        decision = authority.arbitrate_command(store, command)
        assert decision.accepted is False
        assert 'archive' in decision.reason_detail
        assert pg.get_sprint(store, sid)['status'] == 'planned'
    finally:
        producer.close()


def test_pg_kind_range_refuses_without_mutation(store):
    sid = pg.create_sprint(store, 'Native range refusal')
    basis = sprintcore.sprint_kind_revision(pg.get_sprint(store, sid))
    for invalid in [True, 0, 1 << 63, 10 ** 100]:
        with pytest.raises(ValueError, match='signed 64-bit'):
            pg.set_sprint_kind(store, invalid, 'archive', expected_revision=basis)
    assert pg.get_sprint(store, sid)['kind'] == 'active_sprint'
