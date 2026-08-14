from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from sprintctl import contracts


REPO_ID = str(uuid4())
ITEM_UUID = str(uuid4())
SPRINT_UUID = str(uuid4())
CREDENTIAL_REF = "sha256:" + "a" * 64
PROPOSED_CREDENTIAL_REF = "sha256:" + "b" * 64


def _refs(aggregate_type: str) -> dict[str, object]:
    result: dict[str, object] = {
        "repo_id": REPO_ID,
        "aggregate_type": aggregate_type,
        "aggregate_id": 17,
    }
    if aggregate_type == "claim":
        result["claim_id"] = 17
    else:
        result["aggregate_uuid"] = ITEM_UUID if aggregate_type == "item" else SPRINT_UUID
    return result


def _payload(record_type: str) -> dict[str, object]:
    return {
        "item.transition": {"to_status": "blocked", "claim_id": 17, "credential_ref": CREDENTIAL_REF},
        "item.done": {"to_status": "done", "claim_id": 17, "credential_ref": CREDENTIAL_REF},
        "sprint.activate": {},
        "sprint.close": {},
        "claim.acquire": {
            "agent": "worker-a",
            "claim_type": "execute",
            "exclusive": True,
            "ttl_seconds": 600,
            "credential_ref": CREDENTIAL_REF,
            "metadata": {"runtime_session_id": "session-a"},
        },
        "claim.renew": {"claim_id": 17, "ttl_seconds": 600, "credential_ref": CREDENTIAL_REF},
        "claim.handoff": {
            "claim_id": 17,
            "to_actor": "worker-b",
            "mode": "rotate",
            "ttl_seconds": 600,
            "credential_ref": CREDENTIAL_REF,
            "proposed_credential_ref": PROPOSED_CREDENTIAL_REF,
            "metadata": {"runtime_session_id": "session-b"},
        },
        "claim.release": {"claim_id": 17, "credential_ref": CREDENTIAL_REF},
        "capability-receipt.accept": {
            "pointer": {
                "project": "sprintctl",
                "receipt_id": "sprintctl.2026-07-14.boundary",
                "receipt_path": (
                    "/projects/dev/_artifacts/sprintctl/capability/receipts/"
                    "sprintctl.2026-07-14.boundary.json"
                ),
                "receipt_sha256": "c" * 64,
            }
        },
    }[record_type]


def _aggregate_type(record_type: str) -> str:
    if record_type.startswith("claim.") and record_type != "claim.acquire":
        return "claim"
    if record_type.startswith("sprint.") or record_type == "capability-receipt.accept":
        return "sprint"
    return "item"


def _command(record_type: str, *, payload=None, refs=None, basis_revision="revision:7"):
    return contracts.AuthorityCommand(
        event_id=uuid4(),
        record_type=record_type,
        schema_version="sprintctl-record/v1",
        actor="authority-contract-test",
        authored_at="2026-07-14T12:00:00Z",
        refs=refs if refs is not None else _refs(_aggregate_type(record_type)),
        payload=payload if payload is not None else _payload(record_type),
        basis_revision=basis_revision,
    )


@pytest.mark.parametrize(
    "record_type",
    [
        "claim.acquire",
        "claim.renew",
        "claim.handoff",
        "claim.release",
        "item.transition",
        "item.done",
        "sprint.activate",
        "sprint.close",
        "capability-receipt.accept",
    ],
)
def test_authority_command_shapes_round_trip_canonically(record_type):
    command = _command(record_type)

    assert command.requires_remote_arbitration is True
    assert command.refs["repo_id"] == REPO_ID
    assert contracts.record_from_dict(command.to_dict()) == command


def test_claim_acquire_coordinate_binding_is_paired_and_canonical():
    payload = _payload("claim.acquire")
    payload.update(
        coordinate_claim_id=23,
        coordinate_credential_ref=PROPOSED_CREDENTIAL_REF,
    )
    command = _command("claim.acquire", payload=payload)
    assert command.payload["coordinate_claim_id"] == 23

    for missing in ("coordinate_claim_id", "coordinate_credential_ref"):
        invalid = deepcopy(payload)
        invalid.pop(missing)
        with pytest.raises(ValueError, match="must be supplied together"):
            _command("claim.acquire", payload=invalid)


def test_claim_handoff_rotate_and_transfer_credential_rules():
    rotate = _command("claim.handoff")
    assert rotate.payload["proposed_credential_ref"] == PROPOSED_CREDENTIAL_REF

    missing_proposed = _payload("claim.handoff")
    missing_proposed.pop("proposed_credential_ref")
    with pytest.raises(ValueError, match="required for rotate"):
        _command("claim.handoff", payload=missing_proposed)

    transfer = _payload("claim.handoff")
    transfer["mode"] = "transfer"
    transfer.pop("proposed_credential_ref")
    assert _command("claim.handoff", payload=transfer).payload["mode"] == "transfer"

    transfer["proposed_credential_ref"] = PROPOSED_CREDENTIAL_REF
    with pytest.raises(ValueError, match="forbidden for transfer"):
        _command("claim.handoff", payload=transfer)


