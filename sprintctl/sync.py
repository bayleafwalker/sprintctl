"""Idempotent transport between a producer outbox and the remote ingest ledger.

This is deliberately an additive P1 transport path.  It uploads only records
already authored in the local producer outbox, then materializes the remote
ledger into the isolated cached projection.  It does not redirect the existing
authority-changing CLI paths or treat cached records as authoritative state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Callable, Mapping

from . import authority, backend, outbox, pg, projection


@dataclass(frozen=True, slots=True)
class RepositorySyncPaths:
    repo_root: Path
    outbox_path: Path
    projection_path: Path


def repository_sync_paths(*, cwd: Path | None = None) -> RepositorySyncPaths:
    """Fixed normal-sync storage below the resolved repository root."""
    root, _repo_id, _marker = backend.resolve_repo_identity(cwd or Path.cwd())
    if root is None:
        raise ValueError("cannot resolve a repository for synchronization")
    state = root / ".sprintctl"
    return RepositorySyncPaths(
        repo_root=root,
        outbox_path=state / "sync-outbox.db",
        projection_path=state / "sync-projection.db",
    )


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Evidence from one producer-to-remote-to-cache synchronization pass."""

    uploaded: tuple[pg.IngestResult, ...]
    applied_count: int
    watermark: projection.ProjectionWatermark
    command_decisions: tuple[authority.AuthorityDecision, ...] = ()
    pending_command_event_ids: tuple[str, ...] = ()
    decision_applied_count: int = 0
    decision_watermark: projection.ProjectionWatermark | None = None


def _validate_batch_size(batch_size: int) -> int:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    return batch_size


def _cached_record(value: pg.IngestedRecord) -> projection.CachedIngestRecord:
    """Convert a remote result to its serializable, non-authoritative cache form."""
    record = value.record
    return projection.CachedIngestRecord(
        ingest_offset=value.ingest_offset,
        record={
            "origin_stream_id": record.origin_stream_id,
            "origin_seq": record.origin_seq,
            "event_id": record.event_id,
            "schema_version": record.schema_version,
            "record_class": record.record_class,
            "event_type": record.event_type,
            "actor": record.actor,
            "runtime_session_id": record.runtime_session_id,
            "occurred_at": record.occurred_at,
            "basis_revision": record.basis_revision,
            "correlation_id": record.correlation_id,
            "causation_id": record.causation_id,
            "payload": record.payload,
            "payload_sha256": record.payload_sha256,
            "created_at": record.created_at,
        },
    )


def rebuild_ingest_projection(
    remote_store: pg.PgStore,
    projection_path: Path,
    *,
    batch_size: int = 100,
) -> projection.ProjectionWatermark:
    """Rebuild one repository cache to a captured server high-water."""
    batch_size = _validate_batch_size(batch_size)
    captured = pg.get_ingest_high_water(remote_store)
    return projection.rebuild_cached_projection(
        projection_path,
        repo_id=remote_store.repo_id,
        captured_high_water=captured,
        batch_size=batch_size,
        fetch_page=lambda after, limit: [
            _cached_record(record)
            for record in pg.list_ingested_records(
                remote_store, after_offset=after, limit=limit
            )
        ],
    )


def synchronize_outbox(
    outbox_conn: sqlite3.Connection,
    remote_store: pg.PgStore,
    projection_conn: sqlite3.Connection,
    *,
    batch_size: int = 100,
    credential_resolver: Callable[[outbox.OutboxRecord], Mapping[str, str] | None]
    | None = None,
    apply_ingest_projection: bool = True,
) -> SyncResult:
    """Upload producer records and atomically advance isolated cache cursors.

    Re-submitting every durable producer record is intentional: remote admission
    deduplicates on the producer stream tuple and returns original offsets after
    a lost response. Commands without transient credential material remain
    pending and never mutate authority. If projection application fails, a
    later call repeats safe admission and resumes from unchanged watermarks.
    """
    batch_size = _validate_batch_size(batch_size)
    records = outbox.list_records(outbox_conn)
    uploaded: list[pg.IngestResult] = []
    decisions: list[authority.AuthorityDecision] = []
    pending: list[str] = []
    observations: list[outbox.OutboxRecord] = []

    def flush_observations() -> None:
        for start in range(0, len(observations), batch_size):
            uploaded.extend(
                pg.ingest_records(remote_store, observations[start : start + batch_size])
            )
        observations.clear()

    for index, record in enumerate(records):
        if record.record_class == outbox.OBSERVATION:
            observations.append(record)
            continue
        flush_observations()
        if record.record_class != outbox.AUTHORITY_COMMAND:
            raise ValueError("producer outbox contains a non-producer record class")
        credentials = credential_resolver(record) if credential_resolver is not None else None
        if credentials is None:
            pending.extend(
                blocked.event_id
                for blocked in records[index:]
                if blocked.record_class == outbox.AUTHORITY_COMMAND
            )
            break
        decisions.append(
            authority.arbitrate_command(remote_store, record, credentials=credentials)
        )
    flush_observations()

    watermark = projection.get_watermark(projection_conn)
    applied_count = 0
    if apply_ingest_projection:
        while True:
            remote_records = pg.list_ingested_records(
                remote_store, after_offset=watermark.ingest_offset, limit=batch_size
            )
            if not remote_records:
                break
            watermark = projection.apply_ingested_records(
                projection_conn, [_cached_record(record) for record in remote_records]
            )
            applied_count += len(remote_records)

    decision_watermark = projection.get_authority_watermark(projection_conn)
    decision_applied_count = 0
    while True:
        remote_decisions = authority.list_authority_decisions(
            remote_store,
            after_offset=decision_watermark.ingest_offset,
            limit=batch_size,
        )
        if not remote_decisions:
            break
        cached = [
            projection.CachedAuthorityDecision(
                decision_ingest_offset=decision.decision_ingest_offset,
                decision=decision.to_dict(),
            )
            for decision in remote_decisions
        ]
        decision_watermark = projection.apply_authority_decisions(
            projection_conn,
            cached,
        )
        decision_applied_count += len(cached)

    return SyncResult(
        tuple(uploaded),
        applied_count,
        watermark,
        command_decisions=tuple(decisions),
        pending_command_event_ids=tuple(pending),
        decision_applied_count=decision_applied_count,
        decision_watermark=decision_watermark,
    )


def synchronize_repository(
    remote_store: pg.PgStore,
    *,
    outbox_path: Path,
    projection_path: Path,
    batch_size: int = 100,
    credential_resolver: Callable[[outbox.OutboxRecord], Mapping[str, str] | None]
    | None = None,
) -> SyncResult:
    """Run the durable local upload and projection catch-up for one repository.

    This is the normal synchronization orchestration.  Callers supply only
    fixed repository-owned paths; no rollout or pilot state participates.
    """
    batch_size = _validate_batch_size(batch_size)
    producer = outbox.open_outbox(outbox_path)
    try:
        if projection_path.exists():
            existing = projection.open_cached_projection(projection_path)
            try:
                needs_rebuild = (
                    projection.get_schema_version(existing)
                    != projection.PROJECTION_SCHEMA_VERSION
                )
            finally:
                existing.close()
            if needs_rebuild:
                rebuild_ingest_projection(
                    remote_store, projection_path, batch_size=batch_size
                )
        cache = projection.open_cached_projection(
            projection_path, repo_id=remote_store.repo_id
        )
        try:
            return synchronize_outbox(
                producer,
                remote_store,
                cache,
                batch_size=batch_size,
                credential_resolver=credential_resolver,
            )
        finally:
            cache.close()
    finally:
        producer.close()
