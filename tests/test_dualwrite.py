from __future__ import annotations

import json
from uuid import uuid4

import pytest

from sprintctl import contracts, dualwrite, outbox


def _event_kwargs(record_type: str) -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "record_type": record_type,
        "schema_version": "sprintctl-record/v1",
        "actor": "dual-record-test",
        "authored_at": "2026-07-14T12:00:00Z",
        "refs": {"work_item_id": "1146", "paths": ["sprintctl/dualwrite.py"]},
        "payload": {"summary": "dual-write observation", "passed": True},
        "basis_revision": "item:1146@7",
        "correlation_id": uuid4(),
        "causation_id": uuid4(),
        "payload_digest": "a" * 64,
        "artifact_digest": "b" * 64,
    }


def test_mirrors_a_classified_observation_with_full_canonical_envelope(tmp_path):
    observation = contracts.Observation(**_event_kwargs("work.completed"))
    producer = outbox.open_outbox(tmp_path / "producer.db")

    result = dualwrite.mirror_event(producer, observation, runtime_session_id="session-1146")

    assert result.disposition is dualwrite.MirrorDisposition.MIRRORED
    assert result.mirrored is True
    assert result.record is not None
    assert result.record.event_id == observation.event_id
    assert result.record.event_type == observation.record_type
    assert result.record.actor == observation.actor
    assert result.record.runtime_session_id == "session-1146"
    assert result.record.occurred_at == observation.authored_at
    assert result.record.basis_revision == observation.basis_revision
    assert result.record.correlation_id == observation.correlation_id
    assert result.record.causation_id == observation.causation_id
    assert result.record.payload == observation.to_dict()
    assert contracts.record_from_dict(result.record.payload) == observation


def test_mapping_input_is_canonical_json_safe_and_defensive(tmp_path):
    observation = contracts.Observation(**_event_kwargs("note.recorded"))
    input_envelope = observation.to_dict()
    input_envelope["payload"]["nested"] = {"ordered": ["value"]}
    producer = outbox.open_outbox(tmp_path / "producer.db")

    result = dualwrite.mirror_event(producer, json.loads(json.dumps(input_envelope)))
    input_envelope["payload"]["nested"]["ordered"].append("mutated")

    assert result.record is not None
    assert result.record.payload["payload"]["nested"] == {"ordered": ["value"]}
    assert dualwrite.canonical_event_envelope(result.record.payload).to_dict() == result.record.payload


def test_retries_are_idempotent_by_authoritative_event_identity(tmp_path):
    observation = contracts.Observation(**_event_kwargs("decision.recorded"))
    producer = outbox.open_outbox(tmp_path / "producer.db")

    first = dualwrite.mirror_event(producer, observation)
    second = dualwrite.mirror_event(producer, observation.to_dict())

    assert first.mirrored is second.mirrored is True
    assert first.record is not None and second.record is not None
    assert second.record.origin_stream_id == first.record.origin_stream_id
    assert second.record.origin_seq == first.record.origin_seq
    assert [record.event_id for record in outbox.list_records(producer)] == [observation.event_id]


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("item.done", dualwrite.MirrorDisposition.REFUSED_AUTHORITY_COMMAND),
        ("claim.granted", dualwrite.MirrorDisposition.REFUSED_REMOTE_DECISION),
    ],
)
def test_commands_and_remote_decisions_are_explicit_non_mutating_refusals(tmp_path, event, expected):
    envelope_class = (
        contracts.AuthorityCommand
        if expected is dualwrite.MirrorDisposition.REFUSED_AUTHORITY_COMMAND
        else contracts.RemoteDecision
    )
    producer = outbox.open_outbox(tmp_path / "producer.db")

    result = dualwrite.mirror_event(producer, envelope_class(**_event_kwargs(event)))

    assert result.disposition is expected
    assert result.mirrored is False
    assert result.record is None
    assert outbox.list_records(producer) == []


def test_unclassified_or_non_json_safe_input_is_rejected_before_outbox_mutation(tmp_path):
    producer = outbox.open_outbox(tmp_path / "producer.db")
    invalid = contracts.Observation(**_event_kwargs("work.completed")).to_dict()
    invalid["payload"] = {"not_json": object()}

    with pytest.raises(ValueError, match="JSON-compatible"):
        dualwrite.mirror_event(producer, invalid)

    assert outbox.list_records(producer) == []
