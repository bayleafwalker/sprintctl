"""Read-only assessment of Sprintctl's current owner completion state.

This evaluator reports only facts exposed by the Sprintctl owner. It never
creates a Decision, changes a lease, or grants permission to accept work.
Outcome reports remain authoritative only through the existing
``work.lease.report-outcome-v1`` writer, which recomputes current requirements
under the item lock. The current lease schema does not bind a lease to the
item revision or Release at acquisition, so this evaluator never recommends
that writer as a safe automatic completion path.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import releases

SCHEMA_VERSION = "work-completion-assessment/v1"
EVALUATOR_REVISION = "sprintctl-owner-completion/v1"
MAX_HISTORY_ROWS = 64


class AssessmentError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def validate_request(arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict) or set(arguments) != {
        "item_id", "expected_revision", "release_digest"
    }:
        raise AssessmentError(
            "invalid-arguments",
            "assessment requires exactly item_id, expected_revision, and release_digest",
        )
    if type(arguments["item_id"]) is not int or arguments["item_id"] < 1:
        raise AssessmentError("invalid-arguments", "item_id must be a positive integer")
    if (
        not isinstance(arguments["expected_revision"], str)
        or not releases._RELEASE_REVISION_RE.fullmatch(arguments["expected_revision"])
    ):
        raise AssessmentError(
            "invalid-arguments", "expected_revision must be the full Release revision"
        )
    if not isinstance(arguments["release_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", arguments["release_digest"]
    ):
        raise AssessmentError("invalid-arguments", "release_digest must be 64 lowercase hex characters")


def _basis_status(requested: dict, current: dict | None) -> str:
    if current is None or current["release_digest"] is None:
        return "missing"
    if (
        requested["expected_revision"] != current["expected_revision"]
        or requested["release_digest"] != current["release_digest"]
        or current["release_item_revision"] != current["expected_revision"]
    ):
        return "stale"
    return "current"


def _recommendation(
    item_id: int,
    current: dict,
    snapshot_digest: str,
    action: str,
    reason: str,
    next_operation: str | None,
) -> dict:
    identity = {
        "target_item_id": item_id,
        "source_snapshot_digest": snapshot_digest,
        "current_revision": current["expected_revision"] if current else None,
        "action_kind": action,
    }
    return {
        "recommendation_id": digest(identity),
        "action_kind": action,
        "target_item_id": item_id,
        "source_snapshot_digest": snapshot_digest,
        "current_revision": identity["current_revision"],
        "reason_code": reason,
        "next_operation": next_operation,
    }


def evaluate(
    *, repo_id: str, item_id: int, requested_basis: dict, snapshot: dict,
) -> dict:
    """Assess one bounded owner snapshot and suggest nonmutating follow-ups."""
    current = snapshot["current_basis"]
    basis_status = _basis_status(requested_basis, current)
    digest_snapshot = {
        key: value for key, value in snapshot.items() if key != "observed_at"
    }
    source_digest = digest(
        {"requested_basis": requested_basis, "snapshot": digest_snapshot}
    )
    item = snapshot["item"]
    decision = snapshot["terminal_decision"]
    completeness = snapshot["source_completeness"]
    blockers = snapshot["blocker_item_ids"]
    reports = snapshot["outcome_reports"]
    leases = snapshot["leases"]
    active_lease = next((lease for lease in leases if lease["state"] == "active"), None)

    if basis_status == "missing":
        status, reason = "missing-release", "current-release-unavailable"
    elif basis_status == "stale":
        status, reason = "stale-basis", "requested-basis-is-not-current"
    elif (
        item["status"] == "done"
        and item["resolution"] == "accepted"
        and decision is not None
        and decision["kind"] == "accept"
        and decision["release_digest"] == current["release_digest"]
    ):
        status, reason = "accepted-current", "owner-accepted-current-release"
    elif not all(completeness.values()):
        status, reason = "unknown", "owner-history-incomplete"
    elif item["status"] == "done" or item["resolution"] is not None:
        status, reason = "conflict", "terminal-owner-state-does-not-match-current-release"
    elif item["status"] == "blocked" or blockers:
        status, reason = "blocked", "work-item-has-unsettled-blockers"
    elif any(
        report["disposition"] == "awaiting-verification"
        and active_lease is not None
        and report["lease_id"] == active_lease["lease_id"]
        for report in reports
    ):
        status, reason = "requires-judgment", "owner-report-awaits-verification"
    elif active_lease is not None:
        status, reason = "incomplete", "active-lease-release-basis-unavailable"
    elif item["status"] != "active":
        status, reason = "incomplete", "work-item-is-not-active"
    else:
        status, reason = "incomplete", "no-current-owner-acceptance"

    if status == "accepted-current" and all(completeness.values()) and active_lease is None:
        action, next_operation = "no-change", None
    elif status == "accepted-current":
        action, next_operation = "inspect-conflict", None
    elif status in {"stale-basis", "missing-release", "conflict", "unknown", "blocked"}:
        action, next_operation = "inspect-conflict", None
    elif status == "requires-judgment":
        action, next_operation = "request-independent-review", None
    elif active_lease is not None:
        action, next_operation = "inspect-conflict", None
    else:
        action, next_operation = "reverify", "work.lease.acquire-v1"

    suggestion = _recommendation(
        item_id, current, source_digest, action, reason, next_operation
    )
    return {
        "repo_id": repo_id,
        "schema_version": SCHEMA_VERSION,
        "evaluator_revision": EVALUATOR_REVISION,
        "requested_basis": requested_basis,
        "current_basis": current,
        "basis_status": basis_status,
        "snapshot_digest": source_digest,
        "assessment": {"status": status, "reason_codes": [reason]},
        "owner_facts": {
            "item_status": item["status"],
            "resolution": item["resolution"],
            "terminal_decision": decision,
            "verification": snapshot["verification"],
            "active_lease": active_lease,
            "outcome_reports": reports,
            "blocker_item_ids": blockers,
            "source_completeness": completeness,
            "observed_at": snapshot["observed_at"],
        },
        "follow_up": {
            "dedupe_scope": "exact-source-snapshot",
            "prior_rejection_tracking": "unsupported",
            "suggestions": [suggestion],
        },
        "authorizes_acceptance": False,
    }
