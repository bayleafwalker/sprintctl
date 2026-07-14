from __future__ import annotations

import sqlite3

import pytest

from sprintctl import outbox, pg, projection, sync


class _FakeRemote:
    """In-memory stand-in that retains the ingest ledger's retry behavior."""

    def __init__(self):
        self.records: list[pg.IngestedRecord] = []

    def ingest(self, _store, records):
        results: list[pg.IngestResult] = []
        for record in records:
            for existing in self.records:
                if (existing.record.origin_stream_id, existing.record.origin_seq) == (
                    record.origin_stream_id,
                    record.origin_seq,
                ):
                    if existing.record != record:
                        raise pg.IngestConflictError("origin tuple changed")
                    results.append(pg.IngestResult(existing.record, existing.ingest_offset, duplicate=True))
                    break
            else:
                ingested = pg.IngestedRecord(record, len(self.records) + 1)
                self.records.append(ingested)
                results.append(pg.IngestResult(record, ingested.ingest_offset, duplicate=False))
        return results

    def list(self, _store, *, after_offset=0, limit=None):
        result = [record for record in self.records if record.ingest_offset > after_offset]
        return result if limit is None else result[:limit]


def _append(conn, event_id: str, index: int):
    return outbox.append_observation(
        conn,
        event_type="work.completed",
        actor="producer",
        payload={"index": index},
        event_id=event_id,
        occurred_at="2026-07-14T12:00:00Z",
    )


@pytest.fixture
def transport(tmp_path, monkeypatch):
    producer = outbox.open_outbox(tmp_path / "producer.db")
    cache = projection.open_cached_projection(tmp_path / "projection.db")
    remote = _FakeRemote()
    monkeypatch.setattr(sync.pg, "ingest_records", remote.ingest)
    monkeypatch.setattr(sync.pg, "list_ingested_records", remote.list)
    yield producer, cache, remote
    producer.close()
    cache.close()


def test_sync_uploads_observations_and_materializes_remote_cursor(transport):
    producer, cache, remote = transport
    _append(producer, "sync-1", 1)
    _append(producer, "sync-2", 2)

    result = sync.synchronize_outbox(producer, object(), cache, batch_size=1)

    assert [outcome.duplicate for outcome in result.uploaded] == [False, False]
    assert result.applied_count == 2
    assert result.watermark.ingest_offset == 2
    assert [record.ingest_offset for record in projection.list_cached_records(cache)] == [1, 2]
    assert len(outbox.list_records(producer)) == 2
    assert len(remote.records) == 2


def test_sync_recovers_after_lost_response_or_projection_apply_failure(transport):
    producer, cache, remote = transport
    _append(producer, "sync-1", 1)
    cache.execute(
        """
        CREATE TRIGGER reject_projection_apply
        BEFORE INSERT ON cached_ingest_record
        BEGIN
            SELECT RAISE(ABORT, 'injected projection failure');
        END;
        """
    )
    cache.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected projection failure"):
        sync.synchronize_outbox(producer, object(), cache)
    assert len(remote.records) == 1
    assert projection.get_watermark(cache).ingest_offset == 0

    cache.execute("DROP TRIGGER reject_projection_apply")
    cache.commit()
    retried = sync.synchronize_outbox(producer, object(), cache)

    assert [outcome.duplicate for outcome in retried.uploaded] == [True]
    assert retried.applied_count == 1
    assert retried.watermark.ingest_offset == 1


@pytest.mark.parametrize("batch_size", [0, -1, True])
def test_sync_rejects_invalid_batch_sizes(transport, batch_size):
    producer, cache, _remote = transport
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        sync.synchronize_outbox(producer, object(), cache, batch_size=batch_size)
