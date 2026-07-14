"""Idempotent transport between a producer outbox and the remote ingest ledger.

This is deliberately an additive P1 transport path.  It uploads only records
already authored in the local producer outbox, then materializes the remote
ledger into the isolated cached projection.  It does not redirect the existing
authority-changing CLI paths or treat cached records as authoritative state.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from . import outbox, pg, projection


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Evidence from one producer-to-remote-to-cache synchronization pass."""

    uploaded: tuple[pg.IngestResult, ...]
    applied_count: int
    watermark: projection.ProjectionWatermark


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


def synchronize_outbox(
    outbox_conn: sqlite3.Connection,
    remote_store: pg.PgStore,
    projection_conn: sqlite3.Connection,
    *,
    batch_size: int = 100,
) -> SyncResult:
    """Upload local observations and atomically advance the local cache cursor.

    Re-submitting every durable producer record is intentional: remote admission
    deduplicates on the producer stream tuple and returns original offsets after
    a lost response.  If projection application fails, a later call repeats the
    safe upload and resumes from the unchanged local watermark.
    """
    batch_size = _validate_batch_size(batch_size)
    records = outbox.list_records(outbox_conn)
    uploaded: list[pg.IngestResult] = []
    for start in range(0, len(records), batch_size):
        uploaded.extend(pg.ingest_records(remote_store, records[start : start + batch_size]))

    watermark = projection.get_watermark(projection_conn)
    applied_count = 0
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

    return SyncResult(tuple(uploaded), applied_count, watermark)
