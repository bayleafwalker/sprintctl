from __future__ import annotations

import json
from uuid import uuid4

import pytest

from sprintctl import contracts, db, observations, outbox, pilot, projection
from sprintctl.cli import cli


_COMMIT_REF = {
    "kind": "git-commit",
    "source": "repo:sprintctl",
    "revision": "a" * 40,
}
_CAPSULE_REF = {
    "kind": "artifact",
    "source": "agentops:_artifacts/sprintctl/session-capsules/capsule.json",
    "revision": "sha256:" + "b" * 64,
}


def _append_work_completed(conn, *, event_id=None, basis_revision="item:old@status:active"):
    return observations.append_item_evidence_observation(
        conn,
        event_type=observations.WORK_COMPLETED,
        actor="session-wrapper",
        repo_id="sprintctl",
        sprint_id=407,
        work_item_id=1161,
        runtime_session_id="session-1161",
        summary="Implementation and verification completed",
        evidence_refs=[_COMMIT_REF],
        basis_revision=basis_revision,
        event_id=event_id,
        authored_at="2026-07-19T12:00:00Z",
    )


def _configure_repo_marker(tmp_path) -> None:
    state = tmp_path / ".sprintctl"
    state.mkdir(exist_ok=True)
    (state / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )


def test_work_completed_is_strict_item_linked_evidence_not_an_authority_command(tmp_path):
    producer = outbox.open_outbox(tmp_path / "producer.db")
    try:
        record = _append_work_completed(producer)
        projected = observations.project_item_evidence(record)
    finally:
        producer.close()

    assert contracts.record_class_for_type(record.event_type) is contracts.RecordClass.OBSERVATION
    assert projected.work_item_id == 1161
    assert projected.runtime_session_id == "session-1161"
    assert projected.evidence_refs[0].to_dict() == _COMMIT_REF
    assert projected.basis.classification is observations.BasisClassification.UNRESOLVED
    assert projected.to_dict()["authority_mutated"] is False


def test_session_capsule_contract_accepts_only_an_immutable_pointer(tmp_path):
    producer = outbox.open_outbox(tmp_path / "producer.db")
    try:
        record = observations.append_item_evidence_observation(
            producer,
            event_type=observations.SESSION_CAPSULE_RECORDED,
            actor="session-wrapper",
            repo_id="sprintctl",
            sprint_id=407,
            work_item_id=1161,
            runtime_session_id="session-1161",
            capsule_ref=_CAPSULE_REF,
            evidence_refs=[_COMMIT_REF],
            basis_revision="item:old@status:active",
            authored_at="2026-07-19T12:01:00Z",
        )
        projected = observations.project_item_evidence(record)
    finally:
        producer.close()

    assert projected.capsule_ref is not None
    assert projected.capsule_ref.to_dict() == _CAPSULE_REF
    assert record.payload["payload"]["artifact_schema_version"] == "session-capsule/v1"
    assert "transcript" not in json.dumps(record.payload).lower()

    with pytest.raises(ValueError, match="exactly kind, source, and revision"):
        observations.build_item_evidence_observation(
            event_type=observations.SESSION_CAPSULE_RECORDED,
            actor="session-wrapper",
            repo_id="sprintctl",
            sprint_id=407,
            work_item_id=1161,
            capsule_ref={**_CAPSULE_REF, "raw_transcript": "must not be embedded"},
            authored_at="2026-07-19T12:01:00Z",
        )


def test_offline_append_retry_deduplicates_and_retains_anachronistic_basis(tmp_path):
    producer = outbox.open_outbox(tmp_path / "producer.db")
    event_id = str(uuid4())
    try:
        first = _append_work_completed(producer, event_id=event_id)
        retried = _append_work_completed(producer, event_id=event_id)
        projected = observations.project_item_evidence(
            first,
            current_revision="item:new@status:done",
        )

        assert retried == first
        assert [record.event_id for record in outbox.list_records(producer)] == [event_id]
    finally:
        producer.close()

    assert projected.basis.to_dict() == {
        "observed_revision": "item:old@status:active",
        "current_revision": "item:new@status:done",
        "classification": "anachronistic",
    }


def test_work_completed_rejects_missing_or_mutable_evidence():
    common = {
        "event_type": observations.WORK_COMPLETED,
        "actor": "session-wrapper",
        "repo_id": "sprintctl",
        "sprint_id": 407,
        "work_item_id": 1161,
        "summary": "Complete",
        "authored_at": "2026-07-19T12:00:00Z",
    }
    with pytest.raises(ValueError, match="at least one evidence_ref"):
        observations.build_item_evidence_observation(**common)
    with pytest.raises(ValueError, match="evidence ref kind"):
        observations.build_item_evidence_observation(
            **common,
            evidence_refs=[{"kind": "raw-transcript", "source": "inline", "revision": "latest"}],
        )


