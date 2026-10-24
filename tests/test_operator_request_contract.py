from __future__ import annotations

import pytest

from sprintctl import operator_request as request
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS, catalog_operation_specs


def packet() -> dict:
    return {
        "schema": "operator-request/v1",
        "kind": "perform",
        "required_outcome": "The operator runs the checked migration command.",
        "reason": "The native session has no route to the approved operator host.",
        "execution": {
            "host": "ops.example.invalid",
            "directory": "/srv/service",
            "intended_identity": "deployment-operator",
            "target": "staging/service-a",
            "inputs": [{"name": "operator session", "acquire_from": "approved terminal login"}],
        },
        "procedure": {
            "version": "1.0.0",
            "steps": [{
                "step_id": "check",
                "instruction": "Run the documented read-only preflight command.",
                "expected_result": "The target reports the expected release digest.",
                "preconditions": ["The operator is on ops.example.invalid."],
                "verification": "Compare the returned digest with the pinned release.",
                "partial_progress": "Record the observed step before proceeding.",
                "recovery": "Stop and refresh this request if the digest differs.",
            }],
        },
        "blocker": {
            "condition": "A checked operator-only preflight remains outstanding.",
            "checked_at": "2026-10-24T10:00:00Z",
            "fresh_until": "2026-10-24T11:00:00Z",
            "source_revisions": [{"source": "release", "revision": "sha256:abc"}],
        },
    }


def test_complete_packet_and_publish_arguments_are_accepted():
    value = {
        "request_id": "req_01JBC3D4E5F6G7H8J9K0MNPQRS",
        "packet": packet(),
        "dependants": [
            {"work_item_id": 12, "session_id": "ses-1", "runtime_id": "codex"},
            {"work_item_id": 12, "session_id": "ses-2", "runtime_id": "claude"},
        ],
        "idempotency_key": "publish-operator-request-1",
    }

    result = request.publish_arguments(value)

    assert result["request_id"] == value["request_id"]
    assert result["packet"] == value["packet"]
    assert len(result["dependants"]) == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(kind="delegate"),
        lambda p: p.update(kind=[]),
        lambda p: p["procedure"]["steps"][0].pop("recovery"),
        lambda p: p["execution"].update(access_token="bearer-secret"),
        lambda p: p["blocker"].update(checked_at="yesterday"),
        lambda p: p["blocker"].update(fresh_until="2026-10-24T09:00:00Z"),
        lambda p: p["procedure"]["steps"][0].update(raw_transcript="private conversation"),
    ],
)
def test_incomplete_unsupported_stale_or_sensitive_packets_refuse(change):
    candidate = packet()
    change(candidate)

    with pytest.raises(request.RequestRefused):
        request.validate_packet(candidate)


def test_dependant_withdrawal_is_scoped_to_one_work_item_and_session():
    args = request.dependant_withdraw_arguments({
        "request_id": "req_01JBC3D4E5F6G7H8J9K0MNPQRS",
        "expected_revision": 7,
        "work_item_id": 12,
        "session_id": "ses-1",
        "idempotency_key": "withdraw-dependent-1",
    })

    assert args["expected_revision"] == 7
    assert (args["work_item_id"], args["session_id"]) == (12, "ses-1")


def test_duplicate_dependants_and_identity_claims_refuse():
    entries = [
        {"work_item_id": 12, "session_id": "ses-1"},
        {"work_item_id": 12, "session_id": "ses-1"},
    ]
    with pytest.raises(request.RequestRefused, match="unique"):
        request.validate_dependants(entries)

    candidate = packet()
    candidate["execution"]["workspace_id"] = "another-workspace"
    with pytest.raises(request.RequestRefused):
        request.validate_packet(candidate)


def test_revisions_must_be_positive_int32_and_not_boolean():
    for value in (0, -1, 2**31, True, "7"):
        with pytest.raises(request.RequestRefused):
            request.validate_revision(value)


def test_list_rejects_non_string_state():
    with pytest.raises(request.RequestRefused):
        request.list_arguments({"limit": 25, "state": []})


def test_request_operations_are_advertised_only_by_schema28_owners():
    old = catalog_operation_specs(resource_schema_available=True, remote_schema_version=27)
    current = catalog_operation_specs(resource_schema_available=True, remote_schema_version=28)
    request_names = {contract.name for contract in WORK_OPERATION_CONTRACTS
                     if contract.name.startswith("work.request.")}

    assert request_names
    assert not (request_names & {spec["name"] for spec in old})
    assert request_names <= {spec["name"] for spec in current}


def test_outcome_and_progress_arguments_preserve_distinct_states():
    request_id = "req_01JBC3D4E5F6G7H8J9K0MNPQRS"
    progress = request.progress_arguments({
        "request_id": request_id, "expected_revision": 3, "work_item_id": 12,
        "session_id": "ses-1", "step_id": "check", "status": "completed",
        "note": "The observed digest matched.", "idempotency_key": "progress-key-1",
    })
    report = request.report_performed_arguments({
        "request_id": request_id, "expected_revision": 4, "outcome": "performed",
        "note": "The command was run.", "idempotency_key": "report-key-1",
    })
    attestation = request.attest_arguments({
        "request_id": request_id, "expected_revision": 5,
        "statement": "I observed the physical work complete.",
        "performed_at": "2026-10-24T10:30:00Z", "idempotency_key": "attest-key-1",
    })

    assert progress["status"] == "completed"
    assert report["outcome"] == "performed"
    assert report["note"]
    assert attestation["performed_at"].endswith("+00:00")


def test_progress_and_attestation_reject_ambiguous_or_invalid_inputs():
    with pytest.raises(request.RequestRefused):
        request.progress_arguments({
            "request_id": "req_01JBC3D4E5F6G7H8J9K0MNPQRS", "expected_revision": 3,
            "work_item_id": 12, "session_id": "ses-1", "step_id": "check", "status": [],
            "note": "progress", "idempotency_key": "progress-key-1",
        })
    with pytest.raises(request.RequestRefused):
        request.report_performed_arguments({
            "request_id": "req_01JBC3D4E5F6G7H8J9K0MNPQRS", "expected_revision": 3,
            "outcome": "resolved", "note": "claimed success", "idempotency_key": "report-key-1",
        })
    with pytest.raises(request.RequestRefused):
        request.attest_arguments({
            "request_id": "req_01JBC3D4E5F6G7H8J9K0MNPQRS", "expected_revision": 3,
            "statement": "complete", "performed_at": "not a time", "idempotency_key": "attest-key-1",
        })
