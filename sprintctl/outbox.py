"""Durable producer-side outbox storage.

The outbox is an additive local producer log for observations and authority
command requests.  It never accepts remote decisions and never projects a
command as effective authority state.
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
OBSERVATION = contracts.RecordClass.OBSERVATION.value
AUTHORITY_COMMAND = contracts.RecordClass.AUTHORITY_COMMAND.value
_PRODUCER_RECORD_CLASSES = {OBSERVATION, AUTHORITY_COMMAND}


class OutboxIdempotencyError(ValueError):
    """An event ID was reused for different immutable producer record data."""


@dataclass(frozen=True)
class OutboxRecord:
    """One immutable, producer-authored observation or authority command."""

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


_STREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_stream (
    singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),
    origin_stream_id TEXT NOT NULL UNIQUE,
    next_origin_seq  INTEGER NOT NULL CHECK (next_origin_seq > 0),
    created_at       TEXT NOT NULL
)
"""

_RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_record (
    origin_stream_id   TEXT NOT NULL REFERENCES outbox_stream(origin_stream_id),
    origin_seq         INTEGER NOT NULL CHECK (origin_seq > 0),
    event_id           TEXT NOT NULL UNIQUE,
    schema_version     INTEGER NOT NULL CHECK (schema_version > 0),
    record_class       TEXT NOT NULL CHECK (record_class IN ('observation', 'authority-command')),
    event_type         TEXT NOT NULL,
    actor              TEXT NOT NULL,
    runtime_session_id TEXT,
    occurred_at        TEXT NOT NULL,
    basis_revision     TEXT,
    correlation_id     TEXT,
    causation_id       TEXT,
    payload            TEXT NOT NULL,
    payload_sha256     TEXT NOT NULL,
    record_sha256      TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (origin_stream_id, origin_seq)
)
"""

_UPDATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS outbox_record_is_immutable_update
BEFORE UPDATE ON outbox_record
BEGIN
    SELECT RAISE(ABORT, 'outbox records are immutable');
END
"""

_DELETE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS outbox_record_is_immutable_delete
BEFORE DELETE ON outbox_record
BEGIN
    SELECT RAISE(ABORT, 'outbox records are immutable');
END
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("outbox payload must be a mapping")
    copied = dict(payload)
    try:
        encoded = json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("outbox payload must be JSON serializable") from exc
    return json.loads(encoded), encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _record_fingerprint(
    *,
    schema_version: int,
    record_class: str,
    event_type: str,
    actor: str,
    runtime_session_id: str | None,
    occurred_at: str,
    basis_revision: str | None,
    correlation_id: str | None,
    causation_id: str | None,
    payload_sha256: str,
) -> str:
    canonical = json.dumps(
        {
            "schema_version": schema_version,
            "record_class": record_class,
            "event_type": event_type,
            "actor": actor,
            "runtime_session_id": runtime_session_id,
            "occurred_at": occurred_at,
            "basis_revision": basis_revision,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "payload_sha256": payload_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"outbox {field} must be a non-empty string")
    return value


def open_outbox(path: Path) -> sqlite3.Connection:
    """Open and initialize a local producer outbox at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    init_outbox(conn)
    return conn


def init_outbox(conn: sqlite3.Connection) -> None:
    """Initialize or safely migrate the version-1 producer outbox schema."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute(_STREAM_SCHEMA)
    conn.commit()
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox_record'"
    ).fetchone()
    if table is None:
        conn.execute(_RECORD_SCHEMA)
        conn.execute(_UPDATE_TRIGGER)
        conn.execute(_DELETE_TRIGGER)
        conn.commit()
        return

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(outbox_record)").fetchall()
    }
    table_sql = str(table["sql"] or "")
    if "record_sha256" not in columns or "authority-command" not in table_sql:
        _migrate_observation_only_schema(conn)
    else:
        conn.execute(_UPDATE_TRIGGER)
        conn.execute(_DELETE_TRIGGER)
        conn.commit()