@pytest.mark.parametrize("record_type", ["claim.renew", "claim.handoff", "claim.release"])
def test_claim_ref_and_payload_ids_must_match(record_type):
    payload = _payload(record_type)
    payload["claim_id"] = 18
    with pytest.raises(ValueError, match="refs.claim_id must equal payload.claim_id"):
        _command(record_type, payload=payload)


@pytest.mark.parametrize(
    "secret_field",
    ["claim_token", "token", "credential", "secret", "password", "api_key"],
)
def test_raw_secret_and_credential_fields_are_rejected_recursively(secret_field):
    payload = _payload("claim.acquire")
    payload["metadata"] = {"nested": {secret_field: "must-not-enter-outbox"}}
    with pytest.raises(ValueError, match="must not contain secret field"):
        _command("claim.acquire", payload=payload)


@pytest.mark.parametrize("unknown_field", ["proof", "claim_proof", "bearer", "notes"])
def test_claim_metadata_is_strictly_allowlisted(unknown_field):
    payload = _payload("claim.acquire")
    payload["metadata"] = {unknown_field: "must-not-enter-outbox"}
    with pytest.raises(ValueError, match=f"unknown fields: {unknown_field}"):
        _command("claim.acquire", payload=payload)


def test_unknown_fields_missing_basis_and_unstable_refs_are_rejected():
    payload = _payload("item.done")
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields: unexpected"):
        _command("item.done", payload=payload)

    with pytest.raises(ValueError, match="basis_revision is required"):
        _command("item.done", basis_revision=None)

    refs = _refs("item")
    refs["aggregate_uuid"] = "not-a-uuid"
    with pytest.raises(ValueError, match="refs.aggregate_uuid must be a UUID"):
        _command("item.done", refs=refs)


@pytest.mark.parametrize(
    ("aggregate_type", "field"),
    [("claim", "repo_id"), ("item", "repo_id"), ("item", "aggregate_uuid")],
)
def test_authority_refs_reject_missing_required_uuids_with_value_error(aggregate_type, field):
    refs = _refs(aggregate_type)
    refs[field] = None
    with pytest.raises(ValueError, match=rf"refs\.{field} must be a UUID"):
        _command("claim.release" if aggregate_type == "claim" else "item.done", refs=refs)


def test_item_done_is_strictly_done_and_claim_completion_is_retired():
    with pytest.raises(ValueError, match="must be 'done'"):
        _command("item.done", payload={"to_status": "blocked"})

    assert contracts.record_class_for_type("claim.handed-off") is contracts.RecordClass.REMOTE_DECISION
    assert contracts.record_class_for_type("claim.released") is contracts.RecordClass.REMOTE_DECISION
    with pytest.raises(ValueError, match="not classified"):
        contracts.record_class_for_type("item.done-from-claim.completed")


def test_claim_renew_metadata_is_optional_and_matches_the_acquire_allowlist():
    # No metadata at all: unchanged, backward-compatible shape.
    without_metadata = _command("claim.renew")
    assert "metadata" not in without_metadata.payload

    # An explicit, partial metadata object round-trips canonically and only
    # keeps the fields actually supplied.
    payload = _payload("claim.renew")
    payload["metadata"] = {"branch": "feature/renew", "pid": 4242}
    with_metadata = _command("claim.renew", payload=payload)
    assert with_metadata.payload["metadata"] == {
        "branch": "feature/renew",
        "pid": 4242,
    }

    # The same strict allowlist claim.acquire/claim.handoff already enforce.
    payload = _payload("claim.renew")
    payload["metadata"] = {"bearer": "must-not-enter-outbox"}
    with pytest.raises(ValueError, match="unknown fields: bearer"):
        _command("claim.renew", payload=payload)


def test_claim_handoff_note_is_optional_and_is_a_plain_string():
    without_note = _command("claim.handoff")
    assert "note" not in without_note.payload

    payload = _payload("claim.handoff")
    payload["note"] = "Structured handoff note."
    with_note = _command("claim.handoff", payload=payload)
    assert with_note.payload["note"] == "Structured handoff note."

    payload = _payload("claim.handoff")
    payload["note"] = 12345
    with pytest.raises(ValueError, match="payload.note must be a non-empty string"):
        _command("claim.handoff", payload=payload)

    payload = _payload("claim.handoff")
    payload["note"] = {"claim_token": "must-not-enter-outbox"}
    with pytest.raises(ValueError, match="must not contain secret field"):
        _command("claim.handoff", payload=payload)
