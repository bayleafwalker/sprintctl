from copy import deepcopy

import jsonschema

from sprintctl import completion_assessment as completion
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS


BASIS = {
    "item_id": 7,
    "expected_revision": "item:00000000-0000-0000-0000-000000000007@description:v2@sha256:" + "a" * 64 + "@revise:0",
    "release_digest": "b" * 64,
}
_CONTRACT = next(
    contract for contract in WORK_OPERATION_CONTRACTS
    if contract.name == "work.completion.assess-v1"
)


def snapshot(**changes):
    return {
        "current_basis": {
            **BASIS,
            "release_item_revision": BASIS["expected_revision"],
        },
        "item": {"status": "active", "resolution": None},
        "terminal_decision": None,
        "verification": {
            "profile": "checked",
            "requirements": ["checks"],
            "required_checks": [],
        },
        "leases": [],
        "outcome_reports": [],
        "blocker_item_ids": [],
        "source_completeness": {
            "current_releases": True,
            "leases": True,
            "outcome_reports": True,
            "blockers": True,
        },
        "observed_at": "2026-10-26T12:00:00Z",
        **changes,
    }


def evaluate(source=None, requested=None):
    result = completion.evaluate(
        repo_id="fixture",
        item_id=BASIS["item_id"],
        requested_basis=requested or BASIS,
        snapshot=source or snapshot(),
    )
    jsonschema.validate(result, _CONTRACT.result_schema)
    return result


def test_current_acceptance_is_owner_fact_and_advisory_only():
    source = snapshot(
        item={"status": "done", "resolution": "accepted"},
        terminal_decision={
            "decision_id": 31,
            "kind": "accept",
            "release_digest": BASIS["release_digest"],
        },
    )
    result = evaluate(source)

    assert result["assessment"]["status"] == "accepted-current"
    assert result["follow_up"]["suggestions"][0]["action_kind"] == "no-change"
    assert result["authorizes_acceptance"] is False


def test_stale_requested_revision_fails_closed_before_acceptance():
    old_basis = {**BASIS, "release_digest": "c" * 64}
    source = snapshot(
        item={"status": "done", "resolution": "accepted"},
        terminal_decision={
            "decision_id": 31,
            "kind": "accept",
            "release_digest": BASIS["release_digest"],
        },
    )
    result = evaluate(source, old_basis)

    assert result["basis_status"] == "stale"
    assert result["assessment"]["status"] == "stale-basis"
    assert result["follow_up"]["suggestions"][0]["action_kind"] == "inspect-conflict"


def test_unbound_active_lease_blocks_automatic_follow_up_but_review_stays_separate():
    active = {
        "lease_id": "lease_" + "A" * 26,
        "state": "active",
        "stale": False,
        "verification": {
            "profile": "checked",
            "requirements": ["checks"],
            "required_checks": [],
        },
    }
    basic = evaluate(snapshot(leases=[active]))
    assert basic["assessment"]["status"] == "incomplete"
    assert basic["assessment"]["reason_codes"] == [
        "active-lease-release-basis-unavailable"
    ]
    assert basic["follow_up"]["suggestions"][0]["action_kind"] == "inspect-conflict"
    assert basic["follow_up"]["suggestions"][0]["next_operation"] is None

    pending_review = {
        "report_id": "outcome_" + "B" * 26,
        "lease_id": active["lease_id"],
        "outcome": "succeeded",
        "disposition": "awaiting-verification",
        "reason_code": None,
        "verification_profile": "human-authorized",
        "decision_id": None,
        "created_at": "2026-10-26T11:00:00Z",
    }
    reviewed = evaluate(snapshot(leases=[active], outcome_reports=[pending_review]))
    assert reviewed["assessment"]["status"] == "requires-judgment"
    assert reviewed["follow_up"]["suggestions"][0]["action_kind"] == "request-independent-review"


def test_expired_active_lease_does_not_offer_unbound_lease_for_completion():
    active = {
        "lease_id": "lease_" + "A" * 26,
        "state": "active",
        "stale": True,
        "verification": {
            "profile": "checked",
            "requirements": ["checks"],
            "required_checks": [],
        },
    }

    result = evaluate(snapshot(leases=[active]))

    assert result["assessment"]["status"] == "incomplete"
    assert result["assessment"]["reason_codes"] == [
        "active-lease-release-basis-unavailable"
    ]
    assert result["follow_up"]["suggestions"][0]["action_kind"] == "inspect-conflict"
    assert result["follow_up"]["suggestions"][0]["next_operation"] is None


def test_unknown_owner_history_never_becomes_sufficient_and_ids_are_repeatable():
    source = snapshot(source_completeness={
        "current_releases": True, "leases": True, "outcome_reports": False,
        "blockers": True,
    })
    first = evaluate(source)
    repeated_snapshot = deepcopy(source)
    repeated_snapshot["observed_at"] = "2026-10-26T12:01:00Z"
    repeated = evaluate(repeated_snapshot)

    assert first["assessment"]["status"] == "unknown"
    assert first["follow_up"]["suggestions"][0]["action_kind"] == "inspect-conflict"
    assert first["snapshot_digest"] == repeated["snapshot_digest"]
    assert (
        first["follow_up"]["suggestions"][0]["recommendation_id"]
        == repeated["follow_up"]["suggestions"][0]["recommendation_id"]
    )
    assert first["follow_up"]["prior_rejection_tracking"] == "unsupported"


def test_owner_acceptance_remains_a_fact_when_unrelated_history_is_truncated():
    source = snapshot(
        item={"status": "done", "resolution": "accepted"},
        terminal_decision={
            "decision_id": 31,
            "kind": "accept",
            "release_digest": BASIS["release_digest"],
        },
        source_completeness={
            "current_releases": True, "leases": True, "outcome_reports": False,
            "blockers": True,
        },
    )

    result = evaluate(source)

    assert result["assessment"]["status"] == "accepted-current"
    assert result["follow_up"]["suggestions"][0]["action_kind"] == "inspect-conflict"


def test_stale_release_record_is_not_treated_as_current_basis():
    stale_release = snapshot()
    stale_release["current_basis"]["release_item_revision"] = "item:older"

    result = evaluate(stale_release)

    assert result["basis_status"] == "stale"
    assert result["assessment"]["status"] == "stale-basis"


def test_closed_request_validation_requires_full_basis():
    completion.validate_request(BASIS)
    try:
        completion.validate_request({**BASIS, "extra": True})
    except completion.AssessmentError as exc:
        assert exc.code == "invalid-arguments"
    else:
        raise AssertionError("unknown request field was accepted")
