from __future__ import annotations

from uuid import uuid4

import pytest

from sprintctl import contracts


REPO_ID = str(uuid4())
ITEM_UUID = str(uuid4())
SPRINT_UUID = str(uuid4())


def _command(record_type: str, *, payload: dict[str, object], aggregate_type: str, aggregate_uuid: str):
    return contracts.AuthorityCommand(
        event_id=uuid4(),
        record_type=record_type,
        schema_version="sprintctl-record/v1",
        actor="authority-contract-test",
        authored_at="2026-07-14T12:00:00Z",
        refs={
            "repo_id": REPO_ID,
            "aggregate_type": aggregate_type,
            "aggregate_id": 17,
            "aggregate_uuid": aggregate_uuid,
        },
        payload=payload,
        basis_revision="revision:7",
    )


@pytest.mark.parametrize(
    ("record_type", "payload", "aggregate_type", "aggregate_uuid"),
    [
        ("item.transition", {"to_status": "blocked"}, "item", ITEM_UUID),
        ("item.done", {"to_status": "done"}, "item", ITEM_UUID),
        ("sprint.activate", {}, "sprint", SPRINT_UUID),
        ("sprint.close", {}, "sprint", SPRINT_UUID),
    ],
)
def test_supported_authority_command_shapes_round_trip_canonically(
    record_type, payload, aggregate_type, aggregate_uuid,
):
    command = _command(
        record_type, payload=payload, aggregate_type=aggregate_type,
        aggregate_uuid=aggregate_uuid,
    )
    assert contracts.record_from_dict(command.to_dict()) == command


@pytest.mark.parametrize(
    "record_type",
    ["claim.acquire", "claim.renew", "claim.handoff", "claim.release"],
)
def test_claim_authority_record_types_are_retired(record_type):
    with pytest.raises(ValueError, match="not classified"):
        contracts.record_class_for_type(record_type)


def test_item_commands_reject_claim_proof_fields():
    with pytest.raises(ValueError, match="unknown fields: claim_id"):
        _command(
            "item.done",
            payload={"to_status": "done", "claim_id": 17},
            aggregate_type="item",
            aggregate_uuid=ITEM_UUID,
        )


def test_item_done_is_strictly_done():
    with pytest.raises(ValueError, match="must be 'done'"):
        _command(
            "item.done",
            payload={"to_status": "blocked"},
            aggregate_type="item",
            aggregate_uuid=ITEM_UUID,
        )