def _migrate_observation_only_schema(conn: sqlite3.Connection) -> None:
    """Rebuild an existing observation-only v1 table without changing records."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck after taking the writer reservation: another opener may have
        # completed the migration while this connection waited for the lock.
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(outbox_record)").fetchall()
        }
        table = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox_record'"
        ).fetchone()
        if (
            "record_sha256" in columns
            and table is not None
            and "authority-command" in str(table["sql"] or "")
        ):
            conn.commit()
            return
        rows = conn.execute("SELECT * FROM outbox_record ORDER BY origin_seq").fetchall()
        conn.execute("DROP TRIGGER IF EXISTS outbox_record_is_immutable_update")
        conn.execute("DROP TRIGGER IF EXISTS outbox_record_is_immutable_delete")
        conn.execute("ALTER TABLE outbox_record RENAME TO outbox_record_v1_observation")
        conn.execute(_RECORD_SCHEMA)
        for row in rows:
            record_sha256 = _record_fingerprint(
                schema_version=int(row["schema_version"]),
                record_class=row["record_class"],
                event_type=row["event_type"],
                actor=row["actor"],
                runtime_session_id=row["runtime_session_id"],
                occurred_at=row["occurred_at"],
                basis_revision=row["basis_revision"],
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
                payload_sha256=row["payload_sha256"],
            )
            conn.execute(
                """
                INSERT INTO outbox_record (
                    origin_stream_id, origin_seq, event_id, schema_version,
                    record_class, event_type, actor, runtime_session_id,
                    occurred_at, basis_revision, correlation_id, causation_id,
                    payload, payload_sha256, record_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["origin_stream_id"],
                    row["origin_seq"],
                    row["event_id"],
                    row["schema_version"],
                    row["record_class"],
                    row["event_type"],
                    row["actor"],
                    row["runtime_session_id"],
                    row["occurred_at"],
                    row["basis_revision"],
                    row["correlation_id"],
                    row["causation_id"],
                    row["payload"],
                    row["payload_sha256"],
                    record_sha256,
                    row["created_at"],
                ),
            )
        conn.execute("DROP TABLE outbox_record_v1_observation")
        conn.execute(_UPDATE_TRIGGER)
        conn.execute(_DELETE_TRIGGER)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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
    row = conn.execute(
        "SELECT origin_stream_id FROM outbox_stream WHERE singleton = 1"
    ).fetchone()
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
        "INSERT INTO outbox_stream "
        "(singleton, origin_stream_id, next_origin_seq, created_at) VALUES (1, ?, 1, ?)",
        (origin_stream_id, created_at),
    )
    return origin_stream_id, 1


