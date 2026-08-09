"""Observable projection of Sprintctl-owned maintenance capabilities.

This module owns reference mapping, observation ordering and finite retention.
It does not grant maintenance authority and never interprets state outside the
maintenance capability owner's frozen terminal-state declaration.
"""

from __future__ import annotations

import base64
from datetime import datetime
import secrets
import sqlite3
import time
import re
from typing import Any, Callable, Mapping


TERMINAL_STATES = frozenset({"reconciled", "aborted", "revoked", "expired"})
MAX_EVENTS = 256
MAX_BATCH = 100
REFERENCE_PATTERN = re.compile(r"^smr1_[A-Za-z0-9_-]{43}$")
NOT_FOUND_PAYLOAD = {"code": "resource_not_found", "message": "resource not found"}


class ResourceNotFound(ValueError):
    pass


class CursorExpired(ValueError):
    pass


def _reference() -> str:
    return "smr1_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _revision(position: int) -> str:
    return f"sprintctl-maintenance-revision-{position}"


def _cursor(position: int) -> str:
    return f"sprintctl-maintenance-cursor-{position}"


def _position(cursor: str) -> int:
    prefix = "sprintctl-maintenance-cursor-"
    if not isinstance(cursor, str) or not cursor.startswith(prefix):
        raise ResourceNotFound("resource not found")
    try:
        value = int(cursor[len(prefix):])
    except ValueError as exc:
        raise ResourceNotFound("resource not found") from exc
    if value < 0:
        raise ResourceNotFound("resource not found")
    return value


def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _value(row: Any, index: int, key: str) -> Any:
    return row[key] if isinstance(row, Mapping) else row[index]


