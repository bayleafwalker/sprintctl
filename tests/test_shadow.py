from __future__ import annotations

import json

import pytest

from sprintctl import outbox, shadow


def _observation(event_id: str, *, payload: dict[str, object] | None = None, **changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "record_class": "observation",
        "event_id": event_id,
        "event_type": "work.completed",
        "actor": "agent-a",
        "occurred_at": "2026-07-14T12:00:00Z",
        "payload": payload if payload is not None else {"item": 1145, "tags": ["shadow", "parity"]},
        "runtime_session_id": "session-a",
        "basis_revision": "item:1145@rev:1",
        "correlation_id": "correlation-a",
        "causation_id": None,
    }
    record.update(changes)
    return record


def test_projection_canonicalizes_json_without_mutating_source_and_preserves_sequence():
    first = _observation("event-2", payload={"z": 2, "a": {"later": True, "first": False}})
    second = _observation("event-1", payload={"item": 1145})

    projection = shadow.project_observations([first, second])

    assert [record.event_id for record in projection.observations] == ["event-2", "event-1"]
    assert projection.observations[0].payload == {"a": {"first": False, "later": True}, "z": 2}
    projection.observations[0].payload["a"]["first"] = "changed"
    assert first["payload"] == {"z": 2, "a": {"later": True, "first": False}}
    assert json.loads(json.dumps(projection.to_dict())) == projection.to_dict()


def test_projection_accepts_outbox_records(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer.db")
    record = outbox.append_observation(
        conn,
        event_type="note.recorded",
        actor="agent-a",
        payload={"summary": "pilot evidence"},
        event_id="outbox-event",
        occurred_at="2026-07-14T12:00:00Z",
    )

    projection = shadow.project_observations([record])

    assert projection.to_dict()["observations"] == [
        {
            "event_id": "outbox-event",
            "event_type": "note.recorded",
            "actor": "agent-a",
            "occurred_at": "2026-07-14T12:00:00Z",
            "payload": {"summary": "pilot evidence"},
            "runtime_session_id": None,
            "basis_revision": None,
            "correlation_id": None,
            "causation_id": None,
        }
    ]
    conn.close()


@pytest.mark.parametrize(
    "record",
    [
        _observation("command", record_class="authority-command", event_type="item.done"),
        _observation("decision", record_class="remote-decision", event_type="item.transitioned"),
        _observation("unknown", event_type="unclassified.event"),
    ],
)
def test_projection_rejects_commands_decisions_and_unknown_types_without_interpreting_them(record):
    with pytest.raises((shadow.UnsupportedShadowRecordError, ValueError)):
        shadow.project_observations([record])


def test_projection_rejects_duplicate_identity_and_non_json_payloads():
    with pytest.raises(shadow.DuplicateShadowObservationError, match="duplicate event_id"):
        shadow.project_observations([_observation("duplicate"), _observation("duplicate")])
    with pytest.raises(ValueError, match="JSON-safe"):
        shadow.project_observations([_observation("not-json", payload={"bad": {1, 2}})])


def test_compare_parity_emits_every_classification_in_deterministic_sequence():
    authoritative = shadow.project_observations(
        [
            _observation("equal"),
            _observation("missing"),
            _observation("mismatched", payload={"item": "authority"}),
        ]
    )
    shadow_projection = shadow.project_observations(
        [
            _observation("unexpected"),
            _observation("equal"),
            _observation("mismatched", payload={"item": "shadow"}),
        ]
    )

    report = shadow.compare_parity(authoritative, shadow_projection)

    assert [(entry.classification, entry.event_id) for entry in report.evidence] == [
        (shadow.ParityClassification.EQUAL, "equal"),
        (shadow.ParityClassification.MISSING, "missing"),
        (shadow.ParityClassification.MISMATCHED, "mismatched"),
        (shadow.ParityClassification.UNEXPECTED, "unexpected"),
    ]
    assert report.counts == {"equal": 1, "missing": 1, "unexpected": 1, "mismatched": 1}
    assert report.is_equal is False
    evidence = report.to_dict()["evidence"]
    assert evidence[1]["authoritative_index"] == 1
    assert evidence[1]["shadow"] is None
    assert evidence[3]["authoritative"] is None
    assert evidence[3]["shadow_index"] == 0
    assert json.loads(json.dumps(report.to_dict())) == report.to_dict()


def test_equal_parity_accepts_raw_sequences_and_does_not_mutate_them():
    authoritative = [_observation("event-1", payload={"nested": {"b": 2, "a": 1}})]
    shadow_records = [_observation("event-1", payload={"nested": {"a": 1, "b": 2}})]

    report = shadow.compare_parity(authoritative, shadow_records)

    assert report.is_equal is True
    assert report.counts == {"equal": 1, "missing": 0, "unexpected": 0, "mismatched": 0}
    assert authoritative[0]["payload"] == {"nested": {"b": 2, "a": 1}}
    assert shadow_records[0]["payload"] == {"nested": {"a": 1, "b": 2}}
