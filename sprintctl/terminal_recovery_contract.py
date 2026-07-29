"""Frozen, transport-free contract for privileged terminal-claim recovery.

This module deliberately does not register a served operation, open a store,
or verify a credential. It gives a future adapter and identity deployment one
token-free request/result boundary to implement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Protocol
from uuid import UUID


OPERATION_NAME = "work.claim.recover-terminal/v1"
RECOVERY_CAPABILITY = "work:claim-recovery"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_REF = re.compile(r"^ad:[0-9A-HJKMNP-TV-Z]{26}$")
_SECRET_NAMES = frozenset({
    "claim_token", "token", "credential", "secret", "password", "api_key",
    "access_token", "authorization", "private_key",
})


def _uuid(value: str, field: str) -> str:
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if value != canonical:
        raise ValueError(f"{field} must be a canonical UUID")
    return canonical


def _positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


def _audit_ref(value: str, field: str) -> str:
    if not isinstance(value, str) or not _AUDIT_REF.fullmatch(value):
        raise ValueError(f"{field} must be an auditctl event reference (ad:<ULID>)")
    return value


def _opaque_reference(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty opaque reference")
    if any(secret in value.lower().replace("-", "_") for secret in _SECRET_NAMES):
        raise ValueError(f"{field} must not contain a secret-bearing reference")
    return value


class TerminalDisposition(StrEnum):
    CLAIM_RELEASE = "claim.release"
    ITEM_DONE_FROM_CLAIM = "item.done-from-claim"
    ITEM_TRANSITION_DONE = "item.transition.done"
    ITEM_TRANSITION_BLOCKED = "item.transition.blocked"


class TerminalRecoveryResultClass(StrEnum):
    SETTLED = "settled"
    NOT_SETTLED = "not-settled"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TerminalRecoveryRequest:
    """Lookup-only recovery request; raw proof and worker identity are absent."""

    repo_id: str
    claim_id: int
    terminal_request_id: str
    expected_lease_epoch: int
    terminal_disposition: TerminalDisposition
    terminal_request_digest: str
    recovery_capability_ref: str
    incident_audit_ref: str
    operator_approval_audit_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_id", _uuid(self.repo_id, "repo_id"))
        object.__setattr__(self, "claim_id", _positive_int(self.claim_id, "claim_id"))
        object.__setattr__(self, "terminal_request_id", _uuid(self.terminal_request_id, "terminal_request_id"))
        object.__setattr__(self, "expected_lease_epoch", _positive_int(self.expected_lease_epoch, "expected_lease_epoch"))
        object.__setattr__(self, "terminal_disposition", TerminalDisposition(self.terminal_disposition))
        object.__setattr__(self, "terminal_request_digest", _sha256(self.terminal_request_digest, "terminal_request_digest"))
        object.__setattr__(self, "recovery_capability_ref", _opaque_reference(self.recovery_capability_ref, "recovery_capability_ref"))
        object.__setattr__(self, "incident_audit_ref", _audit_ref(self.incident_audit_ref, "incident_audit_ref"))
        object.__setattr__(self, "operator_approval_audit_ref", _audit_ref(self.operator_approval_audit_ref, "operator_approval_audit_ref"))

    @property
    def ledger_key(self) -> tuple[str, str]:
        """The immutable request identity consulted before current claim state."""
        return (self.repo_id, self.terminal_request_id)


@dataclass(frozen=True, slots=True)
class TerminalRecoveryDecision:
    """Token-free result shape. Conflict disclosure is deliberately narrow."""

    result: TerminalRecoveryResultClass
    decision_id: str | None = None
    terminal_event_id: str | None = None
    resulting_item_state: str | None = None
    mismatch_class: str | None = None
    conflicting_request_id: str | None = None

    def __post_init__(self) -> None:
        result = TerminalRecoveryResultClass(self.result)
        object.__setattr__(self, "result", result)
        for field in ("decision_id", "terminal_event_id", "conflicting_request_id"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _uuid(value, field))
        if self.resulting_item_state is not None and self.resulting_item_state not in {"pending", "active", "done", "blocked"}:
            raise ValueError("resulting_item_state must be a sprintctl item status")
        if result is TerminalRecoveryResultClass.SETTLED:
            if None in (self.decision_id, self.terminal_event_id, self.resulting_item_state):
                raise ValueError("settled requires decision_id, terminal_event_id, and resulting_item_state")
            if self.mismatch_class is not None or self.conflicting_request_id is not None:
                raise ValueError("settled must not disclose conflict fields")
        elif result is TerminalRecoveryResultClass.CONFLICT:
            if not self.mismatch_class or self.conflicting_request_id is None:
                raise ValueError("conflict requires mismatch_class and conflicting_request_id")
            if any((self.decision_id, self.terminal_event_id, self.resulting_item_state)):
                raise ValueError("conflict must not disclose terminal state")
        elif any((self.decision_id, self.terminal_event_id, self.resulting_item_state, self.mismatch_class, self.conflicting_request_id)):
            raise ValueError(f"{result.value} must not include decision or conflict detail")


@dataclass(frozen=True, slots=True)
class VerifiedRecoveryCapability:
    """The deployed identity authority returns this after online verification."""

    capability_ref: str
    subject_id: str
    repo_id: str
    claim_id: int
    terminal_request_id: str
    terminal_disposition: TerminalDisposition
    expected_lease_epoch: int


class RecoveryCapabilityVerifier(Protocol):
    """Identity boundary required from deployment; lookup/revocation failures deny."""

    def verify_online(self, request: TerminalRecoveryRequest) -> VerifiedRecoveryCapability:
        """Validate scope, expiry, and revocation against the deployed authority.

        Implementations fail closed on authority/revocation lookup failure and do
        not return raw credentials, claim proof, or provider secrets.
        """
