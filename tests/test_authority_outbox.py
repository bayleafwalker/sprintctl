from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
from uuid import uuid4

import pytest

from sprintctl import contracts, outbox


def _envelope(record_type: str, *, event_id=None, actor="agent-a"):
    common = {
        "event_id": event_id or uuid4(),
        "record_type": record_type,
        "schema_version": "sprintctl-record/v1",
        "actor": actor,
        "authored_at": "2026-07-14T12:00:00Z",
        "basis_revision": "item:revision:7",
    }
    if record_type == "work.completed":
        return contracts.Observation(
            **common,
            refs={"work_item_id": str(uuid4())},
            payload={"summary": "work complete"},
        )
    return contracts.AuthorityCommand(
        **common,
        refs={
            "repo_id": str(uuid4()),
            "aggregate_type": "item",
            "aggregate_uuid": str(uuid4()),
        },
        payload={"to_status": "done"},
    )


def test_observation_and_command_share_one_ordered_immutable_stream(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer.db")
    observation = outbox.append_record(conn, _envelope("work.completed"))
    command = outbox.append_authority_command(conn, _envelope("item.done"))

    assert [record.origin_seq for record in outbox.list_records(conn)] == [1, 2]
    assert [record.record_class for record in outbox.list_records(conn)] == [
        outbox.OBSERVATION,
        outbox.AUTHORITY_COMMAND,
    ]
    assert observation.origin_stream_id == command.origin_stream_id

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE outbox_record SET actor = 'other' WHERE origin_seq = 2")
    conn.rollback()


def test_authority_command_retry_is_fully_idempotent_and_changed_metadata_conflicts(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer.db")
    event_id = uuid4()
    command = _envelope("item.done", event_id=event_id)
    first = outbox.append_authority_command(conn, command, runtime_session_id="session-a")
    retried = outbox.append_record(conn, command, runtime_session_id="session-a")
    assert retried == first

    changed = replace(command, actor="agent-b")
    with pytest.raises(outbox.OutboxIdempotencyError, match="different observation"):
        outbox.append_authority_command(conn, changed, runtime_session_id="session-a")
    with pytest.raises(outbox.OutboxIdempotencyError, match="different observation"):
        outbox.append_authority_command(conn, command, runtime_session_id="session-b")
    assert [record.origin_seq for record in outbox.list_records(conn)] == [1]


def test_append_record_rejects_remote_decisions(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer.db")
    decision = contracts.RemoteDecision(
        event_id=uuid4(),
        record_type="claim.released",
        schema_version="sprintctl-record/v1",
        actor="remote-authority",
        authored_at="2026-07-14T12:00:00Z",
        refs={"claim_id": 7},
        payload={"status": "released"},
    )
    with pytest.raises(ValueError, match="cannot append remote-decision"):
        outbox.append_record(conn, decision)
    assert outbox.list_records(conn) == []


class _StubConnection:
    def __init__(self, wal_error: Exception | None = None) -> None:
        self.row_factory = None
        self.wal_error = wal_error
        self.closed = False
        self.statements: list[str] = []

    def execute(self, statement: str):
        self.statements.append(statement)
        if statement == "PRAGMA journal_mode = WAL" and self.wal_error is not None:
            raise self.wal_error
        return self

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "error_code",
    [
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_BUSY_RECOVERY,
        sqlite3.SQLITE_BUSY_SNAPSHOT,
        sqlite3.SQLITE_BUSY_TIMEOUT,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_LOCKED_SHAREDCACHE,
        sqlite3.SQLITE_LOCKED_VTAB,
    ],
)
def test_busy_or_locked_classification_accepts_primary_and_extended_codes(
    error_code,
):
    retryable = sqlite3.OperationalError("database is busy or locked")
    retryable.sqlite_errorcode = error_code

    assert outbox._is_busy_or_locked(retryable) is True


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_open_retries_busy_or_locked_wal_and_closes_failed_connection(
    tmp_path, monkeypatch, error_code
):
    retryable = sqlite3.OperationalError("database is locked")
    retryable.sqlite_errorcode = error_code
    first = _StubConnection(retryable)
    second = _StubConnection()
    connections = iter((first, second))
    delays = []
    monkeypatch.setattr(
        outbox.sqlite3, "connect", lambda *_args, **_kwargs: next(connections)
    )
    monkeypatch.setattr(outbox, "_initialize_schema_after_wal", lambda _conn: None)
    monkeypatch.setattr(outbox.time, "sleep", delays.append)

    connected = outbox.open_outbox(tmp_path / "producer.db")

    assert connected is second
    assert first.closed is True
    assert second.closed is False
    assert delays == [outbox._WAL_BUSY_RETRY_DELAYS_SECONDS[0]]


def test_open_stops_after_bounded_busy_wal_retries(tmp_path, monkeypatch):
    failures = []
    connections = []
    for _ in range(len(outbox._WAL_BUSY_RETRY_DELAYS_SECONDS) + 1):
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        failures.append(busy)
        connections.append(_StubConnection(busy))
    pending_connections = iter(connections)
    delays = []
    monkeypatch.setattr(
        outbox.sqlite3, "connect", lambda *_args, **_kwargs: next(pending_connections)
    )
    monkeypatch.setattr(outbox.time, "sleep", delays.append)

    with pytest.raises(sqlite3.OperationalError) as caught:
        outbox.open_outbox(tmp_path / "producer.db")

    assert caught.value is failures[-1]
    assert all(connection.closed for connection in connections)
    assert delays == list(outbox._WAL_BUSY_RETRY_DELAYS_SECONDS)


def test_open_propagates_non_busy_wal_error_without_retry(tmp_path, monkeypatch):
    non_busy = sqlite3.OperationalError("not a busy failure")
    non_busy.sqlite_errorcode = sqlite3.SQLITE_READONLY
    failed = _StubConnection(non_busy)
    connect_calls = 0

    def failing_connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return failed

    monkeypatch.setattr(outbox.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError) as caught:
        outbox.open_outbox(tmp_path / "producer.db")

    assert caught.value is non_busy
    assert connect_calls == 1
    assert failed.closed is True


def test_open_closes_schema_failure_without_retry(tmp_path, monkeypatch):
    failed = _StubConnection()
    connect_calls = 0
    schema_failure = RuntimeError("schema migration failed")

    def connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return failed

    def fail_schema(_conn):
        raise schema_failure

    monkeypatch.setattr(outbox.sqlite3, "connect", connect)
    monkeypatch.setattr(outbox, "_initialize_schema_after_wal", fail_schema)

    with pytest.raises(RuntimeError) as caught:
        outbox.open_outbox(tmp_path / "producer.db")

    assert caught.value is schema_failure
    assert connect_calls == 1
    assert failed.closed is True


def _create_legacy_outbox(path):
    conn = sqlite3.connect(path)
    payload_json = json.dumps({"legacy": True}, sort_keys=True, separators=(",", ":"))
    payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
    conn.executescript(
        """
        CREATE TABLE outbox_stream (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            origin_stream_id TEXT NOT NULL UNIQUE,
            next_origin_seq INTEGER NOT NULL CHECK (next_origin_seq > 0),
            created_at TEXT NOT NULL
        );
        CREATE TABLE outbox_record (
            origin_stream_id TEXT NOT NULL REFERENCES outbox_stream(origin_stream_id),
            origin_seq INTEGER NOT NULL CHECK (origin_seq > 0),
            event_id TEXT NOT NULL UNIQUE,
            schema_version INTEGER NOT NULL CHECK (schema_version > 0),
            record_class TEXT NOT NULL CHECK (record_class = 'observation'),
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            runtime_session_id TEXT,
            occurred_at TEXT NOT NULL,
            basis_revision TEXT,
            correlation_id TEXT,
            causation_id TEXT,
            payload TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (origin_stream_id, origin_seq)
        );
        CREATE TRIGGER outbox_record_is_immutable_update BEFORE UPDATE ON outbox_record
        BEGIN SELECT RAISE(ABORT, 'outbox records are immutable'); END;
        CREATE TRIGGER outbox_record_is_immutable_delete BEFORE DELETE ON outbox_record
        BEGIN SELECT RAISE(ABORT, 'outbox records are immutable'); END;
        """
    )
    conn.execute(
        "INSERT INTO outbox_stream VALUES (1, 'legacy-stream', 2, '2026-07-14T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO outbox_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-stream", 1, "legacy-event", 1, "observation", "work.completed",
            "legacy-agent", None, "2026-07-14T10:00:00Z", None, None, None,
            payload_json, payload_sha256, "2026-07-14T10:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


def test_existing_observation_only_v1_database_migrates_without_data_loss(tmp_path):
    path = tmp_path / "producer.db"
    _create_legacy_outbox(path)

    migrated = outbox.open_outbox(path)
    legacy = outbox.list_records(migrated)
    assert len(legacy) == 1
    assert legacy[0].event_id == "legacy-event"
    assert "record_sha256" in {
        row["name"] for row in migrated.execute("PRAGMA table_info(outbox_record)")
    }

    command = outbox.append_authority_command(migrated, _envelope("item.done"))
    assert command.origin_stream_id == "legacy-stream"
    assert command.origin_seq == 2


@pytest.mark.parametrize("_history", range(16))
def test_concurrent_openers_serialize_migration_and_append_without_loss(
    tmp_path, _history
):
    path = tmp_path / "producer.db"
    _create_legacy_outbox(path)
    barrier = threading.Barrier(2)
    errors = []

    def append(index):
        try:
            barrier.wait()
            conn = outbox.open_outbox(path)
            try:
                outbox.append_observation(
                    conn,
                    event_type="work.completed",
                    actor=f"writer-{index}",
                    payload={"index": index},
                    event_id=f"concurrent-{index}",
                    occurred_at="2026-07-14T12:00:00Z",
                )
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    migrated = outbox.open_outbox(path)
    try:
        records = outbox.list_records(migrated)
        event_ids = [record.event_id for record in records]
        assert event_ids[0] == "legacy-event"
        assert set(event_ids[1:]) == {"concurrent-1", "concurrent-2"}
        assert [record.origin_seq for record in records] == [1, 2, 3]
    finally:
        migrated.close()
