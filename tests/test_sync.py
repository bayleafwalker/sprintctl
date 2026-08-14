from __future__ import annotations

import sqlite3
import uuid

import pytest

from sprintctl import authority, contracts, outbox, pg, projection, sync


def test_repository_sync_paths_are_fixed_under_the_repository(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    paths = sync.repository_sync_paths(cwd=tmp_path)

    assert paths.repo_root == tmp_path
    assert paths.outbox_path == tmp_path / ".sprintctl" / "sync-outbox.db"
    assert paths.projection_path == tmp_path / ".sprintctl" / "sync-projection.db"


def test_normal_sync_migrates_legacy_pilot_databases_without_removing_them(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    legacy_dir = tmp_path / ".sprintctl"
    legacy_dir.mkdir()
    legacy_outbox = legacy_dir / "shadow-pilot-outbox.db"
    legacy_projection = legacy_dir / "shadow-pilot-projection.db"
    producer = outbox.open_outbox(legacy_outbox)
    try:
        _append(producer, "legacy-observation", 1)
    finally:
        producer.close()
    projection.open_cached_projection(legacy_projection).close()

    paths = sync.repository_sync_paths(cwd=tmp_path)
    copied = sync.migrate_legacy_sync_state(paths)

    assert copied == (paths.outbox_path, paths.projection_path)
    assert legacy_outbox.exists()
    assert legacy_projection.exists()
    migrated = outbox.open_outbox(paths.outbox_path)
    try:
        assert [record.event_id for record in outbox.list_records(migrated)] == ["legacy-observation"]
    finally:
        migrated.close()
    assert sync.migrate_legacy_sync_state(paths) == ()


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
    monkeypatch.setattr(sync.authority, "list_authority_decisions", lambda *_args, **_kwargs: [])
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


def test_authority_command_remains_pending_without_proof_then_caches_decision(
    transport, monkeypatch
):
    producer, cache, _remote = transport
    token = "transient-claim-proof"
    ref = authority.credential_ref(token)
    command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()),
        record_type="claim.acquire",
        schema_version="1",
        actor="sync-test",
        authored_at="2026-07-14T12:00:00Z",
        refs={
            "repo_id": str(uuid.uuid4()),
            "aggregate_type": "item",
            "aggregate_uuid": str(uuid.uuid4()),
        },
        payload={
            "agent": "worker-a",
            "claim_type": "execute",
            "exclusive": True,
            "ttl_seconds": 300,
            "credential_ref": ref,
            "metadata": {},
        },
        basis_revision="item:pending",
    )
    durable = outbox.append_authority_command(producer, command)
    later_observation = _append(producer, "after-pending-command", 2)
    calls = []
    decision = authority.AuthorityDecision(
        request_event_id=durable.event_id,
        decision_event_id=str(uuid.uuid4()),
        decision_ingest_offset=7,
        decision_type="claim.granted",
        outcome="accepted",
        reason_code=None,
        reason_detail=None,
        effect={"claim_id": 42, "actor": "worker-a"},
    )

    def arbitrate(_store, record, *, credentials):
        calls.append((record.event_id, credentials))
        return decision

    monkeypatch.setattr(sync.authority, "arbitrate_command", arbitrate)
    monkeypatch.setattr(
        sync.authority,
        "list_authority_decisions",
        lambda _store, *, after_offset=0, limit=None: [decision]
        if after_offset < decision.decision_ingest_offset
        else [],
    )

    pending = sync.synchronize_outbox(producer, object(), cache)
    assert pending.pending_command_event_ids == (durable.event_id,)
    assert calls == []
    assert pending.uploaded == ()
    assert _remote.records == []

    accepted = sync.synchronize_outbox(
        producer,
        object(),
        cache,
        credential_resolver=lambda _record: {ref: token},
    )
    assert accepted.command_decisions == (decision,)
    assert accepted.pending_command_event_ids == ()
    assert calls == [(durable.event_id, {ref: token})]
    assert [result.record.event_id for result in accepted.uploaded] == [
        later_observation.event_id
    ]
    assert accepted.decision_watermark.ingest_offset == 7
    cached = projection.list_cached_authority_decisions(cache)
    assert [entry.decision["request_event_id"] for entry in cached] == [durable.event_id]
    assert token not in str(cached[0].decision)


@pytest.mark.parametrize("batch_size", [0, -1, True])
def test_sync_rejects_invalid_batch_sizes(transport, batch_size):
    producer, cache, _remote = transport
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        sync.synchronize_outbox(producer, object(), cache, batch_size=batch_size)
