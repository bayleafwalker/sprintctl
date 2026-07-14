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


def test_concurrent_openers_serialize_migration_and_append_without_loss(tmp_path):
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
        event_ids = [record.event_id for record in outbox.list_records(migrated)]
        assert event_ids[0] == "legacy-event"
        assert set(event_ids[1:]) == {"concurrent-1", "concurrent-2"}
    finally:
        migrated.close()
