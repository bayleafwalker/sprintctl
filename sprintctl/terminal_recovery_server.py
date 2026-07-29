"""Server-side, PostgreSQL-only semantics for terminal claim recovery.

This is deliberately not a CLI surface.  Composition must supply an online
identity/revocation verifier and explicitly register the returned adapter.
Without that dependency the operation is unavailable before any store I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .terminal_recovery_contract import (
    OPERATION_NAME,
    RecoveryCapabilityVerifier,
    TerminalDisposition,
    TerminalRecoveryDecision,
    TerminalRecoveryMismatchClass as Mismatch,
    TerminalRecoveryRequest,
    TerminalRecoveryResultClass as Result,
    require_authenticated_coordinator_principal,
    require_verified_capability_scope,
)


_AUDIT_NAMESPACE = uuid5(NAMESPACE_URL, "sprintctl:terminal-recovery-audit/v1")


def _audit_id(request: TerminalRecoveryRequest) -> str:
    return str(uuid5(_AUDIT_NAMESPACE, f"{request.repo_id}:{request.terminal_request_id}"))


def _conflict(request_id: str, mismatch: Mismatch) -> TerminalRecoveryDecision:
    return TerminalRecoveryDecision(
        result=Result.CONFLICT,
        mismatch_class=mismatch,
        conflicting_request_id=request_id,
    )


@dataclass(slots=True)
class PostgresTerminalRecoveryAdapter:
    """Injected served adapter; no credential, token, or local fallback exists."""

    store: Any
    verifier: RecoveryCapabilityVerifier

    def recover_terminal(
        self,
        request: TerminalRecoveryRequest,
        *,
        authenticated_coordinator_principal: str,
    ) -> TerminalRecoveryDecision:
        # This order is security-significant.  Do not move an I/O operation
        # above these three calls: verifier outages and bad bindings deny before
        # the recovery ledger or claim store is observable.
        try:
            verified = self.verifier.verify_online(request)
            require_verified_capability_scope(request, verified)
            require_authenticated_coordinator_principal(
                authenticated_coordinator_principal, verified
            )
        except Exception:
            return TerminalRecoveryDecision(result=Result.UNAVAILABLE)

        with self.store.conn.cursor() as cur:
            # Immutable historical settlement always wins, even after a later
            # claim lineage change.  This is intentionally the first database
            # statement in the operation.
            cur.execute(
                "SELECT * FROM terminal_recovery_ledger "
                "WHERE repo_id = %s AND terminal_request_id = %s",
                request.ledger_key,
            )
            row = cur.fetchone()
            if row is None:
                # The unrecorded path is lookup-only.  A current active claim
                # is never evidence to invent a terminal settlement; it is a
                # typed conflict.  Absence is likewise not-settled, permitting
                # only a separately authorized normal operation.
                cur.execute(
                    "SELECT id, lease_epoch, status, expires_at FROM claim "
                    "WHERE repo_id = %s AND id = %s FOR SHARE",
                    (request.repo_id, request.claim_id),
                )
                claim = cur.fetchone()
            else:
                claim = None

        if row is not None:
            decision = self._historical_decision(request, dict(row))
            if decision.result is Result.SETTLED:
                # A unique key makes duplicate/lost-response recovery evidence
                # deterministic and at-most-once.
                try:
                    append_recovery_audit(
                        self.store,
                        request,
                        coordinator_subject=verified.subject_id,
                    )
                    self.store.conn.commit()
                except Exception:
                    self.store.conn.rollback()
                    return TerminalRecoveryDecision(result=Result.UNAVAILABLE)
            return decision

        if claim is None:
            return TerminalRecoveryDecision(result=Result.NOT_SETTLED)
        claim = dict(claim)
        if int(claim["lease_epoch"]) != request.expected_lease_epoch:
            return _conflict(request.terminal_request_id, Mismatch.LEASE_EPOCH)
        if claim["status"] == "active":
            return _conflict(request.terminal_request_id, Mismatch.ACTIVE_CLAIM)
        return _conflict(request.terminal_request_id, Mismatch.SUPERSEDED_CLAIM)

    @staticmethod
    def _historical_decision(
        request: TerminalRecoveryRequest, row: dict[str, Any]
    ) -> TerminalRecoveryDecision:
        if int(row["claim_id"]) != request.claim_id:
            return _conflict(request.terminal_request_id, Mismatch.CLAIM)
        if int(row["lease_epoch"]) != request.expected_lease_epoch:
            return _conflict(request.terminal_request_id, Mismatch.LEASE_EPOCH)
        if row["terminal_disposition"] != request.terminal_disposition.value:
            return _conflict(request.terminal_request_id, Mismatch.DISPOSITION)
        if row["terminal_request_digest"] != request.terminal_request_digest:
            return _conflict(request.terminal_request_id, Mismatch.REQUEST_DIGEST)
        return TerminalRecoveryDecision(
            result=Result.SETTLED,
            decision_id=str(row["decision_id"]),
            terminal_event_id=str(row["terminal_event_id"]),
            resulting_item_state=row["resulting_item_state"],
        )


def append_terminal_settlement(
    store: Any,
    request: TerminalRecoveryRequest,
    *,
    decision_id: str,
    terminal_event_id: str,
    resulting_item_state: str,
) -> None:
    """Append one original terminal settlement; conflicting rewrites fail.

    Normal terminal settlement integration calls this in the same transaction
    as its durable decision.  It accepts only opaque IDs and never reads a
    claim proof.
    """
    UUID(decision_id)
    UUID(terminal_event_id)
    cur = store.conn.cursor()
    try:
        append_terminal_settlement_row(
            cur,
            request=request,
            decision_id=decision_id,
            terminal_event_id=terminal_event_id,
            resulting_item_state=resulting_item_state,
        )
    finally:
        cur.close()


def append_terminal_settlement_row(
    cur: Any,
    *,
    request: TerminalRecoveryRequest,
    decision_id: str,
    terminal_event_id: str,
    resulting_item_state: str,
) -> None:
    """Transaction-owned variant used by the normal authority arbiter."""
    UUID(decision_id)
    UUID(terminal_event_id)
    cur.execute(
        """
            INSERT INTO terminal_recovery_ledger (
                repo_id, terminal_request_id, claim_id, lease_epoch,
                terminal_disposition, terminal_request_digest, decision_id,
                terminal_event_id, resulting_item_state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo_id, terminal_request_id) DO NOTHING
        """,
        (request.repo_id, request.terminal_request_id, request.claim_id,
         request.expected_lease_epoch, request.terminal_disposition.value,
         request.terminal_request_digest, decision_id, terminal_event_id,
         resulting_item_state),
    )
    if cur.rowcount == 0:
        cur.execute(
            "SELECT claim_id, lease_epoch, terminal_disposition, terminal_request_digest, "
            "decision_id, terminal_event_id, resulting_item_state "
            "FROM terminal_recovery_ledger WHERE repo_id = %s AND terminal_request_id = %s",
            request.ledger_key,
        )
        existing = dict(cur.fetchone())
        expected = (request.claim_id, request.expected_lease_epoch,
                    request.terminal_disposition.value, request.terminal_request_digest,
                    decision_id, terminal_event_id, resulting_item_state)
        actual = tuple(existing[name] for name in (
            "claim_id", "lease_epoch", "terminal_disposition", "terminal_request_digest",
            "decision_id", "terminal_event_id", "resulting_item_state"))
        if actual != expected:
            raise ValueError("terminal recovery ledger record is immutable")


def append_terminal_settlement_from_authority(
    cur: Any,
    *,
    repo_id: str,
    claim_id: int,
    lease_epoch: int,
    terminal_request_id: str,
    terminal_disposition: TerminalDisposition,
    terminal_request_digest: str,
    decision_id: str,
    terminal_event_id: str,
    resulting_item_state: str,
) -> None:
    """Bind a normal authority settlement to the recovery ledger in its tx.

    Capability and approval references belong exclusively to a later recovery
    invocation and are deliberately not invented for the original settlement.
    The contract object is used here only to validate its immutable identity.
    """
    request = TerminalRecoveryRequest(
        repo_id=repo_id,
        claim_id=claim_id,
        terminal_request_id=terminal_request_id,
        expected_lease_epoch=lease_epoch,
        terminal_disposition=terminal_disposition,
        terminal_request_digest=terminal_request_digest,
        recovery_capability_ref=f"capref:{terminal_request_id}",
        incident_audit_ref="ad:01ARZ3NDEKTSV4RRFFQ69G5FAV",
        operator_approval_audit_ref="ad:01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    append_terminal_settlement_row(
        cur,
        request=request,
        decision_id=decision_id,
        terminal_event_id=terminal_event_id,
        resulting_item_state=resulting_item_state,
    )


def append_recovery_audit(
    store: Any,
    request: TerminalRecoveryRequest,
    *,
    coordinator_subject: str,
) -> str:
    """Append the deterministic, at-most-once audit linkage for a recovery."""
    audit_id = _audit_id(request)
    cur = store.conn.cursor()
    try:
        return append_recovery_audit_row(
            cur,
            request=request,
            coordinator_subject=coordinator_subject,
        )
    finally:
        cur.close()


def append_recovery_audit_row(
    cur: Any,
    *,
    request: TerminalRecoveryRequest,
    coordinator_subject: str,
) -> str:
    """Transaction-owned at-most-once audit insertion."""
    audit_id = _audit_id(request)
    cur.execute(
        """
            INSERT INTO terminal_recovery_audit (
                repo_id, terminal_request_id, audit_event_id, recovery_capability_ref,
                coordinator_subject, incident_audit_ref, operator_approval_audit_ref
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (repo_id, terminal_request_id) DO NOTHING
            """,
        (request.repo_id, request.terminal_request_id, audit_id,
         request.recovery_capability_ref, coordinator_subject,
         request.incident_audit_ref, request.operator_approval_audit_ref),
    )
    return audit_id


def configured_terminal_recovery_adapter(
    store: Any, verifier: RecoveryCapabilityVerifier | None
) -> PostgresTerminalRecoveryAdapter | None:
    """Composition registry hook: absent verification means absent operation."""
    if verifier is None or not callable(getattr(verifier, "verify_online", None)):
        return None
    return PostgresTerminalRecoveryAdapter(store=store, verifier=verifier)


__all__ = [
    "OPERATION_NAME",
    "PostgresTerminalRecoveryAdapter",
    "append_recovery_audit",
    "append_recovery_audit_row",
    "append_terminal_settlement",
    "append_terminal_settlement_from_authority",
    "append_terminal_settlement_row",
    "configured_terminal_recovery_adapter",
]
