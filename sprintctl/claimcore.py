"""Shared claim-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`ClaimConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
claim-table behaviour. Mirrors the pattern established in ``sprintcore.py``.

This is sub-increment 4a of the Claim extraction (see the architectural
plan for WorkItem/Event/Claim unification): pure helpers and read-only
queries only. ``create_claim``, ``heartbeat_claim``, ``release_claim``, and
``handoff_claim`` stay in each backend's wrapper — they involve
backend-specific locking (SQLite's ``BEGIN IMMEDIATE`` vs PostgreSQL's
advisory + row locks) and a collision-retry loop that later sub-increments
will address individually.

Backend-neutral claim serialization already lives in ``sprintctl.rows``
(``serialize_claim``, ``claim_identity_status``, etc.) — this module only
owns the query shapes that produce the rows ``rows.serialize_claim``
consumes.
"""

from __future__ import annotations

import secrets
from typing import Protocol

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