def _append_fields(
    conn: sqlite3.Connection,
    *,
    record_class: str,
    event_type: str,
    actor: str,
    payload: Mapping[str, Any],
    runtime_session_id: str | None,
    basis_revision: str | None,
    correlation_id: str | None,
    causation_id: str | None,
    occurred_at: str | None,
    event_id: str | None,
) -> OutboxRecord:
    event_type = _required_text(event_type, "event_type")
    classified = contracts.record_class_for_type(event_type).value
    if classified != record_class or record_class not in _PRODUCER_RECORD_CLASSES:
        raise ValueError(
            f"outbox event_type {event_type!r} is {classified}; cannot append as {record_class}"
        )
    actor = _required_text(actor, "actor")
    payload_value, payload_json, payload_sha256 = _canonical_payload(payload)
    event_id = _required_text(event_id or str(uuid4()), "event_id")
    created_at = _utc_now()

    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            "SELECT * FROM outbox_record WHERE event_id = ?", (event_id,)
        ).fetchone()
        effective_occurred_at = (
            existing_row["occurred_at"]
            if existing_row is not None and occurred_at is None
            else occurred_at or _utc_now()
        )
        record_sha256 = _record_fingerprint(
            schema_version=OUTBOX_SCHEMA_VERSION,
            record_class=record_class,
            event_type=event_type,
            actor=actor,
            runtime_session_id=runtime_session_id,
            occurred_at=effective_occurred_at,
            basis_revision=basis_revision,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload_sha256=payload_sha256,
        )
        if existing_row is not None:
            if existing_row["record_sha256"] != record_sha256:
                raise OutboxIdempotencyError(
                    f"event_id {event_id!r} already identifies a different observation "
                    "or authority-command producer record"
                )
            record = _record_from_row(existing_row)
            conn.commit()
            return record

        origin_stream_id, origin_seq = _get_or_create_stream(conn, created_at)
        conn.execute(
            """
            INSERT INTO outbox_record (
                origin_stream_id, origin_seq, event_id, schema_version,
                record_class, event_type, actor, runtime_session_id, occurred_at,
                basis_revision, correlation_id, causation_id, payload,
                payload_sha256, record_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                origin_stream_id,
                origin_seq,
                event_id,
                OUTBOX_SCHEMA_VERSION,
                record_class,
                event_type,
                actor,
                runtime_session_id,
                effective_occurred_at,
                basis_revision,
                correlation_id,
                causation_id,
                payload_json,
                payload_sha256,
                record_sha256,
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
        record_class=record_class,
        event_type=event_type,
        actor=actor,
        runtime_session_id=runtime_session_id,
        occurred_at=effective_occurred_at,
        basis_revision=basis_revision,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload_value,
        payload_sha256=payload_sha256,
        created_at=created_at,
    )


def append_record(
    conn: sqlite3.Connection,
    record: contracts.RecordEnvelope,
    *,
    runtime_session_id: str | None = None,
) -> OutboxRecord:
    """Append a canonical producer envelope, rejecting remote decisions."""
    if not isinstance(record, contracts.RecordEnvelope):
        raise TypeError("record must be a sprintctl RecordEnvelope")
    if record.record_class is contracts.RecordClass.REMOTE_DECISION:
        raise ValueError("producer outbox cannot append remote-decision records")
    return _append_fields(
        conn,
        record_class=record.record_class.value,
        event_type=record.record_type,
        actor=record.actor,
        payload=record.to_dict(),
        runtime_session_id=runtime_session_id,
        basis_revision=record.basis_revision,
        correlation_id=record.correlation_id,
        causation_id=record.causation_id,
        occurred_at=record.authored_at,
        event_id=record.event_id,
    )


def append_authority_command(
    conn: sqlite3.Connection,
    command: contracts.AuthorityCommand,
    *,
    runtime_session_id: str | None = None,
) -> OutboxRecord:
    """Append one validated authority command request without applying it."""
    if not isinstance(command, contracts.AuthorityCommand):
        raise TypeError("command must be a sprintctl AuthorityCommand")
    return append_record(conn, command, runtime_session_id=runtime_session_id)


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
    """Preserve the established observation append API."""
    record_class = contracts.record_class_for_type(event_type)
    if record_class is not contracts.RecordClass.OBSERVATION:
        raise ValueError(
            f"outbox event_type {event_type!r} is {record_class.value}; "
            "append_observation accepts observations only"
        )
    return _append_fields(
        conn,
        record_class=OBSERVATION,
        event_type=event_type,
        actor=actor,
        payload=payload,
        runtime_session_id=runtime_session_id,
        basis_revision=basis_revision,
        correlation_id=correlation_id,
        causation_id=causation_id,
        occurred_at=occurred_at,
        event_id=event_id,
    )


__all__ = [
    "AUTHORITY_COMMAND",
    "OBSERVATION",
    "OUTBOX_SCHEMA_VERSION",
    "OutboxIdempotencyError",
    "OutboxRecord",
    "append_authority_command",
    "append_observation",
    "append_record",
    "get_origin_stream_id",
    "init_outbox",
    "list_records",
    "open_outbox",
]
