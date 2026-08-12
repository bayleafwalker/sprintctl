"""Shared track-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`TrackConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
track-table behaviour. Mirrors the pattern established in ``sprintcore.py``.
"""

from __future__ import annotations

from typing import Protocol


class TrackConn(Protocol):
    """Minimal storage handle a backend provides for track-table operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_one(self, sql: str, params: tuple) -> dict | None: ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def insert_ignore(
        self, columns: tuple[str, ...], values: tuple, conflict_cols: tuple[str, ...]
    ) -> None:
        """Insert a track row, doing nothing on a conflicting unique key."""
        ...


def _where(conn: TrackConn, *conditions: str) -> str:
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def get_or_create_track(
    conn: TrackConn,
    sprint_id: int,
    name: str,
    description: str = "",
) -> int:
    columns = ("sprint_id", "name", "description")
    values = (sprint_id, name, description)
    conflict_cols = ("sprint_id", "name")
    if conn.tenant_params():
        columns = ("repo_id",) + columns
        values = conn.tenant_params() + values
        conflict_cols = ("repo_id",) + conflict_cols
    conn.insert_ignore(columns, values, conflict_cols)

    sql = (
        "SELECT id FROM track"
        f"{_where(conn, f'sprint_id = {conn.ph}', f'name = {conn.ph}')}"
    )
    row = conn.query_one(sql, conn.tenant_params() + (sprint_id, name))
    return row["id"]


def list_tracks(conn: TrackConn, sprint_id: int) -> list[dict]:
    sql = (
        "SELECT * FROM track"
        f"{_where(conn, f'sprint_id = {conn.ph}')} ORDER BY created_at ASC"
    )
    return conn.query_all(sql, conn.tenant_params() + (sprint_id,))


def get_track(conn: TrackConn, track_id: int) -> dict | None:
    sql = f"SELECT * FROM track{_where(conn, f'id = {conn.ph}')}"
    return conn.query_one(sql, conn.tenant_params() + (track_id,))