class MaintenanceResourceStore:
    """Backend-neutral owner projection over SQLite or psycopg connections."""

    def __init__(self, lifecycle_store: Any, *, monotonic: Callable[[], float] = time.monotonic, pause: Callable[[float], None] = time.sleep):
        self.lifecycle = lifecycle_store
        self.conn = lifecycle_store.conn
        self.repo_id = getattr(lifecycle_store, "repo_id", None)
        self.postgres = self.repo_id is not None
        self.monotonic = monotonic
        self.pause = pause

    @staticmethod
    def schema_exists(lifecycle_store: Any) -> bool:
        if getattr(lifecycle_store, "repo_id", None) is not None:
            cur = lifecycle_store.conn.cursor()
            cur.execute("SELECT to_regclass('maintenance_resource') IS NOT NULL AS present")
            row = cur.fetchone()
            return bool(_value(row, 0, "present"))
        return lifecycle_store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_resource'").fetchone() is not None

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.postgres:
            sql = sql.replace("?", "%s")
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur
        return self.conn.execute(sql, params)

    def _scope(self) -> tuple[str, tuple[Any, ...]]:
        return ("repo_id=? AND ", (self.repo_id,)) if self.postgres else ("", ())

    def ensure_reference(self, capability_id: str, *, commit: bool = True) -> str:
        scope, values = self._scope()
        row = self._execute(f"SELECT resource_ref FROM maintenance_resource WHERE {scope}capability_id=?", values + (capability_id,)).fetchone()
        if row:
            return _value(row, 0, "resource_ref")
        resource_ref = _reference()
        if self.postgres:
            self._execute("INSERT INTO maintenance_resource(repo_id,resource_ref,capability_id) VALUES(?,?,?) ON CONFLICT(repo_id,capability_id) DO NOTHING", (self.repo_id, resource_ref, capability_id))
        else:
            self._execute("INSERT OR IGNORE INTO maintenance_resource(resource_ref,capability_id) VALUES(?,?)", (resource_ref, capability_id))
        if commit:
            self.conn.commit()
        row = self._execute(f"SELECT resource_ref FROM maintenance_resource WHERE {scope}capability_id=?", values + (capability_id,)).fetchone()
        return _value(row, 0, "resource_ref")

    def record_current(self, capability_id: str, *, commit: bool = True) -> str:
        resource_ref = self.ensure_reference(capability_id, commit=False)
        if self.postgres:
            owner = self._execute(
                "SELECT state,not_before,expires_at,updated_at FROM maintenance_capability WHERE repo_id=? AND capability_id=?",
                (self.repo_id, capability_id),
            ).fetchone()
        else:
            owner = self.lifecycle.get(capability_id)
        if owner is None:
            raise ResourceNotFound("resource not found")
        scope, values = self._scope()
        current_row = self._execute(f"SELECT current_position FROM maintenance_resource WHERE {scope}resource_ref=?", values + (resource_ref,)).fetchone()
        current = _value(current_row, 0, "current_position")
        existing = self._execute(f"SELECT state,updated_at FROM maintenance_resource_event WHERE {scope}resource_ref=? ORDER BY position DESC LIMIT 1", values + (resource_ref,)).fetchone()
        updated = _stamp(owner["updated_at"])
        if existing and _value(existing, 0, "state") == owner["state"] and _stamp(_value(existing, 1, "updated_at")) == updated:
            return resource_ref
        position = current + 1
        if self.postgres:
            self._execute("INSERT INTO maintenance_resource_event(repo_id,resource_ref,position,state,updated_at) VALUES(?,?,?,?,?)", (self.repo_id, resource_ref, position, owner["state"], updated))
        else:
            self._execute("INSERT INTO maintenance_resource_event(resource_ref,position,state,updated_at) VALUES(?,?,?,?)", (resource_ref, position, owner["state"], updated))
        self._execute(f"UPDATE maintenance_resource SET current_position=? WHERE {scope}resource_ref=?", (position,) + values + (resource_ref,))
        count_row = self._execute(f"SELECT COUNT(*) AS event_count FROM maintenance_resource_event WHERE {scope}resource_ref=?", values + (resource_ref,)).fetchone()
        count = _value(count_row, 0, "event_count")
        if count > MAX_EVENTS:
            cutoff = position - MAX_EVENTS + 1
            self._execute(f"DELETE FROM maintenance_resource_event WHERE {scope}resource_ref=? AND position<?", values + (resource_ref, cutoff))
            self._execute(f"UPDATE maintenance_resource SET recovery_floor=? WHERE {scope}resource_ref=?", (cutoff - 1,) + values + (resource_ref,))
        if commit:
            self.conn.commit()
        return resource_ref

    def record_if_registered(self, capability_id: str, *, commit: bool = True) -> None:
        scope, values = self._scope()
        row = self._execute(f"SELECT 1 FROM maintenance_resource WHERE {scope}capability_id=?", values + (capability_id,)).fetchone()
        if row is not None:
            self.record_current(capability_id, commit=commit)

    def record_current_in_transaction(self, capability_id: str) -> str:
        return self.record_current(capability_id, commit=False)

    def _binding(self, resource_ref: str) -> tuple[str, int, int]:
        scope, values = self._scope()
        row = self._execute(f"SELECT capability_id,recovery_floor,current_position FROM maintenance_resource WHERE {scope}resource_ref=?", values + (resource_ref,)).fetchone()
        if row is None:
            raise ResourceNotFound("resource not found")
        return str(_value(row, 0, "capability_id")), int(_value(row, 1, "recovery_floor")), int(_value(row, 2, "current_position"))

    def visible(self, resource_ref: Any, *, authorized: bool) -> bool:
        """Non-mutating owner lookup for Vuoro's later visibility guard."""
        if not authorized or not isinstance(resource_ref, str) or not REFERENCE_PATTERN.fullmatch(resource_ref):
            return False
        try:
            self._binding(resource_ref)
        except ResourceNotFound:
            return False
        return True

    def snapshot(self, resource_ref: str) -> dict[str, Any]:
        if self.postgres:
            row = self._execute(
                "SELECT r.current_position,c.state,c.not_before,c.expires_at,c.updated_at FROM maintenance_resource r JOIN maintenance_capability c ON c.repo_id=r.repo_id AND c.capability_id=r.capability_id WHERE r.repo_id=? AND r.resource_ref=?",
                (self.repo_id, resource_ref),
            ).fetchone()
        else:
            row = self._execute(
                "SELECT r.current_position,c.state,c.not_before,c.expires_at,c.updated_at FROM maintenance_resource r JOIN maintenance_capability c ON c.capability_id=r.capability_id WHERE r.resource_ref=?",
                (resource_ref,),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("resource not found")
        position = int(_value(row, 0, "current_position"))
        state = _value(row, 1, "state")
        return {"schema_version": "resource-snapshot/v1", "reference": resource_ref, "revision": _revision(position), "cursor": _cursor(position), "terminal": state in TERMINAL_STATES, "state": {"state": state, "not_before": _stamp(_value(row, 2, "not_before")), "expires_at": _stamp(_value(row, 3, "expires_at")), "updated_at": _stamp(_value(row, 4, "updated_at"))}}

    def changes(self, resource_ref: str, cursor: str, wait_seconds: int = 0) -> dict[str, Any]:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be an integer from 0 through 30")
        after = _position(cursor)
        deadline = self.monotonic() + wait_seconds
        while True:
            _capability_id, floor, current = self._binding(resource_ref)
            if after < floor:
                raise CursorExpired("fetch a fresh snapshot")
            if after > current:
                raise ResourceNotFound("resource not found")
            if current > after or self.monotonic() >= deadline:
                break
            self.pause(min(0.05, max(0.0, deadline - self.monotonic())))
        scope, values = self._scope()
        rows = self._execute(f"SELECT position,state,updated_at FROM maintenance_resource_event WHERE {scope}resource_ref=? AND position>? ORDER BY position LIMIT ?", values + (resource_ref, after, MAX_BATCH)).fetchall()
        next_position = int(_value(rows[-1], 0, "position")) if rows else after
        return {"schema_version": "resource-changes/v1", "reference": resource_ref, "next_cursor": _cursor(next_position), "events": [{"event_id": f"sprintctl-maintenance-event-{int(_value(row, 0, 'position'))}", "terminal": _value(row, 1, "state") in TERMINAL_STATES, "data": {"state": _value(row, 1, "state"), "updated_at": _stamp(_value(row, 2, "updated_at"))}} for row in rows]}

    def reference_envelope(self, capability_id: str) -> dict[str, Any]:
        scope, values = self._scope()
        row = self._execute(f"SELECT resource_ref FROM maintenance_resource WHERE {scope}capability_id=?", values + (capability_id,)).fetchone()
        if row is None:
            raise ResourceNotFound("resource not found")
        resource_ref = _value(row, 0, "resource_ref")
        _capability_id, _floor, position = self._binding(resource_ref)
        return {"schema_version": "resource-reference/v1", "owner": "work", "resource_kind": "work.maintenance-capability", "reference": resource_ref, "revision": _revision(position)}
