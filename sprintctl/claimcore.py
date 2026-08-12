"""Shared claim-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`ClaimConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
claim-table behaviour. Mirrors the pattern established in ``sprintcore.py``.

Sub-increment 4a added pure helpers and read-only queries. Sub-increment 4b
(this addition) adds the heartbeat/release mutation shapes and their
rejection-event payload builders — the "single-row update/delete, no
admission arbitration" operations the architectural plan rated lower risk
than create_claim/handoff_claim. Those two stay in each backend's wrapper:
they involve backend-specific locking (SQLite's ``BEGIN IMMEDIATE`` vs
PostgreSQL's advisory + row locks) and a collision-retry loop.

Backend-neutral claim serialization already lives in ``sprintctl.rows``
(``serialize_claim``, ``claim_identity_status``, etc.) — this module only
owns the query shapes that produce the rows ``rows.serialize_claim``
consumes, plus the rejection-event payloads that were already 100% pure
Python (no conn dependency) but copy-pasted between db.py and pg.py.
Extracting them surfaced one real drift: pg.py's release_claim always
tagged its rejection event ``["claims", "coordination", "release"]``, while
db.py additionally used ``["claims", "coordination", "ambiguity", "legacy"]``
for legacy claims with no claim_token. This module converges on db.py's
more specific behavior (a bug fix, not a stylistic choice) — see
release_rejection_event.

Sub-increment 4c adds handoff_claim's mutation shape and payload builders.
It surfaced two more drifts, both fixed here:

- pg.py's handoff UPDATE never bumped ``lease_epoch`` on rotation/legacy
  adoption; db.py's didn't either, but pg.py's did (``lease_epoch =
  lease_epoch + CASE WHEN ... THEN 1 ELSE 0 END``). lease_epoch is a real
  fencing token consumed by terminal_recovery_server.py and authority.py
  to reject stale in-flight operations after a claim changes hands — not
  bumping it on SQLite meant a session holding a pre-handoff lease_epoch
  could still pass an expected_lease_epoch check post-handoff on that
  backend. handoff_update now bumps it on both backends, matching pg's
  (correct) prior behavior. No existing test exercised this on the SQLite
  path; see the added regression test in test_claims.py.
- pg.py's rejection-event ``attempted_by`` payloads (both the
  legacy-ambiguity and coordination-failure branches) carried only
  actor/claim_id/claim_token_present, dropping runtime_session_id,
  instance_id, branch, worktree_path, commit_sha, pr_ref, hostname, and pid
  that db.py's included — a real audit-trail completeness gap on the pg
  path. Converged on db.py's richer identity, since that's what an operator
  investigating a rejected handoff needs to see.
"""

from __future__ import annotations

import secrets
from typing import Protocol

from . import rows as _rows

CLAIM_TYPES = ("inspect", "execute", "review", "coordinate")


class ClaimConflict(ValueError):
    pass


def _generate_claim_token() -> str:
    return secrets.token_urlsafe(24)


class ClaimConn(Protocol):
    """Minimal storage handle a backend provides for claim-table operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    true_literal: str
    """SQL boolean-true literal for the ``exclusive`` column (``1``/``true``)."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_one(self, sql: str, params: tuple) -> dict | None: ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def mutate(self, sql: str, params: tuple) -> None: ...

    def now_sql(self) -> str:
        """SQL expression for 'current time' in an expires_at comparison.

        SQLite: ``strftime('%Y-%m-%dT%H:%M:%SZ','now')``. PostgreSQL:
        ``statement_timestamp()`` — deliberately not ``now()``, which would
        pin to transaction start on a long-lived served-mode connection and
        make lease-expiry comparisons wrong. Do not change this to ``now()``.
        """
        ...

    def expires_at_offset_sql(self) -> str:
        """SQL expression for 'now + N seconds', with one placeholder for N."""
        ...

    def join_tenant_clause(self, left_alias: str, right_alias: str) -> str:
        """Extra join condition scoping both sides to the same tenant.

        Empty on SQLite (no repo_id column); ``AND a.repo_id = b.repo_id`` on
        PostgreSQL, where rows from different repos could otherwise join.
        """
        ...


def _where(conn: ClaimConn, *conditions: str) -> str:
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def get_claim_row(conn: ClaimConn, claim_id: int) -> dict | None:
    sql = f"SELECT * FROM claim{_where(conn, f'id = {conn.ph}')}"
    return conn.query_one(sql, conn.tenant_params() + (claim_id,))


