"""PostgreSQL: the served runtime connection survives database restarts.

A restart (or failover, or an idle-session kill) terminates the one shared
runtime connection.  These checks terminate real backends through a sibling
session as the same disposable role and assert what the served application
does next:

* the next request of any kind reconnects before it dispatches, so a loss
  never latches the runtime unavailable;
* only pure reads and durably keyed commands are replayed on the fresh
  connection; any other command returns ``postgres-runtime-unavailable`` with
  an unknown outcome and is never sent twice;
* ``served_runtime_ready`` asks the database, so readiness drops while the
  server is unreachable and comes back without an intervening request.
"""
from __future__ import annotations

import os
import socket
import uuid
from types import SimpleNamespace

import pytest

from sprintctl.application import ApplicationRejection, WorkApplication
from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    assert_disposable_connection,
    dict_row,
    pg,
    psycopg,
)
from tests.pg.test_run_evidence import (
    _append_args,
    _context as _run_context,
    _items,
    _ledger_rows,
    _new_run,
)

pytestmark = PG_MARKS


def _context(actor: str = "recovery-test", idempotency_key: str | None = None):
    return SimpleNamespace(
        identity=SimpleNamespace(
            actor=actor, environment="vuoro-dev", authorities=frozenset()
        ),
        request_id="request-1",
        basis_revision=None,
        catalog_revision="catalog-1",
        idempotency_requirement="required" if idempotency_key else "not-allowed",
        idempotency_key=idempotency_key,
    )


def _terminate_backend(target_connection) -> None:
    """Terminate one disposable backend through a sibling session."""
    target_pid = target_connection.info.backend_pid
    terminator = psycopg.connect(_PG_URL, row_factory=dict_row)
    try:
        row = terminator.execute(
            "SELECT pg_terminate_backend(%s) AS terminated", (target_pid,)
        ).fetchone()
        assert row["terminated"] is True
        terminator.commit()
    finally:
        terminator.close()


if psycopg is not None:

    class _LosesSessionAfterCommit(psycopg.Connection):
        """Commits, then loses its session before the caller sees the result.

        This is the in-doubt window a restart opens for any command: the
        server has made the effect durable, the client only sees an error.
        """

        lose_after_next_commit = False

        def commit(self) -> None:
            super().commit()
            if self.lose_after_next_commit:
                self.lose_after_next_commit = False
                _terminate_backend(self)
                self.execute("SELECT 1")  # raises: the session is gone


@pytest.fixture
def runtime(pg_test_scope):
    """A served application over its own connection and a counting factory."""
    repo_id = pg_test_scope("runtime-recovery")
    factory_calls: list[bool] = []

    def factory():
        factory_calls.append(True)
        return psycopg.connect(_PG_URL, row_factory=dict_row)

    connection = _LosesSessionAfterCommit.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    store = pg.PgStore(
        conn=connection,
        repo_id=repo_id,
        authority_repo_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{repo_id}")),
        connection_factory=factory,
    )
    pg.init_db(store)
    runtime = SimpleNamespace(
        store=store,
        app=WorkApplication.postgres(store),
        factory_calls=factory_calls,
        original=connection,
    )
    try:
        yield runtime
    finally:
        for conn in {id(c): c for c in (connection, store.conn) if c is not None}.values():
            conn.close()


def _count(store, sql: str, params: tuple) -> int:
    probe = psycopg.connect(_PG_URL, row_factory=dict_row)
    try:
        return int(probe.execute(sql, params).fetchone()["n"])
    finally:
        probe.close()


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_read_after_termination_reconnects_and_is_replayed(runtime):
    _terminate_backend(runtime.store.conn)

    result = runtime.app.invoke("work.read.sprints", {}, _context())
    public = runtime.app.invoke("work.public.list-v1", {}, _context())

    assert result == {"repo_id": runtime.store.repo_id, "sprints": []}
    assert public["items"] == []
    assert runtime.factory_calls == [True]
    assert runtime.store.conn is not runtime.original
    assert runtime.original.closed
    assert runtime.app.served_runtime_ready() is True


