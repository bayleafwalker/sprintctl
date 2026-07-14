from dataclasses import replace

import pytest

from sprintctl import outbox, pg


def _record(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer-outbox.db")
    try:
        return outbox.append_observation(
            conn,
            event_type="work.completed",
            actor="producer-a",
            payload={"item": "ingest"},
            event_id="ingest-record-1",
            occurred_at="2026-07-14T12:00:00Z",
        )
    finally:
        conn.close()


def test_prepared_ingest_record_uses_a_stable_full_record_fingerprint(tmp_path):
    record = _record(tmp_path)

    prepared = pg._prepare_ingest_record(record)

    assert prepared.record == record
    assert len(prepared.record_sha256) == 64
    assert prepared.payload_json == '{"item":"ingest"}'


def test_remote_ingestion_rejects_non_observation_records(tmp_path):
    record = replace(_record(tmp_path), record_class="remote-decision")

    with pytest.raises(ValueError, match="producer observations only"):
        pg._prepare_ingest_record(record)


def test_remote_ingestion_rejects_a_payload_digest_that_does_not_match(tmp_path):
    record = replace(_record(tmp_path), payload_sha256="0" * 64)

    with pytest.raises(ValueError, match="payload_sha256 does not match"):
        pg._prepare_ingest_record(record)
