from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import observations, outbox, pg


def test_postgres_timestamp_normalization_converts_aware_values_to_utc():
    local = datetime(
        2026,
        7,
        19,
        15,
        0,
        tzinfo=timezone(timedelta(hours=3)),
    )

    assert pg._iso(local) == "2026-07-19T12:00:00Z"


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


def test_prepared_ingest_record_fingerprint_includes_producer_created_at(tmp_path):
    record = _record(tmp_path)

    original = pg._prepare_ingest_record(record)
    changed = pg._prepare_ingest_record(
        replace(record, created_at="2026-07-14T12:00:01Z")
    )

    assert original.record_sha256 != changed.record_sha256


def test_remote_ingestion_rejects_non_observation_records(tmp_path):
    record = replace(_record(tmp_path), record_class="remote-decision")

    with pytest.raises(ValueError, match="producer observations only"):
        pg._prepare_ingest_record(record)


def test_remote_ingestion_rejects_a_payload_digest_that_does_not_match(tmp_path):
    record = replace(_record(tmp_path), payload_sha256="0" * 64)

    with pytest.raises(ValueError, match="payload_sha256 does not match"):
        pg._prepare_ingest_record(record)


def test_remote_ingestion_validates_typed_item_evidence(tmp_path):
    conn = outbox.open_outbox(tmp_path / "typed-producer.db")
    try:
        record = observations.append_item_evidence_observation(
            conn,
            event_type=observations.WORK_COMPLETED,
            actor="session-wrapper",
            repo_id="sprintctl",
            sprint_id=407,
            work_item_id=1161,
            runtime_session_id="session-1161",
            summary="Complete",
            evidence_refs=[
                {
                    "kind": "git-commit",
                    "source": "repo:sprintctl",
                    "revision": "a" * 40,
                }
            ],
            authored_at="2026-07-19T12:00:00Z",
        )
    finally:
        conn.close()

    assert pg._prepare_ingest_record(record).record == record


def test_remote_ingestion_rejects_untyped_session_capsule_pointer(tmp_path):
    conn = outbox.open_outbox(tmp_path / "untyped-producer.db")
    try:
        record = outbox.append_observation(
            conn,
            event_type=observations.SESSION_CAPSULE_RECORDED,
            actor="untyped-producer",
            payload={"capsule": "inline payload"},
            runtime_session_id="session-1161",
            occurred_at="2026-07-19T12:00:00Z",
        )
    finally:
        conn.close()

    with pytest.raises(ValueError, match="record envelope"):
        pg._prepare_ingest_record(record)