def test_a_read_whose_socket_died_without_a_sqlstate_is_replayed(runtime):
    """A socket lost under psycopg raises a bare OperationalError (no
    SQLSTATE); it is a connection loss because it left the connection dead."""
    dead = socket.socket(fileno=os.dup(runtime.store.conn.pgconn.socket))
    dead.shutdown(socket.SHUT_RDWR)
    dead.close()
    assert not runtime.store.conn.closed  # nothing notices until it is used

    result = runtime.app.invoke("work.read.sprints", {}, _context())

    assert result == {"repo_id": runtime.store.repo_id, "sprints": []}
    assert runtime.factory_calls == [True]
    assert runtime.original.closed


def test_a_mutation_after_termination_is_unknown_and_the_next_request_recovers(runtime):
    _terminate_backend(runtime.store.conn)

    with pytest.raises(ApplicationRejection) as rejected:
        runtime.app.invoke("work.sprint.create", {"name": "in doubt"}, _context())

    assert (rejected.value.code, rejected.value.http_status) == (
        "postgres-runtime-unavailable", 503,
    )
    assert "outcome is unknown" in rejected.value.message
    assert runtime.factory_calls == []  # not replayed, not even reconnected
    assert runtime.store.conn is None

    created = runtime.app.invoke("work.sprint.create", {"name": "resent"}, _context())

    assert created["sprint"]["name"] == "resent"
    assert runtime.factory_calls == [True]
    assert runtime.app.served_runtime_ready() is True
    names = [s["name"] for s in pg.list_sprints(runtime.store)]
    runtime.store.conn.rollback()
    assert names == ["resent"]


@pytest.mark.parametrize("how", ["closed", "terminated-and-noticed"])
def test_a_connection_lost_between_requests_is_replaced_before_dispatch(runtime, how):
    if how == "closed":
        runtime.store.conn.close()
    else:
        _terminate_backend(runtime.store.conn)
        with pytest.raises(psycopg.OperationalError):
            runtime.store.conn.execute("SELECT 1")
    assert runtime.store.conn.closed or runtime.store.conn.broken

    # A mutation that is never replayed still runs: nothing had been sent.
    created = runtime.app.invoke("work.sprint.create", {"name": how}, _context())

    assert created["sprint"]["name"] == how
    assert runtime.factory_calls == [True]
    assert runtime.app.served_runtime_ready() is True


def test_a_reservation_committed_before_the_loss_is_never_inserted_twice(runtime):
    store = runtime.store
    sprint_id = pg.create_sprint(store, "reserve", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "recovery")
    item_id = pg.create_work_item(store, sprint_id, track_id, "reserve me")
    store.conn.lose_after_next_commit = True

    with pytest.raises(ApplicationRejection) as rejected:
        runtime.app.invoke(
            "work.reservation.reserve",
            {"item_id": item_id, "actor": "recovery-test", "session_id": "session-1"},
            # Even a caller-supplied key does not make a reservation
            # replayable: reserve always INSERTs.
            _context(idempotency_key="reserve-1"),
        )

    assert (rejected.value.code, rejected.value.http_status) == (
        "postgres-runtime-unavailable", 503,
    )
    assert "outcome is unknown" in rejected.value.message
    assert runtime.factory_calls == []
    reservations = _count(
        store,
        "SELECT count(*) AS n FROM reservation WHERE repo_id = %s AND work_item_id = %s",
        (store.repo_id, item_id),
    )
    assert reservations == 1  # committed once, and not replayed into a second


