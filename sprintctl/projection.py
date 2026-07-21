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
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4


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
    repo_id       TEXT NOT NULL,
    ingest_offset INTEGER NOT NULL CHECK (ingest_offset > 0),
    record_json   TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    PRIMARY KEY (repo_id, ingest_offset)
);

CREATE TABLE IF NOT EXISTS cached_ingest_watermark (
    repo_id       TEXT PRIMARY KEY,
    ingest_offset INTEGER NOT NULL CHECK (ingest_offset >= 0),
    advanced_at   TEXT
);

CREATE TABLE IF NOT EXISTS cached_authority_decision (
    repo_id                TEXT NOT NULL,
    decision_ingest_offset INTEGER NOT NULL CHECK (decision_ingest_offset > 0),
    decision_json          TEXT NOT NULL,
    decision_sha256        TEXT NOT NULL,
    received_at            TEXT NOT NULL,
    PRIMARY KEY (repo_id, decision_ingest_offset)
);

CREATE TABLE IF NOT EXISTS cached_authority_watermark (
    repo_id                 TEXT PRIMARY KEY,
    decision_ingest_offset INTEGER NOT NULL CHECK (decision_ingest_offset >= 0),
    advanced_at            TEXT
);

CREATE TABLE IF NOT EXISTS cached_projection_meta (
    singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    repo_id        TEXT NOT NULL
);
"""

PROJECTION_SCHEMA_VERSION = 2
"""On-disk schema version this build of ``projection.py`` writes and expects.

