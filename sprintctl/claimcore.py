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
  lease_epoch + CASE WHEN ... THEN 1 ELSE 0 END``). The archived legacy
  claim implementation keeps the epoch behavior aligned across both stores.
- pg.py's rejection-event ``attempted_by`` payloads (both the
  legacy-ambiguity and coordination-failure branches) carried only
  actor/claim_id/claim_token_present, dropping runtime_session_id,
  instance_id, branch, worktree_path, commit_sha, pr_ref, hostname, and pid
  that db.py's included — a real audit-trail completeness gap on the pg
  path. Converged on db.py's richer identity, since that's what an operator
  investigating a rejected handoff needs to see.

Sub-increment 4d adds the transaction-scaffold protocol members (mirroring
``workitemcore.WorkItemConn``'s begin_txn/commit/rollback/lock_for_update)
plus the remaining pure/query-shape helpers: get_active_maintenance_capability_row,
expire_stale_active_claims, evaluate_exclusivity_conflict, and insert_claim.
``require_claim_proof`` and ``MAX_CLAIM_TOKEN_INSERT_RETRIES`` move here from
db.py too (the same asymmetry fix as EditConflict/StatusConflict in
workitemcore.py): both backends import them as peers; db.py re-exports both
names (and its ``_require_claim_proof`` alias, which pg.py's own wrapper
still imports) for existing callers.

Sub-increment 4e uses that scaffold to unify create_claim and handoff_claim
themselves. The lock *order* is preserved exactly per backend -- this
extraction does not move or reorder any lock call, only relocates identical
SQL/logic that was already duplicated between db.py and pg.py.
``lock_work_item_row`` is now called unconditionally when a create is
exclusive, on both backends: PostgreSQL's call is load-bearing (the
``FOR UPDATE`` is the real arbitration point serializing exclusive admission
against other exclusive admissions on the same item); SQLite's is a
redundant-but-harmless existence check (``begin_txn``'s ``BEGIN IMMEDIATE``
already holds the whole-DB write lock) that was previously only performed
once, outside the transaction, before the retry loop started.
"""

from __future__ import annotations

import secrets
from typing import Literal, Protocol

from . import rows as _rows

CLAIM_TYPES = ("inspect", "execute", "review", "coordinate")
MAX_CLAIM_TOKEN_INSERT_RETRIES = 5


class ClaimConflict(ValueError):
    pass


def _generate_claim_token() -> str:
    return secrets.token_urlsafe(24)


def require_claim_proof(row: dict, claim_token: str | None) -> None:
    """Validate that claim_token proves ownership of an active, non-legacy claim."""
    if row["status"] != "active":
        raise ValueError(f"Claim #{row['id']} is {row['status']} and is no longer active")
    if not row["claim_token"]:
        raise ValueError(
            f"Claim #{row['id']} is a legacy ambiguous claim with no claim_token. "
            "Use explicit handoff to adopt it or wait for expiry."
        )
    if not claim_token:
        raise ValueError(f"Claim #{row['id']} requires --claim-token")
    if row["claim_token"] != claim_token:
        raise ValueError(f"Invalid claim_token for claim #{row['id']}")


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

    def begin_txn(self) -> None:
        """Start a serialized multi-statement transaction. See ``WorkItemConn.begin_txn``."""
        ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def execute(self, sql: str, params: tuple) -> None:
        """Run a statement inside the caller's open transaction, without committing."""
        ...

    def insert_row(self, sql: str, params: tuple) -> int:
        """Run an INSERT inside the caller's open transaction, returning the new id."""
        ...

    def lock_capability_arbitration(self) -> None:
        """Serialize repo-wide ordinary-claim admission with maintenance activation.

        No-op on SQLite (``begin_txn``'s whole-DB lock already covers it): a
        repo-scoped ``pg_advisory_xact_lock`` on PostgreSQL, where "at most
        one unexpired exclusive claim" cannot be expressed as a plain unique
        constraint.
        """
        ...

    def lock_work_item_row(self, work_item_id: int) -> dict | None:
        """Lock the work_item row for the rest of the transaction.

        A plain existence-check read on SQLite (``begin_txn``'s lock already
        covers it); ``SELECT ... FOR UPDATE`` on PostgreSQL. Returns None if
        no row matches (id or tenant mismatch).
        """
        ...

    def is_claim_token_collision(self, exc: BaseException) -> bool:
        """True if exc is a unique-constraint violation on claim_token."""
        ...

    def maintenance_capability_active_sql(self) -> str:
        """SQL condition matching an active/observing, unexpired maintenance capability."""
        ...

    def emit_claim_event(
        self, claim_row: dict, *, event_type: str, actor: str, payload: dict
    ) -> None:
        """Look up the claim's work item and append an event."""
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