def test_a_ledger_keyed_command_is_not_replayed_and_a_client_resend_is_one_effect(runtime):
    store = runtime.store
    run_id = _new_run(runtime.app, f"recovery-{uuid.uuid4().hex}")
    key = f"append-{uuid.uuid4().hex}"
    arguments = _append_args(run_id, key)
    store.conn.lose_after_next_commit = True

    with pytest.raises(ApplicationRejection) as rejected:
        runtime.app.invoke("work.evidence.append-v1", arguments, _run_context())

    assert (rejected.value.code, rejected.value.http_status) == (
        "postgres-runtime-unavailable", 503,
    )
    assert "same idempotency key" in rejected.value.message
    assert runtime.factory_calls == []
    probe =SimpleNamespace(conn=psycopg.connect(_PG_URL, row_factory=dict_row), repo_id=store.repo_id)
    try:
        assert _items(probe, run_id) == 1
        assert _ledger_rows(probe, "append_evidence", key) == 1

        resent = runtime.app.invoke("work.evidence.append-v1", arguments, _run_context())

        assert resent["item"]["item_id"] == arguments["item_id"]
        assert _items(probe, run_id) == 1
        assert _ledger_rows(probe, "append_evidence", key) == 1
    finally:
        probe.conn.close()


def test_readiness_is_false_while_the_server_is_unreachable_and_recovers(runtime):
    reachable = runtime.store.connection_factory
    unreachable_port = _unused_port()
    runtime.store.connection_factory = lambda: psycopg.connect(
        host="127.0.0.1", port=unreachable_port, dbname="unreachable",
        connect_timeout=2, row_factory=dict_row,
    )
    _terminate_backend(runtime.store.conn)

    assert runtime.app.served_runtime_ready() is False
    assert runtime.app.served_runtime_ready() is False
    with pytest.raises(ApplicationRejection) as rejected:
        runtime.app.invoke("work.sprint.create", {"name": "down"}, _context())
    assert (rejected.value.code, rejected.value.http_status) == (
        "postgres-runtime-unavailable", 503,
    )

    runtime.store.connection_factory = reachable

    assert runtime.app.served_runtime_ready() is True
    assert runtime.factory_calls == [True]
    assert runtime.app.invoke("work.read.sprints", {}, _context())["sprints"] == []


def test_readiness_leaves_the_session_as_it_found_it(runtime):
    from psycopg.pq import TransactionStatus

    conn = runtime.store.conn
    assert runtime.app.served_runtime_ready() is True
    assert conn.info.transaction_status == TransactionStatus.IDLE
    assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"
    conn.rollback()

    conn.execute("SELECT 1")  # an operation's transaction is open
    assert runtime.app.served_runtime_ready() is True
    assert conn.info.transaction_status == TransactionStatus.INTRANS
    conn.rollback()
    assert runtime.factory_calls == []


def test_a_second_termination_during_the_replay_is_unavailable_until_recovery(runtime):
    reachable = runtime.store.connection_factory

    def terminated_replacement():
        replacement = reachable()
        _terminate_backend(replacement)
        return replacement

    runtime.store.connection_factory = terminated_replacement
    _terminate_backend(runtime.store.conn)

    with pytest.raises(ApplicationRejection) as rejected:
        runtime.app.invoke("work.read.sprints", {}, _context())

    assert (rejected.value.code, rejected.value.http_status) == (
        "postgres-runtime-unavailable", 503,
    )
    assert runtime.store.conn is None
    assert runtime.app.served_runtime_ready() is False

    runtime.store.connection_factory = reachable
    recovered = runtime.app.invoke("work.read.sprints", {}, _context())

    assert recovered == {"repo_id": runtime.store.repo_id, "sprints": []}
    assert runtime.app.served_runtime_ready() is True


def test_get_connection_adds_liveness_defaults_the_dsn_does_not_set(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    store = pg.get_connection(_PG_URL)
    try:
        params = store.conn.info.get_parameters()
        assert params["connect_timeout"] == "5"
        assert params["keepalives_idle"] == "30"
        replacement = store.connection_factory()
        try:
            assert replacement.info.get_parameters()["keepalives_count"] == "3"
        finally:
            replacement.close()
    finally:
        store.conn.close()

    overridden = _PG_URL + ("&" if "?" in _PG_URL else "?") + "connect_timeout=9"
    store = pg.get_connection(overridden)
    try:
        assert store.conn.info.get_parameters()["connect_timeout"] == "9"
    finally:
        store.conn.close()
