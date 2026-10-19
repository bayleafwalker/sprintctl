"""Shared dep-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`DepConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
dep-table behaviour. Mirrors the pattern established in ``sprintcore.py``.

Private admission policy uses a backend-supplied work-item lookup. Each
backend retains lookup binding, connection adaptation and transaction behavior.
This module also owns the raw storage shape and the private read-only graph
projection over backend-selected eligible rows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .workitemcore import effective_priority


class DepConn(Protocol):
    """Minimal storage handle a backend provides for dep-table operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_one(self, sql: str, params: tuple) -> dict | None: ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def mutate(self, sql: str, params: tuple) -> None: ...

    def insert_ignore(
        self, columns: tuple[str, ...], values: tuple, conflict_cols: tuple[str, ...]
    ) -> None:
        """Insert a dep row, doing nothing on a conflicting unique key."""
        ...

    def join_tenant_clause(self, left_alias: str, right_alias: str) -> str:
        """Extra join condition scoping both sides to the same tenant.

        Empty on SQLite (no repo_id column); ``AND a.repo_id = b.repo_id`` on
        PostgreSQL, where rows from different repos could otherwise join.
        """
        ...


def _where(conn: DepConn, *conditions: str, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"{prefix}repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def _validate_add_dep(
    item_id: int,
    blocked_item_id: int,
    *,
    get_item: Callable[[int], object | None],
) -> None:
    """Preserve admission order using the caller's current backend lookup."""
    if item_id == blocked_item_id:
        raise ValueError("An item cannot depend on itself")
    if get_item(item_id) is None:
        raise ValueError(f"Work item #{item_id} not found")
    if get_item(blocked_item_id) is None:
        raise ValueError(f"Work item #{blocked_item_id} not found")


def add_dep(conn: DepConn, item_id: int, blocked_item_id: int) -> int:
    """Record that item_id must be done before blocked_item_id can start.

    Callers must already have validated both items exist and are distinct.
    Duplicate deps are ignored (returns existing dep id).
    """
    columns = ("item_id", "blocked_item_id")
    values = (item_id, blocked_item_id)
    conflict_cols = ("item_id", "blocked_item_id")
    if conn.tenant_params():
        columns = ("repo_id",) + columns
        values = conn.tenant_params() + values
        conflict_cols = ("repo_id",) + conflict_cols
    conn.insert_ignore(columns, values, conflict_cols)

    sql = (
        "SELECT id FROM dep"
        f"{_where(conn, f'item_id = {conn.ph}', f'blocked_item_id = {conn.ph}')}"
    )
    row = conn.query_one(sql, conn.tenant_params() + (item_id, blocked_item_id))
    return row["id"]


def list_deps_blocking(conn: DepConn, item_id: int) -> list[dict]:
    """Return deps where item_id is blocked — i.e. items that must complete first."""
    join_extra = conn.join_tenant_clause("d", "wi")
    sql = (
        "SELECT d.id, d.item_id, d.blocked_item_id, d.created_at,"
        " wi.title AS blocker_title, wi.status AS blocker_status"
        " FROM dep d"
        f" JOIN work_item wi ON d.item_id = wi.id{join_extra}"
        f"{_where(conn, f'd.blocked_item_id = {conn.ph}', alias='d')}"
        " ORDER BY d.created_at ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + (item_id,))


def list_deps_blocked_by(conn: DepConn, item_id: int) -> list[dict]:
    """Return deps where item_id is the blocker — i.e. items waiting on it."""
    join_extra = conn.join_tenant_clause("d", "wi")
    sql = (
        "SELECT d.id, d.item_id, d.blocked_item_id, d.created_at,"
        " wi.title AS waiting_title, wi.status AS waiting_status"
        " FROM dep d"
        f" JOIN work_item wi ON d.blocked_item_id = wi.id{join_extra}"
        f"{_where(conn, f'd.item_id = {conn.ph}', alias='d')}"
        " ORDER BY d.created_at ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + (item_id,))


def remove_dep(conn: DepConn, dep_id: int, item_id: int) -> None:
    """Remove a dep. item_id must match either item_id or blocked_item_id."""
    sql = (
        "SELECT id FROM dep"
        f"{_where(conn, f'id = {conn.ph}', f'(item_id = {conn.ph} OR blocked_item_id = {conn.ph})')}"
    )
    row = conn.query_one(sql, conn.tenant_params() + (dep_id, item_id, item_id))
    if row is None:
        raise ValueError(f"Dep #{dep_id} not found for item #{item_id}")
    sql = f"DELETE FROM dep{_where(conn, f'id = {conn.ph}')}"
    conn.mutate(sql, conn.tenant_params() + (dep_id,))


def _ready_items(items: list[dict], blockers_for: Callable[[int], list[dict]]) -> list[dict]:
    """Project graph readiness from backend-selected eligible pending items.

    Selection, hosted parking, SQL normalization and read transactions remain
    backend-owned. This graph report does not authorize dispatch or effects.
    A done blocker resolves an edge regardless of its terminal resolution.
    """
    ready = []
    for item in items:
        blockers = blockers_for(item["id"])
        unresolved = [b for b in blockers if b["blocker_status"] != "done"]
        if not unresolved:
            ready.append({
                **item,
                "blockers_resolved": len(blockers),
                "unresolved_blockers": 0,
                "effective_priority": effective_priority(item),
            })
    ready.sort(key=lambda item: (
        item["effective_priority"] is None,
        item["effective_priority"] or 0,
        item["created_at"],
        item["id"],
    ))
    return ready
