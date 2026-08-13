"""Shared event-table storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`EventConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
event-table behaviour. Mirrors the pattern established in ``sprintcore.py``.

Also owns the pure, backend-agnostic takeup/payload helpers that used to
live in ``db.py`` and be imported into ``pg.py`` — that made ``pg.py``
depend on ``db.py`` for logic with no SQLite dependency at all. They live
here now so both backends are peers.

Transactional/business-logic operations (``create_event``,
``create_archive_import_event``, ``close_sprint_with_boundary_event``, and
``_require_receipt_close_boundary``) stay in each backend's wrapper: they
call other backend-specific functions and control their own commit/
rollback boundary. Only the raw storage shape lives here.
"""

from __future__ import annotations

import json
from typing import Protocol

KNOWLEDGE_EVENT_TYPES = ("decision", "pattern-noted", "lesson-learned", "risk-accepted")
TAKEUP_EVENT_TYPES = ("sprint-taken-up", "sprint-released")


def _decode_event_payload(row: dict) -> dict:
    """Decode an event row's payload, whether it's already a dict (pg) or JSON text (sqlite)."""
    payload = row.get("payload") or "{}"
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _stringify_payload(row: dict) -> dict:
    """Return row with its payload as a JSON string, matching sqlite's storage convention.

    pg's native adapter returns jsonb payloads already decoded to a dict;
    sqlite's payload column is text and comes back as a string already.
    Callers of list_events/list_events_limited expect the sqlite convention
    (a string they can json.loads themselves) on both backends.
    """
    payload = row.get("payload")
    if isinstance(payload, dict):
        return {**row, "payload": json.dumps(payload)}
    return row


class EventConn(Protocol):
    """Minimal storage handle a backend provides for event-table operations."""

    ph: str
    """Parameter placeholder for the backend's DB-API driver (``?``/``%s``)."""

    def tenant_params(self) -> tuple:
        """Tenant discriminator params (``(repo_id,)`` on pg, ``()`` on SQLite)."""
        ...

    def query_all(self, sql: str, params: tuple) -> list[dict]: ...

    def insert_uncommitted(self, sql: str, params: tuple) -> int:
        """Insert an event row and return its id without committing.

        Event inserts are always part of a larger caller-controlled
        transaction (create_event, close_sprint_with_boundary_event, ...).
        """
        ...


def _where(conn: EventConn, *conditions: str) -> str:
    parts: list[str] = []
    if conn.tenant_params():
        parts.append(f"repo_id = {conn.ph}")
    parts.extend(conditions)
    return " WHERE " + " AND ".join(parts) if parts else ""


def _insert_columns(conn: EventConn) -> str:
    return "repo_id, " if conn.tenant_params() else ""


def _insert_placeholders(conn: EventConn) -> str:
    return f"{conn.ph}, " if conn.tenant_params() else ""


def insert_event_row(
    conn: EventConn,
    sprint_id: int,
    actor: str,
    event_type: str,
    source_type: str,
    work_item_id: int | None,
    payload_json: str,
) -> int:
    """Insert one event row (payload already canonicalized + json.dumps'd by the caller)."""
    ph = conn.ph
    sql = (
        f"INSERT INTO event ({_insert_columns(conn)}sprint_id, work_item_id, source_type,"
        f" actor, event_type, payload)"
        f" VALUES ({_insert_placeholders(conn)}{ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
    )
    params = conn.tenant_params() + (
        sprint_id,
        work_item_id,
        source_type,
        actor,
        event_type,
        payload_json,
    )
    return conn.insert_uncommitted(sql, params)


def list_events(conn: EventConn, sprint_id: int) -> list[dict]:
    sql = f"SELECT * FROM event{_where(conn, f'sprint_id = {conn.ph}')} ORDER BY created_at ASC"
    rows = conn.query_all(sql, conn.tenant_params() + (sprint_id,))
    return [_stringify_payload(r) for r in rows]


def list_events_limited(conn: EventConn, sprint_id: int, limit: int = 50) -> list[dict]:
    sql = (
        f"SELECT * FROM event{_where(conn, f'sprint_id = {conn.ph}')}"
        f" ORDER BY created_at DESC LIMIT {conn.ph}"
    )
    rows = conn.query_all(sql, conn.tenant_params() + (sprint_id, limit))
    return [_stringify_payload(r) for r in reversed(rows)]


def list_knowledge_candidates(conn: EventConn, sprint_id: int) -> list[dict]:
    """Return events of knowledge types for a sprint, with payload deserialized."""
    placeholders = ", ".join([conn.ph] * len(KNOWLEDGE_EVENT_TYPES))
    sql = (
        "SELECT * FROM event"
        f"{_where(conn, f'sprint_id = {conn.ph}', f'event_type IN ({placeholders})')}"
        " ORDER BY created_at ASC, id ASC"
    )
    rows = conn.query_all(
        sql, conn.tenant_params() + (sprint_id, *KNOWLEDGE_EVENT_TYPES)
    )
    result = []
    for row in rows:
        row = dict(row)
        row["payload"] = _decode_event_payload(row)
        result.append(row)
    return result


