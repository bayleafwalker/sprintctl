"""Durable producer-side outbox storage.

This module is deliberately separate from the current sprint SQLite and
PostgreSQL backends.  It is an additive, local producer log: callers append
observations here before a future synchronizer transports them to a remote
authority.  It neither projects authority commands nor stores remote-origin
events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from . import contracts


OUTBOX_SCHEMA_VERSION = 1
OBSERVATION = "observation"


class OutboxIdempotencyError(ValueError):
    """An event ID was reused for a different producer observation."""


@dataclass(frozen=True)
class OutboxRecord:
    """One immutable, producer-authored observation record."""

    origin_stream_id: str
    origin_seq: int
    event_id: str
    schema_version: int
    record_class: str
    event_type: str
    actor: str
    runtime_session_id: str | None
    occurred_at: str
    basis_revision: str | None
    correlation_id: str | None
    causation_id: str | None
    payload: dict[str, Any]
    payload_sha256: str
    created_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_stream (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    origin_stream_id TEXT NOT NULL UNIQUE,
    next_origin_seq INTEGER NOT NULL CHECK (next_origin_seq > 0),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_record (
    origin_stream_id  TEXT NOT NULL REFERENCES outbox_stream(origin_stream_id),
    origin_seq        INTEGER NOT NULL CHECK (origin_seq > 0),
    event_id          TEXT NOT NULL UNIQUE,
    schema_version    INTEGER NOT NULL CHECK (schema_version > 0),
    record_class      TEXT NOT NULL CHECK (record_class = 'observation'),
    event_type        TEXT NOT NULL,
    actor             TEXT NOT NULL,
    runtime_session_id TEXT,
    occurred_at       TEXT NOT NULL,
    basis_revision    TEXT,
    correlation_id    TEXT,
    causation_id      TEXT,
    payload           TEXT NOT NULL,
    payload_sha256    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (origin_stream_id, origin_seq)
);

CREATE TRIGGER IF NOT EXISTS outbox_record_is_immutable_update
BEFORE UPDATE ON outbox_record
BEGIN
    SELECT RAISE(ABORT, 'outbox records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS outbox_record_is_immutable_delete
BEFORE DELETE ON outbox_record
BEGIN
    SELECT RAISE(ABORT, 'outbox records are immutable');
END;
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("outbox payload must be a mapping")
    copied = dict(payload)
    try:
        encoded = json.dumps(copied, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("outbox payload must be JSON serializable") from exc
    return copied, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"outbox {field} must be a non-empty string")
    return value


def open_outbox(path: Path) -> sqlite3.Connection:
    """Open and initialize a local producer outbox at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_outbox(conn)
    return conn


def init_outbox(conn: sqlite3.Connection) -> None:
    """Create the additive outbox schema and its append-only guardrails."""
    # A committed producer record is only useful if a restart can recover it.
    # FULL makes SQLite fsync the WAL at commit boundaries rather than relying
    # on the weaker NORMAL setting commonly used for cache-like databases.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(_SCHEMA)
    conn.commit()


def _record_from_row(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(
        origin_stream_id=row["origin_stream_id"],
        origin_seq=row["origin_seq"],
        event_id=row["event_id"],
        schema_version=row["schema_version"],
        record_class=row["record_class"],
        event_type=row["event_type"],
        actor=row["actor"],
        runtime_session_id=row["runtime_session_id"],
        occurred_at=row["occurred_at"],
        basis_revision=row["basis_revision"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        payload=json.loads(row["payload"]),
        payload_sha256=row["payload_sha256"],
        created_at=row["created_at"],
    )


def get_origin_stream_id(conn: sqlite3.Connection) -> str | None:
    """Return the durable producer stream ID, if this outbox has appended."""
    row = conn.execute("SELECT origin_stream_id FROM outbox_stream WHERE singleton = 1").fetchone()
    return row["origin_stream_id"] if row is not None else None


def list_records(conn: sqlite3.Connection) -> list[OutboxRecord]:
    """Read immutable producer records in their stream order."""
    rows = conn.execute(
        "SELECT * FROM outbox_record ORDER BY origin_stream_id, origin_seq"
    ).fetchall()
    return [_record_from_row(row) for row in rows]


def _get_or_create_stream(conn: sqlite3.Connection, created_at: str) -> tuple[str, int]:
    row = conn.execute(
        "SELECT origin_stream_id, next_origin_seq FROM outbox_stream WHERE singleton = 1"
    ).fetchone()
    if row is not None:
        return row["origin_stream_id"], row["next_origin_seq"]

    origin_stream_id = str(uuid4())
    conn.execute(
        "INSERT INTO outbox_stream (singleton, origin_stream_id, next_origin_seq, created_at) VALUES (1, ?, 1, ?)",
        (origin_stream_id, created_at),
    )
    return origin_stream_id, 1


def append_observation(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    payload: Mapping[str, Any],
    runtime_session_id: str | None = None,
    basis_revision: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    occurred_at: str | None = None,
    event_id: str | None = None,
) -> OutboxRecord:
    """Append one observation with an atomically allocated stream sequence.

    ``BEGIN IMMEDIATE`` makes reading ``next_origin_seq``, inserting the
    record, and advancing the counter one SQLite transaction.  An interrupted
    transaction leaves neither a record nor a consumed sequence number.
    Reusing an event ID with the same event type, actor, and canonical payload
    returns the prior record so a caller can safely recover from a lost local
    response.
    """
    event_type = _required_text(event_type, "event_type")
    record_class = contracts.record_class_for_type(event_type)
    if record_class is not contracts.RecordClass.OBSERVATION:
        raise ValueError(
            f"outbox event_type {event_type!r} is {record_class.value}; "
            "only classified observations may be appended"
        )
    actor = _required_text(actor, "actor")
    payload_value, payload_json, payload_sha256 = _canonical_payload(payload)
    event_id = event_id or str(uuid4())
    event_id = _required_text(event_id, "event_id")
    occurred_at = occurred_at or _utc_now()
    created_at = _utc_now()

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM outbox_record WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            record = _record_from_row(existing)
            if (
                record.event_type != event_type
                or record.actor != actor
                or record.payload_sha256 != payload_sha256
            ):
                raise OutboxIdempotencyError(
                    f"event_id {event_id!r} already identifies a different observation"
                )
            conn.commit()
            return record

        origin_stream_id, origin_seq = _get_or_create_stream(conn, created_at)
        conn.execute(
            """
            INSERT INTO outbox_record (
                origin_stream_id, origin_seq, event_id, schema_version,
                record_class, event_type, actor, runtime_session_id, occurred_at,
                basis_revision, correlation_id, causation_id, payload,
                payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                origin_stream_id,
                origin_seq,
                event_id,
                OUTBOX_SCHEMA_VERSION,
                OBSERVATION,
                event_type,
                actor,
                runtime_session_id,
                occurred_at,
                basis_revision,
                correlation_id,
                causation_id,
                payload_json,
                payload_sha256,
                created_at,
            ),
        )
        conn.execute(
            "UPDATE outbox_stream SET next_origin_seq = ? WHERE singleton = 1",
            (origin_seq + 1,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return OutboxRecord(
        origin_stream_id=origin_stream_id,
        origin_seq=origin_seq,
        event_id=event_id,
        schema_version=OUTBOX_SCHEMA_VERSION,
        record_class=OBSERVATION,
        event_type=event_type,
        actor=actor,
        runtime_session_id=runtime_session_id,
        occurred_at=occurred_at,
        basis_revision=basis_revision,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload_value,
        payload_sha256=payload_sha256,
        created_at=created_at,
    )