def test_cli_append_and_list_never_transition_authoritative_item(
    runner, conn, active_sprint, tmp_path
):
    _configure_repo_marker(tmp_path)
    track_id = db.get_or_create_track(conn, active_sprint["id"], "protocol")
    item_id = db.create_work_item(conn, active_sprint["id"], track_id, "Evidence only")
    db.set_work_item_status(conn, item_id, "active")
    events_before = db.list_events(conn, active_sprint["id"])
    event_id = str(uuid4())

    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0
    args = [
        "event",
        "observation",
        "add",
        "--type",
        "work.completed",
        "--sprint-id",
        str(active_sprint["id"]),
        "--item-id",
        str(item_id),
        "--actor",
        "session-wrapper",
        "--runtime-session-id",
        "session-cli",
        "--summary",
        "Evidence captured",
        "--evidence-ref",
        json.dumps(_COMMIT_REF),
        "--basis-revision",
        "item:old@status:active",
        "--event-id",
        event_id,
        "--json",
    ]
    appended = runner.invoke(cli, args)
    retried = runner.invoke(cli, args)

    assert appended.exit_code == 0, appended.output
    assert retried.exit_code == 0, retried.output
    assert json.loads(appended.output)["disposition"] == "appended"
    assert json.loads(retried.output)["disposition"] == "duplicate"

    listed = runner.invoke(
        cli,
        [
            "event",
            "observation",
            "list",
            "--item-id",
            str(item_id),
            "--current-basis-revision",
            "item:new@status:done",
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["count"] == 1
    assert payload["observations"][0]["basis"]["classification"] == "anachronistic"
    assert payload["observations"][0]["storage"] == {
        "local": True,
        "ingested": False,
        "ingest_offset": None,
    }
    assert payload["authority_mutated"] is False
    assert db.get_work_item(conn, item_id)["status"] == "active"
    assert db.list_events(conn, active_sprint["id"]) == events_before

    status = pilot.shadow_pilot_status(cwd=tmp_path)
    producer = outbox.open_outbox(status.paths.outbox_path)
    try:
        record = outbox.get_record(producer, event_id)
        assert record is not None
    finally:
        producer.close()
    cached_body = {
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
    }
    cache = projection.open_cached_projection(status.paths.projection_path)
    try:
        projection.apply_ingested_records(
            cache,
            [projection.CachedIngestRecord(ingest_offset=1, record=cached_body)],
            advanced_at="2026-07-19T12:05:00Z",
        )
    finally:
        cache.close()

    unioned = runner.invoke(
        cli,
        ["event", "observation", "list", "--item-id", str(item_id), "--json"],
    )
    assert unioned.exit_code == 0, unioned.output
    unioned_payload = json.loads(unioned.output)
    assert unioned_payload["count"] == 1
    assert unioned_payload["observations"][0]["storage"] == {
        "local": True,
        "ingested": True,
        "ingest_offset": 1,
    }
    assert unioned_payload["watermark"]["ingest_offset"] == 1


def test_cli_requires_explicit_pilot_opt_in(runner, tmp_path):
    _configure_repo_marker(tmp_path)
    result = runner.invoke(
        cli,
        [
            "event",
            "observation",
            "add",
            "--type",
            "work.completed",
            "--sprint-id",
            "407",
            "--item-id",
            "1161",
            "--actor",
            "wrapper",
            "--runtime-session-id",
            "session",
            "--summary",
            "Complete",
            "--evidence-ref",
            json.dumps(_COMMIT_REF),
        ],
    )
    assert result.exit_code != 0
    assert "pilot is disabled" in result.output


def test_generic_event_surface_cannot_bypass_capsule_pointer_contract(
    runner, conn, active_sprint
):
    result = runner.invoke(
        cli,
        [
            "event",
            "add",
            "--sprint-id",
            str(active_sprint["id"]),
            "--type",
            "session-capsule.recorded",
            "--actor",
            "untyped-producer",
            "--payload",
            '{"capsule":"inline payload"}',
        ],
    )
    assert result.exit_code != 0
    assert "reserved; use event observation add" in result.output
    assert db.list_events(conn, active_sprint["id"]) == []
