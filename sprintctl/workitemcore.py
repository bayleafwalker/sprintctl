"""Shared work-item storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`WorkItemConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
work-item behaviour. Mirrors the pattern established in ``sprintcore.py``.

Also owns the pure, backend-agnostic validators that used to live in
``db.py`` and be imported into ``pg.py`` — that made ``pg.py`` depend on
``db.py`` for logic with no SQLite dependency at all. They live here now so
both backends are peers.

Only the CRUD-shaped work-item operations are covered here. The CAS/
conflict-detection operations (``update_work_item_description``,
``set_work_item_status``) stay in each backend's wrapper: they span
multi-statement transactions with backend-specific locking (SQLite's
``BEGIN IMMEDIATE`` vs PostgreSQL row/advisory locks) that this module does
not attempt to abstract over.
"""

from __future__ import annotations

import re
from typing import Protocol
from uuid import uuid4

PRIORITY_MIN = 1
PRIORITY_MAX = 9

_PRIORITY_TITLE_RE = re.compile(r"^\[p([1-9])\]\s")


def validate_work_item_description(description: str) -> str:
    """Validate a description supplied through the item mutation surface."""
    if not isinstance(description, str) or not description.strip():
        raise ValueError("work item description must contain non-whitespace text")
    if "\x00" in description:
        raise ValueError("work item description must not contain NUL bytes")
    return description


def validate_priority(priority: int | None) -> int | None:
    """Validate a work item priority: None (unprioritized) or an int in 1..9."""
    if priority is None:
        return None
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("priority must be an integer between 1 and 9")
    if not (PRIORITY_MIN <= priority <= PRIORITY_MAX):
        raise ValueError(
            f"priority must be between {PRIORITY_MIN} and {PRIORITY_MAX} (1 = highest)"
        )
    return priority


def effective_priority(item: dict) -> int | None:
    """Native priority column, falling back to the legacy [pN] title prefix."""
    if item.get("priority") is not None:
        return item["priority"]
    match = _PRIORITY_TITLE_RE.match(item.get("title") or "")
    return int(match.group(1)) if match else None


class WorkItemConn(Protocol):
    """Minimal storage handle a backend provides for work-item operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    updated_at_sql: str
    """SQL expression for 'now' in an ``updated_at`` touch column."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_one(self, sql: str, params: tuple) -> dict | None: ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def insert_id(self, sql: str, params: tuple) -> int: ...

    def update_one(self, sql: str, params: tuple) -> bool:
        """Run an UPDATE, commit and return True if exactly one row matched.

        Rolls back and returns False if no row matched (the SQL's WHERE
        clause is expected to include the tenant + id conditions already).
        """
        ...

    def join_tenant_clause(self, left_alias: str, right_alias: str) -> str:
        """Extra join condition scoping both sides to the same tenant.

        Empty on SQLite (no repo_id column); ``AND a.repo_id = b.repo_id`` on
        PostgreSQL, where rows from different repos could otherwise join.
        """
        ...


def _where(conn: WorkItemConn, *conditions: str) -> str:
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def _insert_columns(conn: WorkItemConn) -> str:
    return "repo_id, " if conn.tenant_params() else ""


def _insert_placeholders(conn: WorkItemConn) -> str:
    return f"{conn.ph}, " if conn.tenant_params() else ""


def create_work_item(
    conn: WorkItemConn,
    sprint_id: int,
    track_id: int,
    title: str,
    description: str = "",
    assignee: str | None = None,
    priority: int | None = None,
) -> int:
    priority = validate_priority(priority)
    ph = conn.ph
    sql = (
        f"INSERT INTO work_item ({_insert_columns(conn)}sprint_id, track_id, title,"
        f" description, assignee, priority, aggregate_uuid)"
        f" VALUES ({_insert_placeholders(conn)}{ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
    )
    params = conn.tenant_params() + (
        sprint_id,
        track_id,
        title,
        description,
        assignee,
        priority,
        str(uuid4()),
    )
    return conn.insert_id(sql, params)


def set_work_item_priority(conn: WorkItemConn, item_id: int, priority: int | None) -> None:
    priority = validate_priority(priority)
    ph = conn.ph
    sql = (
        f"UPDATE work_item SET priority = {ph}, updated_at = {conn.updated_at_sql}"
        f"{_where(conn, f'id = {ph}')}"
    )
    found = conn.update_one(sql, (priority,) + conn.tenant_params() + (item_id,))
    if not found:
        raise ValueError(f"Item #{item_id} not found")


def get_work_item(conn: WorkItemConn, item_id: int) -> dict | None:
    sql = f"SELECT * FROM work_item{_where(conn, f'id = {conn.ph}')}"
    return conn.query_one(sql, conn.tenant_params() + (item_id,))


def list_work_items(
    conn: WorkItemConn,
    sprint_id: int | None = None,
    track_name: str | None = None,
    status: str | None = None,
) -> list[dict]:
    conditions = []
    params: list = []
    if sprint_id is not None:
        conditions.append(f"wi.sprint_id = {conn.ph}")
        params.append(sprint_id)
    if track_name is not None:
        conditions.append(f"t.name = {conn.ph}")
        params.append(track_name)
    if status is not None:
        conditions.append(f"wi.status = {conn.ph}")
        params.append(status)

    where_parts: list[str] = []
    if conn.tenant_params():
        where_parts.append(f"wi.repo_id = {conn.ph}")
    where_parts.extend(conditions)
    where_clause = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    join_extra = conn.join_tenant_clause("wi", "t")

    sql = (
        "SELECT wi.*, t.name AS track_name"
        " FROM work_item wi"
        f" JOIN track t ON wi.track_id = t.id{join_extra}"
        f"{where_clause}"
        " ORDER BY wi.created_at ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + tuple(params))
