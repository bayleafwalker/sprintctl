"""Shared ref-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`RefConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
ref-table behaviour. Mirrors the pattern established in ``sprintcore.py``.

Validation (``ref_type`` membership, URL normalisation, work-item existence)
stays in each backend's wrapper because it calls other backend-specific
functions (``get_work_item``); this module only owns the raw storage shape.
"""

from __future__ import annotations

from typing import Protocol


class RefConn(Protocol):
    """Minimal storage handle a backend provides for ref-table operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_one(self, sql: str, params: tuple) -> dict | None: ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def mutate(self, sql: str, params: tuple) -> None: ...

    def insert_id(self, sql: str, params: tuple) -> int: ...


def _where(conn: RefConn, *conditions: str) -> str:
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def _insert_columns(conn: RefConn) -> str:
    return "repo_id, " if conn.tenant_params() else ""


def _insert_placeholders(conn: RefConn) -> str:
    return f"{conn.ph}, " if conn.tenant_params() else ""


def add_ref(
    conn: RefConn,
    work_item_id: int,
    ref_type: str,
    url: str,
    label: str = "",
) -> int:
    ph = conn.ph
    sql = (
        f"INSERT INTO ref ({_insert_columns(conn)}work_item_id, ref_type, url, label)"
        f" VALUES ({_insert_placeholders(conn)}{ph}, {ph}, {ph}, {ph})"
    )
    params = conn.tenant_params() + (work_item_id, ref_type, url, label)
    return conn.insert_id(sql, params)


def list_refs(conn: RefConn, work_item_id: int) -> list[dict]:
    sql = (
        "SELECT * FROM ref"
        f"{_where(conn, f'work_item_id = {conn.ph}')} ORDER BY created_at ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + (work_item_id,))


def list_refs_for_items(conn: RefConn, work_item_ids: list[int]) -> list[dict]:
    """Return raw ref rows for the given work items, ordered for grouping.

    Callers group by ``work_item_id`` and apply serialization themselves.
    """
    if not work_item_ids:
        return []
    placeholders = ", ".join([conn.ph] * len(work_item_ids))
    sql = (
        "SELECT * FROM ref"
        f"{_where(conn, f'work_item_id IN ({placeholders})')}"
        " ORDER BY created_at ASC, id ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + tuple(work_item_ids))


def remove_ref(conn: RefConn, ref_id: int, work_item_id: int) -> None:
    sql = (
        "SELECT id FROM ref"
        f"{_where(conn, f'id = {conn.ph}', f'work_item_id = {conn.ph}')}"
    )
    row = conn.query_one(sql, conn.tenant_params() + (ref_id, work_item_id))
    if row is None:
        raise ValueError(f"Ref #{ref_id} not found on item #{work_item_id}")
    sql = f"DELETE FROM ref{_where(conn, f'id = {conn.ph}')}"
    conn.mutate(sql, conn.tenant_params() + (ref_id,))
