from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from sprintctl.terminal_recovery_contract import (
    OPERATION_NAME,
    RECOVERY_CAPABILITY,
    TerminalDisposition,
    TerminalRecoveryDecision,
    TerminalRecoveryRequest,
    TerminalRecoveryResultClass,
)


ROOT = Path(__file__).resolve().parents[1]
REPO_ID = str(uuid4())
REQUEST_ID = str(uuid4())
DECISION_ID = str(uuid4())
EVENT_ID = str(uuid4())
CONFLICT_ID = str(uuid4())
AUDIT_REF = "ad:01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _request(**changes):
    values = {
        "repo_id": REPO_ID,
        "claim_id": 17,
        "terminal_request_id": REQUEST_ID,
        "expected_lease_epoch": 3,
        "terminal_disposition": TerminalDisposition.CLAIM_RELEASE,
        "terminal_request_digest": "a" * 64,
        "recovery_capability_ref": "identity-grant:recovery-2026-07-29",
        "incident_audit_ref": AUDIT_REF,
        "operator_approval_audit_ref": AUDIT_REF,
    }
    values.update(changes)
    return TerminalRecoveryRequest(**values)


def test_terminal_recovery_freezes_lookup_only_token_free_request_identity():
    request = _request()

    assert OPERATION_NAME == "work.claim.recover-terminal/v1"
    assert RECOVERY_CAPABILITY == "work:claim-recovery"
    assert request.ledger_key == (REPO_ID, REQUEST_ID)
    assert "actor" not in request.__dataclass_fields__
    assert "claim_token" not in request.__dataclass_fields__


@pytest.mark.parametrize("field", ["claim_token", "token", "credential", "authorization"])
def test_terminal_recovery_rejects_secret_bearing_capability_reference(field):
    with pytest.raises(ValueError, match="secret-bearing"):
        _request(recovery_capability_ref=f"{field}:not-a-reference")


@pytest.mark.parametrize("audit_ref", ["incident:123", "ad:lowercase-not-a-ulid", "ad:01ARZ3NDEKTSV4RRFFQ69G5FAV:extra"])
def test_terminal_recovery_requires_auditctl_event_reference(audit_ref):
    with pytest.raises(ValueError, match="auditctl event reference"):
        _request(incident_audit_ref=audit_ref)


def test_terminal_recovery_result_classes_preserve_redaction_boundary():
    settled = TerminalRecoveryDecision(
        result="settled", decision_id=DECISION_ID, terminal_event_id=EVENT_ID,
        resulting_item_state="done",
    )
    assert settled.result is TerminalRecoveryResultClass.SETTLED

    conflict = TerminalRecoveryDecision(
        result="conflict", mismatch_class="request-digest-mismatch",
        conflicting_request_id=CONFLICT_ID,
    )
    assert conflict.conflicting_request_id == CONFLICT_ID

    with pytest.raises(ValueError, match="must not disclose terminal state"):
        TerminalRecoveryDecision(
            result="conflict", mismatch_class="epoch-mismatch",
            conflicting_request_id=CONFLICT_ID, resulting_item_state="done",
        )
    with pytest.raises(ValueError, match="must not include decision or conflict detail"):
        TerminalRecoveryDecision(result="unavailable", mismatch_class="identity-unavailable")


def test_terminal_recovery_context_is_complete_and_preserves_sidecar_separation():
    packet = json.loads((ROOT / "verification/contexts/terminal-claim-recovery.json").read_text())

    assert packet["schema_version"] == "test-context/v1"
    assert packet["depth"] == 2
    assert packet["source_of_truth"].startswith("immutable-terminal-request-decision-ledger")
    assert "immutable-ledger-lookup-precedes-current-claim-state-inspection" in packet["invariants"]
    assert "online-revocation-or-identity-lookup-failure-is-fail-closed" in packet["invariants"]
    assert "active-sidecar-claim-recover-remains-proof-only-and-is-not-terminal-recovery-authority" in packet["invariants"]
