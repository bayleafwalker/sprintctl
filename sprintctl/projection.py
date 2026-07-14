"""Local cache of remote ingestion records.

This is a deliberately narrow read-side component.  It records the immutable
remote-ingestion envelope and its server-assigned offset; it does not interpret
the envelope or mutate any sprintctl authority tables.  Callers can therefore
make the cache available while offline without mistaking it for authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


class ProjectionGapError(ValueError):
    """Remote records skipped the next offset required by this cache."""

    def __init__(self, expected: int, received: int):
        super().__init__(f"remote ingest offset gap: expected {expected}, received {received}")
        self.expected = expected
        self.received = received


class ProjectionConflictError(ValueError):
    """An already cached offset was replayed with different immutable data."""


@dataclass(frozen=True, slots=True)
class CachedAuthorityDecision:
    """One redacted remote decision retained for offline evidence."""

    decision_ingest_offset: int
    decision: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            isinstance(self.decision_ingest_offset, bool)
            or not isinstance(self.decision_ingest_offset, int)
            or self.decision_ingest_offset < 1
        ):
            raise ValueError("decision_ingest_offset must be a positive integer")
        object.__setattr__(self, "decision", _canonical_record(self.decision))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_ingest_offset": self.decision_ingest_offset,
            "decision": _canonical_record(self.decision),
        }


@dataclass(frozen=True, slots=True)
class CachedIngestRecord:
    """Serializable, immutable remote input for the local read-side cache."""

    ingest_offset: int
    record: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.ingest_offset, bool) or not isinstance(self.ingest_offset, int) or self.ingest_offset < 1:
            raise ValueError("ingest_offset must be a positive integer")
        canonical = _canonical_record(self.record)
        object.__setattr__(self, "record", canonical)

    def to_dict(self) -> dict[str, Any]:
        return {"ingest_offset": self.ingest_offset, "record": _canonical_record(self.record)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CachedIngestRecord":
        if not isinstance(value, Mapping) or set(value) != {"ingest_offset", "record"}:
            raise ValueError("cached ingest record must contain exactly ingest_offset and record")
        return cls(ingest_offset=value["ingest_offset"], record=value["record"])


@dataclass(frozen=True, slots=True)
class ProjectionWatermark:
    """The highest fully-applied remote offset and when it was advanced."""

    ingest_offset: int
    advanced_at: str | None

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Return cache staleness in seconds, or ``None`` before any remote apply."""
        if self.advanced_at is None:
            return None
        timestamp = datetime.fromisoformat(self.advanced_at.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        return max(0.0, (current - timestamp).total_seconds())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cached_ingest_record (
    ingest_offset INTEGER PRIMARY KEY CHECK (ingest_offset > 0),
    record_json   TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    received_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_ingest_watermark (
    singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
    ingest_offset INTEGER NOT NULL CHECK (ingest_offset >= 0),
    advanced_at   TEXT
);

CREATE TABLE IF NOT EXISTS cached_authority_decision (
    decision_ingest_offset INTEGER PRIMARY KEY CHECK (decision_ingest_offset > 0),
    decision_json          TEXT NOT NULL,
    decision_sha256        TEXT NOT NULL,
    received_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_authority_watermark (
    singleton              INTEGER PRIMARY KEY CHECK (singleton = 1),
    decision_ingest_offset INTEGER NOT NULL CHECK (decision_ingest_offset >= 0),
    advanced_at            TEXT
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("cached ingest record body must be a JSON object")
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("cached ingest record body must be JSON serializable") from exc
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):  # Defensive: dict input serializes to an object.
        raise ValueError("cached ingest record body must be a JSON object")
    return parsed


def _encoded_record(record: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    canonical = _canonical_record(record)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def open_cached_projection(path: Path) -> sqlite3.Connection:
    """Open an isolated SQLite cached-ingestion projection at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_cached_projection(conn)
    return conn


def init_cached_projection(conn: sqlite3.Connection) -> None:
    """Create the cache schema and its persisted initial visible watermark."""
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO cached_ingest_watermark (singleton, ingest_offset, advanced_at) "
        "VALUES (1, 0, NULL) ON CONFLICT(singleton) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO cached_authority_watermark "
        "(singleton, decision_ingest_offset, advanced_at) "
        "VALUES (1, 0, NULL) ON CONFLICT(singleton) DO NOTHING"
    )
    conn.commit()


def get_watermark(conn: sqlite3.Connection) -> ProjectionWatermark:
    """Expose the remote watermark and its age through ``ProjectionWatermark``."""
    row = conn.execute(
        "SELECT ingest_offset, advanced_at FROM cached_ingest_watermark WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("cached ingestion projection is not initialized")
    return ProjectionWatermark(ingest_offset=int(row[0]), advanced_at=row[1])


def list_cached_records(conn: sqlite3.Connection) -> list[CachedIngestRecord]:
    """Read cached remote envelopes in their server-assigned ingestion order."""
    rows = conn.execute(
        "SELECT ingest_offset, record_json FROM cached_ingest_record ORDER BY ingest_offset"
    ).fetchall()
    return [CachedIngestRecord(ingest_offset=int(row[0]), record=json.loads(row[1])) for row in rows]


def get_authority_watermark(conn: sqlite3.Connection) -> ProjectionWatermark:
    row = conn.execute(
        "SELECT decision_ingest_offset, advanced_at "
        "FROM cached_authority_watermark WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("cached authority projection is not initialized")
    return ProjectionWatermark(ingest_offset=int(row[0]), advanced_at=row[1])


def list_cached_authority_decisions(
    conn: sqlite3.Connection,
) -> list[CachedAuthorityDecision]:
    rows = conn.execute(
        "SELECT decision_ingest_offset, decision_json "
        "FROM cached_authority_decision ORDER BY decision_ingest_offset"
    ).fetchall()
    return [
        CachedAuthorityDecision(
            decision_ingest_offset=int(row[0]),
            decision=json.loads(row[1]),
        )
        for row in rows
    ]


def apply_authority_decisions(
    conn: sqlite3.Connection,
    decisions: list[CachedAuthorityDecision],
    *,
    advanced_at: str | None = None,
) -> ProjectionWatermark:
    """Atomically cache redacted decisions and advance their independent cursor.

    Decision offsets share the remote ingest sequence with observations and
    command requests, so they are strictly increasing but need not be
    contiguous in this decision-only projection.
    """
    if advanced_at is not None:
        _parse_timestamp(advanced_at)
    previous = 0
    prepared: list[tuple[CachedAuthorityDecision, str, str]] = []
    for decision in decisions:
        if decision.decision_ingest_offset <= previous:
            raise ValueError("authority decisions must be supplied in strictly increasing order")
        previous = decision.decision_ingest_offset
        canonical = _canonical_record(decision.decision)
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        prepared.append(
            (decision, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        watermark = get_authority_watermark(conn)
        highest = watermark.ingest_offset
        applied = False
        for decision, encoded, digest in prepared:
            offset = decision.decision_ingest_offset
            row = conn.execute(
                "SELECT decision_sha256 FROM cached_authority_decision "
                "WHERE decision_ingest_offset = ?",
                (offset,),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ProjectionConflictError(
                        f"authority decision offset {offset} was replayed with different data"
                    )
                continue
            if offset <= watermark.ingest_offset:
                raise ProjectionConflictError(
                    "uncached authority decision exists below the visible watermark"
                )
            received_at = advanced_at or _utc_now()
            conn.execute(
                "INSERT INTO cached_authority_decision "
                "(decision_ingest_offset, decision_json, decision_sha256, received_at) "
                "VALUES (?, ?, ?, ?)",
                (offset, encoded, digest, received_at),
            )
            highest = max(highest, offset)
            applied = True
        if applied:
            conn.execute(
                "UPDATE cached_authority_watermark "
                "SET decision_ingest_offset = ?, advanced_at = ? WHERE singleton = 1",
                (highest, advanced_at or _utc_now()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_authority_watermark(conn)


def apply_ingested_records(
    conn: sqlite3.Connection,
    records: list[CachedIngestRecord],
    *,
    advanced_at: str | None = None,
) -> ProjectionWatermark:
    """Atomically cache one contiguous remote range and advance its watermark.

    An exact replay of an already cached offset is a no-op.  A changed replay,
    out-of-order batch, or missing next offset is rejected.  Inserts and the
    watermark update use one SQLite transaction, so an application failure
    rolls back both the data and the claimed progress cursor.
    """
    if advanced_at is not None:
        _parse_timestamp(advanced_at)
    prepared = [(record, *_encoded_record(record.record)) for record in records]
    previous = 0
    for record, *_ in prepared:
        if record.ingest_offset <= previous:
            raise ValueError("ingest records must be supplied in strictly increasing offset order")
        previous = record.ingest_offset

    conn.execute("BEGIN IMMEDIATE")
    try:
        watermark = get_watermark(conn)
        expected = watermark.ingest_offset + 1
        applied = False
        for record, _canonical, encoded, digest in prepared:
            row = conn.execute(
                "SELECT record_sha256 FROM cached_ingest_record WHERE ingest_offset = ?",
                (record.ingest_offset,),
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ProjectionConflictError(
                        f"remote ingest offset {record.ingest_offset} was replayed with different data"
                    )
                if record.ingest_offset > watermark.ingest_offset:
                    raise ProjectionConflictError("cached record exists beyond the visible watermark")
                continue
            if record.ingest_offset != expected:
                raise ProjectionGapError(expected, record.ingest_offset)
            received_at = advanced_at or _utc_now()
            conn.execute(
                "INSERT INTO cached_ingest_record (ingest_offset, record_json, record_sha256, received_at) "
                "VALUES (?, ?, ?, ?)",
                (record.ingest_offset, encoded, digest, received_at),
            )
            expected += 1
            applied = True
        if applied:
            new_offset = expected - 1
            conn.execute(
                "UPDATE cached_ingest_watermark SET ingest_offset = ?, advanced_at = ? WHERE singleton = 1",
                (new_offset, advanced_at or _utc_now()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_watermark(conn)


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("advanced_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("advanced_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("advanced_at must include a timezone")
    return parsed