def _takeup_event_rows(conn: EventConn, sprint_id: int | None = None) -> list[dict]:
    placeholders = ", ".join([conn.ph] * len(TAKEUP_EVENT_TYPES))
    conditions = [f"event_type IN ({placeholders})"]
    params = conn.tenant_params() + tuple(TAKEUP_EVENT_TYPES)
    if sprint_id is not None:
        conditions.append(f"sprint_id = {conn.ph}")
        params = params + (sprint_id,)
    sql = (
        f"SELECT * FROM event{_where(conn, *conditions)}"
        " ORDER BY sprint_id ASC, created_at ASC, id ASC"
    )
    return conn.query_all(sql, params)


def _takeup_key(row: dict, payload: dict) -> tuple[int, str, str | None]:
    return (row["sprint_id"], row["actor"], payload.get("instance_id"))


def _takeup_actor_key(row: dict) -> tuple[int, str]:
    return (row["sprint_id"], row["actor"])


def _serialize_takeup_row(row: dict, payload: dict) -> dict:
    return {
        "sprint_id": row["sprint_id"],
        "actor": row["actor"],
        "actor_kind": payload.get("actor_kind", "agent"),
        "instance_id": payload.get("instance_id"),
        "hostname": payload.get("hostname"),
        "pid": payload.get("pid"),
        "runtime_session_id": payload.get("runtime_session_id"),
        "taken_up_at": row["created_at"],
        "taken_up_event_id": row["id"],
        "context": payload.get("context"),
        "forced": bool(payload.get("forced", False)),
    }


def _serialize_released_takeup(
    takeup_row: dict,
    takeup_payload: dict,
    release_row: dict,
    release_payload: dict,
) -> dict:
    result = _serialize_takeup_row(takeup_row, takeup_payload)
    result.update(
        {
            "released_at": release_row["created_at"],
            "released_event_id": release_row["id"],
            "reason": release_payload.get("reason"),
            "matched_takeup_event_id": release_payload.get("matched_takeup_event_id"),
        }
    )
    return result


def process_takeup_events(events: list[dict]) -> dict[str, list[dict]]:
    """Derive active/released takeup state from a pre-fetched list of takeup events.

    Accepts events already fetched from either sqlite or pg. The payload field
    may be a dict (pg jsonb) or a JSON string (sqlite text); _decode_event_payload
    handles both.
    """
    active_by_event_id: dict[int, tuple[dict, dict]] = {}
    active_by_key: dict[tuple[int, str, str | None], list[int]] = {}
    active_by_actor: dict[tuple[int, str], list[int]] = {}
    released: list[dict] = []
    unmatched_releases: list[dict] = []

    for row in events:
        payload = _decode_event_payload(row)
        if row["event_type"] == "sprint-taken-up":
            active_by_event_id[row["id"]] = (row, payload)
            active_by_key.setdefault(_takeup_key(row, payload), []).append(row["id"])
            active_by_actor.setdefault(_takeup_actor_key(row), []).append(row["id"])
            continue

        matched_id = payload.get("matched_takeup_event_id")
        takeup_pair: tuple[dict, dict] | None = None
        if matched_id is not None:
            takeup_pair = active_by_event_id.pop(matched_id, None)
        if takeup_pair is None:
            key = _takeup_key(row, payload)
            candidates = active_by_key.get(key, [])
            while candidates and candidates[-1] not in active_by_event_id:
                candidates.pop()
            if candidates:
                takeup_pair = active_by_event_id.pop(candidates.pop(), None)
        if takeup_pair is None and payload.get("instance_id") is None:
            candidates = active_by_actor.get(_takeup_actor_key(row), [])
            while candidates and candidates[-1] not in active_by_event_id:
                candidates.pop()
            if candidates:
                takeup_pair = active_by_event_id.pop(candidates.pop(), None)
        if takeup_pair is None:
            unmatched_releases.append(
                {
                    "sprint_id": row["sprint_id"],
                    "actor": row["actor"],
                    "actor_kind": payload.get("actor_kind", "agent"),
                    "instance_id": payload.get("instance_id"),
                    "hostname": payload.get("hostname"),
                    "pid": payload.get("pid"),
                    "released_at": row["created_at"],
                    "released_event_id": row["id"],
                    "reason": payload.get("reason"),
                    "matched_takeup_event_id": payload.get("matched_takeup_event_id"),
                }
            )
            continue

        released.append(_serialize_released_takeup(*takeup_pair, row, payload))

    current_by_key: dict[tuple[int, str, str | None], dict] = {}
    for takeup_row, takeup_payload in active_by_event_id.values():
        current_by_key[_takeup_key(takeup_row, takeup_payload)] = _serialize_takeup_row(
            takeup_row,
            takeup_payload,
        )
    active = sorted(
        current_by_key.values(),
        key=lambda item: (item["sprint_id"], item["taken_up_at"], item["taken_up_event_id"]),
    )
    return {
        "active_takeups": active,
        "released_takeups": released,
        "unmatched_releases": unmatched_releases,
    }


def list_takeup_history(conn: EventConn, sprint_id: int | None = None) -> dict[str, list[dict]]:
    """Return active and released sprint takeup rows derived from events."""
    return process_takeup_events(_takeup_event_rows(conn, sprint_id))


def list_active_takeups(conn: EventConn, sprint_id: int | None = None) -> list[dict]:
    return list_takeup_history(conn, sprint_id)["active_takeups"]