def get_active_exclusive_claim_row(conn: ClaimConn, work_item_id: int) -> dict | None:
    sql = (
        "SELECT * FROM claim"
        + _where(
            conn,
            f"work_item_id = {conn.ph}",
            f"exclusive = {conn.true_literal}",
            "status = 'active'",
            f"expires_at > {conn.now_sql()}",
        )
        + " ORDER BY created_at ASC LIMIT 1"
    )
    return conn.query_one(sql, conn.tenant_params() + (work_item_id,))


def get_active_coordinate_claim_row(conn: ClaimConn, work_item_id: int) -> dict | None:
    sql = (
        "SELECT * FROM claim"
        + _where(
            conn,
            f"work_item_id = {conn.ph}",
            f"exclusive = {conn.true_literal}",
            "status = 'active'",
            "claim_type = 'coordinate'",
            f"expires_at > {conn.now_sql()}",
        )
        + " ORDER BY created_at ASC LIMIT 1"
    )
    return conn.query_one(sql, conn.tenant_params() + (work_item_id,))


def list_claims_by_sprint(
    conn: ClaimConn,
    sprint_id: int,
    active_only: bool = True,
    expiring_within_seconds: int | None = None,
) -> list[dict]:
    """List all claims for items in a sprint, optionally filtered to active or expiring soon."""
    conditions = [f"wi.sprint_id = {conn.ph}"]
    params: list = [sprint_id]
    if active_only:
        conditions.append("c.status = 'active'")
        conditions.append(f"c.expires_at > {conn.now_sql()}")
    if expiring_within_seconds is not None:
        conditions.append(f"c.expires_at <= {conn.expires_at_offset_sql()}")
        params.append(expiring_within_seconds)

    tenant_where = f"c.repo_id = {conn.ph}" if conn.tenant_params() else None
    where_parts = ([tenant_where] if tenant_where else []) + conditions
    join_extra = conn.join_tenant_clause("c", "wi")

    sql = (
        "SELECT c.*, wi.title AS item_title, wi.status AS item_status"
        " FROM claim c"
        f" JOIN work_item wi ON c.work_item_id = wi.id{join_extra}"
        " WHERE " + " AND ".join(where_parts) +
        " ORDER BY c.expires_at ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + tuple(params))


def list_claims(conn: ClaimConn, work_item_id: int, active_only: bool = True) -> list[dict]:
    """List claims for a work item; active_only filters to non-expired claims."""
    conditions = [f"work_item_id = {conn.ph}"]
    if active_only:
        conditions.append("status = 'active'")
        conditions.append(f"expires_at > {conn.now_sql()}")
    sql = f"SELECT * FROM claim{_where(conn, *conditions)} ORDER BY created_at ASC"
    return conn.query_all(sql, conn.tenant_params() + (work_item_id,))


def find_claim_by_identity(
    conn: ClaimConn,
    *,
    instance_id: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
    runtime_session_id: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    """Find claims matching the given identity fields, most recent first.

    Useful for session resumption when the claim_token is lost but the agent
    knows its own instance_id, runtime_session_id, or hostname+pid.
    At least one of instance_id, runtime_session_id, or (hostname+pid) must be provided.
    """
    if not any([instance_id, runtime_session_id, (hostname and pid is not None)]):
        raise ValueError(
            "At least one of --instance-id, --runtime-session-id, or "
            "--hostname + --pid must be provided to resume a claim."
        )
    conditions: list[str] = []
    params: list = []
    if active_only:
        conditions.append("status = 'active'")
        conditions.append(f"expires_at > {conn.now_sql()}")
    if instance_id:
        conditions.append(f"instance_id = {conn.ph}")
        params.append(instance_id)
    if runtime_session_id:
        conditions.append(f"runtime_session_id = {conn.ph}")
        params.append(runtime_session_id)
    if hostname and pid is not None:
        conditions.append(f"(hostname = {conn.ph} AND pid = {conn.ph})")
        params.extend([hostname, pid])
    sql = f"SELECT * FROM claim{_where(conn, *conditions)} ORDER BY created_at DESC"
    return conn.query_all(sql, conn.tenant_params() + tuple(params))


def heartbeat_update(
    conn: ClaimConn,
    claim_id: int,
    ttl_seconds: int,
    runtime_session_id: str | None,
    instance_id: str | None,
    branch: str | None,
    worktree_path: str | None,
    commit_sha: str | None,
    pr_ref: str | None,
    hostname: str | None,
    pid: int | None,
) -> None:
    """Refresh a claim's expiry and heartbeat timestamp, and any supplied identity fields."""
    ph = conn.ph
    sql = (
        "UPDATE claim SET"
        f" heartbeat = {conn.now_sql()},"
        f" expires_at = {conn.expires_at_offset_sql()},"
        f" runtime_session_id = COALESCE({ph}, runtime_session_id),"
        f" instance_id = COALESCE({ph}, instance_id),"
        f" branch = COALESCE({ph}, branch),"
        f" worktree_path = COALESCE({ph}, worktree_path),"
        f" commit_sha = COALESCE({ph}, commit_sha),"
        f" pr_ref = COALESCE({ph}, pr_ref),"
        f" hostname = COALESCE({ph}, hostname),"
        f" pid = COALESCE({ph}, pid)"
        f"{_where(conn, f'id = {ph}')}"
    )
    params = (
        ttl_seconds,
        runtime_session_id,
        instance_id,
        branch,
        worktree_path,
        commit_sha,
        pr_ref,
        hostname,
        pid,
    ) + conn.tenant_params() + (claim_id,)
    conn.mutate(sql, params)


def release_delete(conn: ClaimConn, claim_id: int) -> None:
    """Delete a claim row. Caller has already verified claim proof."""
    sql = f"DELETE FROM claim{_where(conn, f'id = {conn.ph}')}"
    conn.mutate(sql, conn.tenant_params() + (claim_id,))


def heartbeat_rejection_event(
    claim_id: int,
    row: dict,
    detail: str,
    *,
    actor: str | None,
    claim_token: str | None,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> tuple[str, dict]:
    """Build the (event_type, payload) for a rejected heartbeat, given the ValueError detail."""
    is_legacy = not row["claim_token"]
    event_type = "claim-ambiguity-detected" if is_legacy else "coordination-failure"
    payload = {
        "summary": f"Claim heartbeat rejected for claim #{claim_id}",
        "detail": detail,
        "tags": ["claims", "coordination", "heartbeat"],
        "operation": "heartbeat",
        "reason": "legacy-ambiguous-claim" if is_legacy else "invalid-claim-proof",
        "claim": _rows.claim_event_identity(row),
        "attempted_by": _rows.claim_attempt_identity(
            actor=actor,
            claim_id=claim_id,
            claim_token_present=claim_token is not None,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
        ),
    }
    return event_type, payload


def release_rejection_event(
    claim_id: int,
    row: dict,
    detail: str,
    *,
    actor: str | None,
    claim_token: str | None,
) -> tuple[str, dict]:
    """Build the (event_type, payload) for a rejected release, given the ValueError detail.

    A legacy claim (no claim_token) gets the more specific ambiguity/legacy
    tags; see the module docstring for the pg.py drift this fixes.
    """
    is_legacy = not row["claim_token"]
    event_type = "claim-ambiguity-detected" if is_legacy else "coordination-failure"
    tags = (
        ["claims", "coordination", "ambiguity", "legacy"]
        if is_legacy
        else ["claims", "coordination", "release"]
    )
    payload = {
        "summary": f"Claim release rejected for claim #{claim_id}",
        "detail": detail,
        "tags": tags,
        "operation": "release",
        "reason": "legacy-ambiguous-claim" if is_legacy else "invalid-claim-proof",
        "claim": _rows.claim_event_identity(row),
        "attempted_by": _rows.claim_attempt_identity(
            actor=actor,
            claim_id=claim_id,
            claim_token_present=claim_token is not None,
        ),
    }
    return event_type, payload


def handoff_legacy_ambiguous_event(
    claim_id: int,
    row: dict,
    *,
    actor: str,
    claim_token: str | None,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> dict:
    """Payload for the 'legacy ambiguous claim, adoption not permitted' rejection."""
    return {
        "summary": f"Legacy claim ambiguity detected for claim #{claim_id}",
        "detail": (
            "An explicit handoff was attempted for a legacy claim without a "
            "claim_token. Re-run with legacy adoption enabled to mint a new proof."
        ),
        "tags": ["claims", "coordination", "ambiguity", "legacy"],
        "operation": "handoff",
        "reason": "legacy-ambiguous-claim",
        "claim": _rows.claim_event_identity(row),
        "attempted_by": _rows.claim_attempt_identity(
            actor=actor,
            claim_id=claim_id,
            claim_token_present=claim_token is not None,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
        ),
    }


def handoff_rejection_event(
    claim_id: int,
    row: dict,
    detail: str,
    *,
    actor: str,
    claim_token: str | None,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> dict:
    """Payload for a handoff rejected by an invalid claim proof."""
    return {
        "summary": f"Claim handoff rejected for claim #{claim_id}",
        "detail": detail,
        "tags": ["claims", "coordination", "handoff"],
        "operation": "handoff",
        "reason": "invalid-claim-proof",
        "claim": _rows.claim_event_identity(row),
        "attempted_by": _rows.claim_attempt_identity(
            actor=actor,
            claim_id=claim_id,
            claim_token_present=claim_token is not None,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
        ),
    }


def handoff_success_event(
    claim_id: int,
    actor: str,
    mode: str,
    *,
    legacy_ambiguous: bool,
    lost_proof_adopted: bool,
    note: str | None,
    from_identity: dict,
    to_identity: dict,
) -> tuple[str, dict]:
    """Payload for a completed handoff (rotate/transfer/legacy-adopt)."""
    event_type = "claim-ownership-corrected" if legacy_ambiguous else "claim-handoff"
    payload = {
        "summary": (
            f"Claim #{claim_id} ownership corrected"
            if legacy_ambiguous
            else f"Claim #{claim_id} handed off to {actor}"
        ),
        "detail": note
        or (
            "A legacy ambiguous claim was explicitly adopted and re-issued with a new token."
            if legacy_ambiguous
            else (
                "The previous proof was unavailable; explicit recovery adoption "
                "minted a replacement token."
                if lost_proof_adopted
                else f"Claim ownership was transferred with mode={mode}."
            )
        ),
        "tags": ["claims", "handoff", "coordination"],
        "operation": "handoff",
        "mode": mode,
        "legacy_adopted": legacy_ambiguous,
        "lost_proof_adopted": lost_proof_adopted,
        "token_rotated": mode == "rotate" or legacy_ambiguous,
        "from_identity": from_identity,
        "to_identity": to_identity,
    }
    return event_type, payload


def handoff_update(
    conn: ClaimConn,
    claim_id: int,
    actor: str,
    next_claim_token: str,
    ttl_seconds: int,
    runtime_session_id: str | None,
    instance_id: str | None,
    branch: str | None,
    worktree_path: str | None,
    commit_sha: str | None,
    pr_ref: str | None,
    hostname: str | None,
    pid: int | None,
    *,
    bump_lease_epoch: bool,
) -> None:
    """Rotate ownership/proof on a claim row, bumping lease_epoch when rotating.

    bump_lease_epoch must be True whenever the caller mints a new
    claim_token (mode == "rotate", or any legacy/lost-proof adoption) — a
    prior lease_epoch value must stop satisfying fencing checks once
    ownership proof changes hands. See the module docstring for the
    SQLite-side gap this fixes.
    """
    ph = conn.ph
    sql = (
        "UPDATE claim SET"
        f" agent = {ph},"
        f" claim_token = {ph},"
        f" lease_epoch = lease_epoch + CASE WHEN {ph} THEN 1 ELSE 0 END,"
        f" expires_at = {conn.expires_at_offset_sql()},"
        f" runtime_session_id = {ph},"
        f" instance_id = {ph},"
        f" branch = {ph},"
        f" worktree_path = {ph},"
        f" commit_sha = {ph},"
        f" pr_ref = {ph},"
        f" hostname = {ph},"
        f" pid = {ph},"
        f" heartbeat = {conn.now_sql()}"
        f"{_where(conn, f'id = {ph}')}"
    )
    params = (
        actor,
        next_claim_token,
        bump_lease_epoch,
        ttl_seconds,
        runtime_session_id,
        instance_id,
        branch,
        worktree_path,
        commit_sha,
        pr_ref,
        hostname,
        pid,
    ) + conn.tenant_params() + (claim_id,)
    conn.mutate(sql, params)
