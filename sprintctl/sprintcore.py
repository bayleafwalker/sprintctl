"""Shared sprint-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`SprintConn`.  The query shapes, tenant
handling, and result normalisation live here once so the two backends cannot
drift apart in sprint-table behaviour.

Historical drift resolved by this module (converged on the PostgreSQL
production-authority behaviour):

- ``get_active_sprint`` now breaks ``created_at`` ties by ``id DESC`` on both
  backends, making "newest active sprint" deterministic.
- ``create_sprint`` coerces falsy ``start_date``/``end_date`` to NULL on both
  backends.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol
import re
from uuid import uuid4

from . import contracts as _contracts
from .workitemcore import StatusConflict


_KIND_REVISION = re.compile(
    r"^sprint:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"@kind:(active_sprint|backlog|archive)@status:(planned|active|closed)$"
)


def sprint_kind_revision(sprint: dict) -> str:
    """State-basis comparison, not a monotonic revision or authority proof."""
    return f"sprint:{sprint['aggregate_uuid']}@kind:{sprint['kind']}@status:{sprint['status']}"


def validate_sprint_kind_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (1 << 63) - 1:
        raise ValueError("sprint_id must be a positive signed 64-bit integer")
    return value


def validate_kind_revision(value: str) -> str:
    if not isinstance(value, str) or not _KIND_REVISION.fullmatch(value):
        raise ValueError("expected_revision must be a valid sprint kind revision")
    return value


class SprintConn(Protocol):
    """Minimal storage handle a backend provides for sprint-table operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_one(self, sql: str, params: tuple) -> dict | None: ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def mutate(self, sql: str, params: tuple) -> None: ...

    def insert_id(self, sql: str, params: tuple) -> int: ...


def _where(conn: SprintConn, *conditions: str) -> str:
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def _insert_columns(conn: SprintConn) -> str:
    return "repo_id, " if conn.tenant_params() else ""


def _insert_placeholders(conn: SprintConn) -> str:
    return f"{conn.ph}, " if conn.tenant_params() else ""


def create_sprint(
    conn: SprintConn,
    name: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    status: str = "planned",
    kind: str = "active_sprint",
) -> int:
    ph = conn.ph
    sql = (
        f"INSERT INTO sprint ({_insert_columns(conn)}name, goal, start_date,"
        f" end_date, status, kind, aggregate_uuid)"
        f" VALUES ({_insert_placeholders(conn)}{ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
    )
    params = conn.tenant_params() + (
        name,
        goal,
        start_date or None,
        end_date or None,
        status,
        kind,
        str(uuid4()),
    )
    return conn.insert_id(sql, params)


def get_sprint(conn: SprintConn, sprint_id: int) -> dict | None:
    sql = f"SELECT * FROM sprint{_where(conn, f'id = {conn.ph}')}"
    return conn.query_one(sql, conn.tenant_params() + (sprint_id,))


def get_active_sprint(conn: SprintConn) -> dict | None:
    """Return the newest active sprint.

    Multiple active sprints are allowed. Callers that need to reason about
    ambiguity should use list_active_sprints instead.
    """
    where = _where(conn, "status = 'active'", "kind = 'active_sprint'")
    sql = f"SELECT * FROM sprint{where} ORDER BY created_at DESC, id DESC LIMIT 1"
    return conn.query_one(sql, conn.tenant_params())


def list_active_sprints(conn: SprintConn) -> list[dict]:
    where = _where(conn, "status = 'active'", "kind = 'active_sprint'")
    sql = f"SELECT * FROM sprint{where} ORDER BY created_at DESC, id DESC"
    return conn.query_all(sql, conn.tenant_params())


def list_sprints(conn: SprintConn) -> list[dict]:
    sql = f"SELECT * FROM sprint{_where(conn)} ORDER BY created_at DESC"
    return conn.query_all(sql, conn.tenant_params())


def set_sprint_kind(
    conn: SprintConn,
    sprint_id: int,
    kind: str,
    *,
    valid_kinds: tuple[str, ...],
) -> None:
    if kind not in valid_kinds:
        raise ValueError(
            f"Invalid kind '{kind}'. Must be one of: {', '.join(valid_kinds)}"
        )
    if get_sprint(conn, sprint_id) is None:
        raise ValueError(f"Sprint #{sprint_id} not found")
    sql = f"UPDATE sprint SET kind = {conn.ph}{_where(conn, f'id = {conn.ph}')}"
    conn.mutate(sql, (kind,) + conn.tenant_params() + (sprint_id,))


class _CloseParticipant(Protocol):
    """Private close operations; backend wrappers retain transaction ownership."""

    def locked_sprint(
        self, sprint_id: int,
    ) -> AbstractContextManager[tuple[dict | None, Callable[[], None]]]: ...

    def revision(self, sprint: dict) -> str: ...

    def validate_transition(self, current: str, target: str) -> None: ...

    def insert_event(
        self, sprint_id: int, actor: str, event_type: str, *,
        source_type: str, payload: dict,
    ) -> int: ...


def _close_sprint_with_boundary_event(
    participant: _CloseParticipant,
    sprint_id: int,
    actor: str,
    expected_revision: str | None,
) -> int:
    """Sequence an explicit close inside the caller's existing transaction.

    Request validation and commit/rollback stay in each public backend wrapper.
    In particular, this adds no PostgreSQL caller-composition guarantee.
    """
    with participant.locked_sprint(sprint_id) as (sprint, mark_closed):
        if sprint is None:
            raise ValueError(f"Sprint #{sprint_id} not found")
        current = sprint["status"]
        current_revision = participant.revision(sprint)
        if expected_revision is not None and expected_revision != current_revision:
            raise StatusConflict(
                f"Sprint #{sprint_id} status revision mismatch: "
                f"expected {expected_revision}, current {current_revision}"
            )
        participant.validate_transition(current, "closed")
        mark_closed()
    # Preserve the PG SELECT/UPDATE cursor boundary before canonical insertion.
    return participant.insert_event(
        sprint_id,
        actor.strip(),
        _contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
        source_type="actor",
        payload={"previous_status": current, "status": "closed"},
    )