def get_active_maintenance_capability_row(conn: ClaimConn) -> dict | None:
    """Return an active/observing, unexpired maintenance capability row, if any.

    Ordinary claims are disabled while such a capability is active; see
    ``create_claim``.
    """
    sql = (
        "SELECT capability_id FROM maintenance_capability"
        + _where(conn, conn.maintenance_capability_active_sql())
        + " LIMIT 1"
    )
    return conn.query_one(sql, conn.tenant_params())


def expire_stale_active_claims(conn: ClaimConn, work_item_id: int) -> None:
    """Close every elapsed active claim on a work item before admission checks run.

    Expiry is projected lazily elsewhere, but reacquisition is an authority
    boundary: the caller must run this inside its reserved write transaction
    so retained history and a newly granted lease agree.
    """
    ph = conn.ph
    sql = "UPDATE claim SET status = 'expired'" + _where(
        conn, f"work_item_id = {ph}", "status = 'active'", f"expires_at <= {conn.now_sql()}"
    )
    conn.execute(sql, conn.tenant_params() + (work_item_id,))


def evaluate_exclusivity_conflict(
    conflict: dict | None, coordinate_claim_id: int | None
) -> Literal["none", "delegate", "conflict"]:
    """Classify an active exclusive claim against a would-be sub-agent claim.

    "delegate" only when the conflicting claim IS the coordinate claim the
    caller says it's claiming under; the caller must still prove ownership
    of that coordinate claim (see ``create_claim``) before the delegated
    claim is admitted.
    """
    if conflict is None:
        return "none"
    if (
        conflict["claim_type"] == "coordinate"
        and coordinate_claim_id is not None
        and coordinate_claim_id == conflict["id"]
    ):
        return "delegate"
    return "conflict"


def insert_claim(
    conn: ClaimConn,
    work_item_id: int,
    agent: str,
    claim_type: str,
    exclusive: bool,
    ttl_seconds: int,
    branch: str | None,
    worktree_path: str | None,
    commit_sha: str | None,
    pr_ref: str | None,
    claim_token: str,
    runtime_session_id: str | None,
    instance_id: str | None,
    hostname: str | None,
    pid: int | None,
) -> int:
    ph = conn.ph
    tenant_cols = "repo_id, " if conn.tenant_params() else ""
    tenant_phs = f"{ph}, " if conn.tenant_params() else ""
    lease_where = _where(conn, f"work_item_id = {ph}", "status = 'expired'")
    sql = (
        f"INSERT INTO claim ({tenant_cols}work_item_id, agent, claim_type, exclusive, expires_at,"
        " branch, worktree_path, commit_sha, pr_ref,"
        " claim_token, runtime_session_id, instance_id, hostname, pid,"
        " lease_epoch)"
        f" VALUES ({tenant_phs}{ph}, {ph}, {ph}, {ph}, {conn.expires_at_offset_sql()},"
        f" {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph},"
        f" COALESCE((SELECT MAX(lease_epoch) FROM claim{lease_where}), 0) + 1)"
    )
    params = (
        conn.tenant_params()
        + (
            work_item_id, agent, claim_type, exclusive, ttl_seconds,
            branch, worktree_path, commit_sha, pr_ref,
            claim_token, runtime_session_id, instance_id, hostname, pid,
        )
        + conn.tenant_params() + (work_item_id,)
    )
    return conn.insert_row(sql, params)


def create_claim(
    conn: ClaimConn,
    work_item_id: int,
    agent: str,
    claim_type: str,
    exclusive: bool,
    ttl_seconds: int,
    branch: str | None,
    worktree_path: str | None,
    commit_sha: str | None,
    pr_ref: str | None,
    runtime_session_id: str | None,
    instance_id: str | None,
    hostname: str | None,
    pid: int | None,
    coordinate_claim_id: int | None,
    coordinate_claim_token: str | None,
) -> int:
    """Admit a new claim, enforcing exclusivity and maintenance-capability gating.

    Caller must have already validated claim_type and confirmed the work
    item exists (see the module docstring for why that stays backend-side).
    Retries on a claim_token collision -- astronomically rare, but each
    attempt draws a fresh random token.
    """
    for attempt in range(MAX_CLAIM_TOKEN_INSERT_RETRIES):
        claim_token = _generate_claim_token()
        try:
            conn.begin_txn()
            conn.lock_capability_arbitration()
            if get_active_maintenance_capability_row(conn) is not None:
                raise ClaimConflict(
                    "ordinary claims are disabled while an exact-plan maintenance capability is active"
                )
            if exclusive:
                if conn.lock_work_item_row(work_item_id) is None:
                    raise ValueError(f"Work item #{work_item_id} not found")
            # Expiry is projected lazily, but reacquisition is an authority
            # boundary: close every elapsed row before checking conflicts so
            # retained history and the newly granted lease agree.  Keep this
            # inside the reserved write transaction to match PostgreSQL's
            # work-item-row arbitration.
            expire_stale_active_claims(conn, work_item_id)
            if exclusive:
                conflict = get_active_exclusive_claim_row(conn, work_item_id)
                disposition = evaluate_exclusivity_conflict(conflict, coordinate_claim_id)
                if disposition == "conflict":
                    raise ClaimConflict(
                        f"Item #{work_item_id} is exclusively claimed by "
                        f"'{conflict['agent']}' (claim #{conflict['id']})"
                    )
                if disposition == "delegate":
                    coord_row = get_claim_row(conn, coordinate_claim_id)
                    if coord_row is None:
                        raise ValueError(f"Coordinate claim #{coordinate_claim_id} not found")
                    require_claim_proof(coord_row, coordinate_claim_token)
            claim_id = insert_claim(
                conn, work_item_id, agent, claim_type, exclusive, ttl_seconds,
                branch, worktree_path, commit_sha, pr_ref, claim_token,
                runtime_session_id, instance_id, hostname, pid,
            )
            conn.commit()
            return claim_id
        except ClaimConflict:
            # The repository arbitration lock is transaction-scoped.  A normal
            # rejected admission must close its transaction before control
            # returns to a caller that may retain and reuse this connection.
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            if conn.is_claim_token_collision(exc):
                if attempt == MAX_CLAIM_TOKEN_INSERT_RETRIES - 1:
                    raise RuntimeError(
                        "Failed to create claim: could not generate a unique claim token."
                    ) from exc
                continue
            raise
    raise RuntimeError("Unreachable")