Guarded projection-backed readers (see ``sprintctl/cli.py``) compare this
against :func:`get_schema_version` and refuse to read a mismatched cache,
falling back to backend mode explicitly rather than misinterpreting an
incompatible on-disk layout.
"""

DEFAULT_STALE_AFTER_SECONDS = 300
"""Default staleness threshold used by :func:`assess_freshness`."""


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


def open_cached_projection(
    path: Path, *, repo_id: str | None = None
) -> sqlite3.Connection:
    """Open an isolated SQLite cached-ingestion projection at ``path``."""
    if repo_id is not None and (not isinstance(repo_id, str) or not repo_id.strip()):
        raise ValueError("projection repo_id must be a non-empty string")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    existing = _existing_schema_version(conn)
    if existing is None:
        init_cached_projection(conn, repo_id=repo_id or "local")
    elif existing == PROJECTION_SCHEMA_VERSION:
        bound = _bound_repo_id(conn)
        if repo_id is not None and bound != repo_id:
            conn.close()
            raise ValueError(
                f"cached projection belongs to repository {bound!r}, not {repo_id!r}"
            )
    return conn


def _existing_schema_version(conn: sqlite3.Connection) -> int | None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'cached_projection_meta'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute(
        "SELECT schema_version FROM cached_projection_meta WHERE singleton = 1"
    ).fetchone()
    return None if row is None else int(row[0])


def _bound_repo_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT repo_id FROM cached_projection_meta WHERE singleton = 1"
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError("cached ingestion projection has no repository binding")
    return str(row[0])


def _require_current_schema(conn: sqlite3.Connection) -> str:
    version = get_schema_version(conn)
    if version != PROJECTION_SCHEMA_VERSION:
        raise RuntimeError(
            f"cached projection schema {version} requires an atomic rebuild to "
            f"schema {PROJECTION_SCHEMA_VERSION}"
        )
    return _bound_repo_id(conn)


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def init_cached_projection(conn: sqlite3.Connection, *, repo_id: str = "local") -> None:
    """Create the cache schema and its persisted initial visible watermark."""
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO cached_ingest_watermark (repo_id, ingest_offset, advanced_at) "
        "VALUES (?, 0, NULL) ON CONFLICT(repo_id) DO NOTHING",
        (repo_id,),
    )
    conn.execute(
        "INSERT INTO cached_authority_watermark "
        "(repo_id, decision_ingest_offset, advanced_at) "
        "VALUES (?, 0, NULL) ON CONFLICT(repo_id) DO NOTHING",
        (repo_id,),
    )
    conn.execute(
        "INSERT INTO cached_projection_meta (singleton, schema_version, repo_id) "
        "VALUES (1, ?, ?) ON CONFLICT(singleton) DO NOTHING",
        (PROJECTION_SCHEMA_VERSION, repo_id),
    )
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version recorded when this cache file was created.

    A cache created before schema versioning existed still gets a row here
    the first time :func:`init_cached_projection` runs against it (every
    caller of :func:`open_cached_projection` runs it), so this only raises
    for a connection that never went through initialization at all.
    """
    row = conn.execute(
        "SELECT schema_version FROM cached_projection_meta WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("cached ingestion projection is not initialized")
    return int(row[0])


def get_watermark(conn: sqlite3.Connection) -> ProjectionWatermark:
    """Expose the remote watermark and its age through ``ProjectionWatermark``."""
    if not _table_has_column(conn, "cached_ingest_watermark", "repo_id"):
        row = conn.execute(
            "SELECT ingest_offset, advanced_at FROM cached_ingest_watermark "
            "WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("cached ingestion projection is not initialized")
        return ProjectionWatermark(ingest_offset=int(row[0]), advanced_at=row[1])
    repo_id = _bound_repo_id(conn)
    row = conn.execute(
        "SELECT ingest_offset, advanced_at FROM cached_ingest_watermark WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("cached ingestion projection is not initialized")
    return ProjectionWatermark(ingest_offset=int(row[0]), advanced_at=row[1])


def list_cached_records(conn: sqlite3.Connection) -> list[CachedIngestRecord]:
    """Read cached remote envelopes in their server-assigned ingestion order."""
    repo_id = _require_current_schema(conn)
    rows = conn.execute(
        "SELECT ingest_offset, record_json FROM cached_ingest_record "
        "WHERE repo_id = ? ORDER BY ingest_offset",
        (repo_id,),
    ).fetchall()
    return [CachedIngestRecord(ingest_offset=int(row[0]), record=json.loads(row[1])) for row in rows]


def get_authority_watermark(conn: sqlite3.Connection) -> ProjectionWatermark:
    if not _table_has_column(conn, "cached_authority_watermark", "repo_id"):
        row = conn.execute(
            "SELECT decision_ingest_offset, advanced_at "
            "FROM cached_authority_watermark WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("cached authority projection is not initialized")
        return ProjectionWatermark(ingest_offset=int(row[0]), advanced_at=row[1])
    repo_id = _bound_repo_id(conn)
    row = conn.execute(
        "SELECT decision_ingest_offset, advanced_at "
        "FROM cached_authority_watermark WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("cached authority projection is not initialized")
    return ProjectionWatermark(ingest_offset=int(row[0]), advanced_at=row[1])


def list_cached_authority_decisions(
    conn: sqlite3.Connection,
) -> list[CachedAuthorityDecision]:
    repo_id = _require_current_schema(conn)
    rows = conn.execute(
        "SELECT decision_ingest_offset, decision_json "
        "FROM cached_authority_decision WHERE repo_id = ? "
        "ORDER BY decision_ingest_offset",
        (repo_id,),
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
    repo_id = _require_current_schema(conn)
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
                "WHERE repo_id = ? AND decision_ingest_offset = ?",
                (repo_id, offset),
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
                "(repo_id, decision_ingest_offset, decision_json, decision_sha256, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (repo_id, offset, encoded, digest, received_at),
            )
            highest = max(highest, offset)
            applied = True
        if applied:
            conn.execute(
                "UPDATE cached_authority_watermark "
                "SET decision_ingest_offset = ?, advanced_at = ? WHERE repo_id = ?",
                (highest, advanced_at or _utc_now(), repo_id),
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
    repo_id = _require_current_schema(conn)
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
                "SELECT record_sha256 FROM cached_ingest_record "
                "WHERE repo_id = ? AND ingest_offset = ?",
                (repo_id, record.ingest_offset),
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
                "INSERT INTO cached_ingest_record "
                "(repo_id, ingest_offset, record_json, record_sha256, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (repo_id, record.ingest_offset, encoded, digest, received_at),
            )
            expected += 1
            applied = True
        if applied:
            new_offset = expected - 1
            conn.execute(
                "UPDATE cached_ingest_watermark SET ingest_offset = ?, advanced_at = ? "
                "WHERE repo_id = ?",
                (new_offset, advanced_at or _utc_now(), repo_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_watermark(conn)


def rebuild_cached_projection(
    path: Path,
    *,
    repo_id: str,
    captured_high_water: int,
    fetch_page: Callable[[int, int], list[CachedIngestRecord]],
    batch_size: int = 100,
) -> ProjectionWatermark:
    """Rebuild through a captured repository high-water, then replace atomically.

    The live cache is never truncated in place. A retained suffix or an empty
    page below the captured high-water fails the sibling build and leaves the
    original file untouched.
    """
    if (
        isinstance(captured_high_water, bool)
        or not isinstance(captured_high_water, int)
        or captured_high_water < 0
    ):
        raise ValueError("captured_high_water must be a non-negative integer")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    sibling = path.with_name(f".{path.name}.rebuild-{uuid4().hex}")
    cache: sqlite3.Connection | None = None
    try:
        cache = open_cached_projection(sibling, repo_id=repo_id)
        watermark = get_watermark(cache)
        while watermark.ingest_offset < captured_high_water:
            previous_offset = watermark.ingest_offset
            page = fetch_page(watermark.ingest_offset, batch_size)
            bounded = [
                record
                for record in page
                if record.ingest_offset <= captured_high_water
            ]
            if not bounded:
                raise ProjectionGapError(
                    watermark.ingest_offset + 1,
                    page[0].ingest_offset if page else captured_high_water,
                )
            watermark = apply_ingested_records(cache, bounded)
            if watermark.ingest_offset == previous_offset:
                raise ProjectionGapError(
                    previous_offset + 1,
                    bounded[0].ingest_offset,
                )
        if watermark.ingest_offset != captured_high_water:
            raise ProjectionGapError(captured_high_water, watermark.ingest_offset)
        cache.close()
        cache = None
        os.replace(sibling, path)
        return watermark
    except Exception:
        if cache is not None:
            cache.close()
        sibling.unlink(missing_ok=True)
        raise


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


@dataclass(frozen=True, slots=True)
class ProjectionFreshness:
    """A guarded reader's health assessment of one cached projection.

    ``healthy`` is ``True`` only when the schema version matches this build,
    the watermark has advanced at least once, and its age is within
    ``stale_after_seconds``.  Any other outcome carries an explicit
    ``fallback_reason`` a caller can disclose instead of silently serving a
    cache that may not reflect current remote state.
    """

    watermark: ProjectionWatermark
    schema_version: int
    age_seconds: float | None
    stale_after_seconds: int
    healthy: bool
    fallback_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "watermark_offset": self.watermark.ingest_offset,
            "watermark_advanced_at": self.watermark.advanced_at,
            "age_seconds": self.age_seconds,
            "schema_version": self.schema_version,
            "stale_after_seconds": self.stale_after_seconds,
            "healthy": self.healthy,
            "fallback_reason": self.fallback_reason,
        }


def assess_freshness(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> ProjectionFreshness:
    """Assess whether ``conn`` is safe for a guarded read-only consumer.

    Three independent conditions can make a cache unsafe to read as current:
    an incompatible on-disk schema version (``schema-upgrade-required``), a
    watermark that has never advanced past zero (``never-synchronized``), and
    a watermark whose age exceeds ``stale_after_seconds`` (``stale``).  Only
    one reason is reported, in that priority order, since a schema mismatch
    makes the other two moot and an unsynchronized cache has no meaningful
    age.
    """
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds < 1
    ):
        raise ValueError("stale_after_seconds must be a positive integer")

    watermark = get_watermark(conn)
    schema_version = get_schema_version(conn)
    age = watermark.age_seconds(now)

    if schema_version != PROJECTION_SCHEMA_VERSION:
        fallback_reason: str | None = "schema-upgrade-required"
    elif watermark.advanced_at is None:
        fallback_reason = "never-synchronized"
    elif age is not None and age > stale_after_seconds:
        fallback_reason = "stale"
    else:
        fallback_reason = None

    return ProjectionFreshness(
        watermark=watermark,
        schema_version=schema_version,
        age_seconds=age,
        stale_after_seconds=stale_after_seconds,
        healthy=fallback_reason is None,
        fallback_reason=fallback_reason,
    )