def handoff_claim(
    conn: ClaimConn,
    claim_id: int,
    claim_token: str | None,
    *,
    actor: str,
    mode: str = "rotate",
    ttl_seconds: int = 300,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
    performed_by: str | None = None,
    note: str | None = None,
    allow_legacy_adopt: bool = False,
) -> dict:
    row = get_claim_row(conn, claim_id)
    if row is None:
        raise ValueError(f"Claim #{claim_id} not found")
    if mode not in {"transfer", "rotate"}:
        raise ValueError("mode must be 'transfer' or 'rotate'")

    legacy_ambiguous = not bool(row["claim_token"])
    # A token is deliberately unrecoverable from a remote claim row.  When a
    # session has lost its locally persisted proof, the documented escape hatch
    # is an explicit, auditable adoption.  Only an omitted token may take this
    # path: a supplied-but-invalid token remains a rejected proof attempt.
    lost_proof_adopted = (
        not legacy_ambiguous
        and allow_legacy_adopt
        and claim_token is None
    )
    identity_kwargs = dict(
        runtime_session_id=runtime_session_id,
        instance_id=instance_id,
        branch=branch,
        worktree_path=worktree_path,
        commit_sha=commit_sha,
        pr_ref=pr_ref,
        hostname=hostname,
        pid=pid,
    )
    if legacy_ambiguous:
        if not allow_legacy_adopt:
            payload = handoff_legacy_ambiguous_event(
                claim_id,
                row,
                actor=performed_by or actor,
                claim_token=claim_token,
                **identity_kwargs,
            )
            conn.emit_claim_event(
                row, event_type="claim-ambiguity-detected",
                actor=performed_by or actor, payload=payload,
            )
            raise ValueError(
                f"Claim #{claim_id} is a legacy ambiguous claim with no claim_token. "
                "Use allow_legacy_adopt to mint a new ownership proof."
            )
        mode = "rotate"
    elif not lost_proof_adopted:
        try:
            require_claim_proof(row, claim_token)
        except ValueError as exc:
            payload = handoff_rejection_event(
                claim_id,
                row,
                str(exc),
                actor=performed_by or actor,
                claim_token=claim_token,
                **identity_kwargs,
            )
            conn.emit_claim_event(
                row, event_type="coordination-failure",
                actor=performed_by or actor, payload=payload,
            )
            raise

    from_identity = _rows.claim_event_identity(row)
    next_claim_token = row["claim_token"]
    bump_lease_epoch = mode == "rotate" or not next_claim_token
    if mode == "rotate" or not next_claim_token:
        next_claim_token = _generate_claim_token()

    handoff_update(
        conn,
        claim_id,
        actor,
        next_claim_token,
        ttl_seconds,
        runtime_session_id,
        instance_id,
        branch,
        worktree_path,
        commit_sha,
        pr_ref,
        hostname,
        pid,
        bump_lease_epoch=bump_lease_epoch,
    )

    updated_row = get_claim_row(conn, claim_id)
    assert updated_row is not None
    event_type, payload = handoff_success_event(
        claim_id,
        actor,
        mode,
        legacy_ambiguous=legacy_ambiguous,
        lost_proof_adopted=lost_proof_adopted,
        note=note,
        from_identity=from_identity,
        to_identity=_rows.claim_event_identity(updated_row),
    )
    conn.emit_claim_event(updated_row, event_type=event_type, actor=performed_by or actor, payload=payload)
    return _rows.serialize_claim(updated_row, include_secret=True)


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
