"""Shared work-item storage logic for both sprintctl backends.

Each backend (``db.py`` for SQLite, ``pg.py`` for PostgreSQL) supplies a tiny
connection adapter implementing :class:`WorkItemConn`. The query shapes and
tenant handling live here once so the two backends cannot drift apart in
work-item behaviour. Mirrors the pattern established in ``sprintcore.py``.

Also owns the pure, backend-agnostic validators that used to live in
``db.py`` and be imported into ``pg.py`` — that made ``pg.py`` depend on
``db.py`` for logic with no SQLite dependency at all. They live here now so
both backends are peers.

``update_work_item_description`` (this module's first CAS/conflict-detection
operation) prototypes a transaction-scaffold abstraction over the CRUD
protocol: ``begin_txn``/``commit``/``rollback`` for the multi-statement
transaction, and ``for_update_of``/``lock_for_update`` for row locking.
SQLite serializes the whole write (``BEGIN IMMEDIATE`` takes a whole-DB write
lock before any read); PostgreSQL locks only the row in question
(``SELECT ... FOR UPDATE``). This is a genuine, pre-existing concurrency-model
difference this abstraction does not paper over: two concurrent SQLite CAS
writers on *different* work items already serialize against each other
(same as before this extraction), while PostgreSQL's row lock lets them
proceed in parallel. What both backends must guarantee identically — and
what ``tests/test_core.py``'s and ``tests/pg/test_work_item.py``'s
concurrency tests assert — is that two writers racing the *same* item
produce exactly one accepted write and one ``EditConflict``.

``EditConflict``/``StatusConflict`` live here (not ``db.py``) so both
backends import them as peers; ``db.py`` re-exports them for existing
callers.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol
from uuid import uuid4

from . import contracts as _contracts

PRIORITY_MIN = 1
PRIORITY_MAX = 9

_ITEM_EDIT_REVISION_RE = re.compile(
    r"^item:[0-9a-fA-F-]{36}@description:v[0-9]+@sha256:[0-9a-f]{64}$"
)


def _description_revision(item: dict, version: int) -> str:
    digest = hashlib.sha256((item.get("description") or "").encode("utf-8")).hexdigest()
    return f"item:{item['aggregate_uuid']}@description:v{version}@sha256:{digest}"


def validate_item_edit_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _ITEM_EDIT_REVISION_RE.fullmatch(revision):
        raise ValueError("expected_revision must be a valid item description revision")
    return revision


_ITEM_STATUS_REVISION_RE = re.compile(
    r"^item:[0-9a-fA-F-]{36}@status:(pending|active|done|blocked)$"
)


def item_status_revision(item: dict) -> str:
    """Return the CAS token for the status-transition state of one item."""
    return f"item:{item['aggregate_uuid']}@status:{item['status']}"


def validate_item_status_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _ITEM_STATUS_REVISION_RE.fullmatch(revision):
        raise ValueError("expected_revision must be a valid item status revision")
    return revision


class EditConflict(ValueError):
    """A work-item edit was based on a stale description revision."""


class StatusConflict(ValueError):
    """An item or sprint transition was based on a stale status revision."""

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

    def begin_txn(self) -> None:
        """Start a serialized multi-statement transaction.

        SQLite: ``BEGIN IMMEDIATE`` — acquires the whole-DB write lock
        immediately, before any read, so a later writer cannot interleave
        between this call and the locked read that follows. PostgreSQL: a
        no-op — the connection is already in an explicit (non-autocommit)
        transaction, and the lock is acquired by the locked read itself
        (``FOR UPDATE``), not by a separate BEGIN statement.
        """
        ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def for_update_of(self, alias: str) -> str:
        """SQL suffix to lock the rows a SELECT just read for the rest of the txn.

        Empty on SQLite (``begin_txn``'s whole-DB lock already covers it);
        ``FOR UPDATE OF {alias}`` on PostgreSQL.
        """
        ...

    def lock_for_update(self, table: str, id_: int, extra_where: str = "") -> dict | None:
        """Read one row by id and hold it locked for the rest of the transaction.

        A plain, unlocked read on SQLite (see ``begin_txn``); ``SELECT ...
        FOR UPDATE`` on PostgreSQL. Returns None if no row matches (id or
        tenant mismatch). Callers must have already called ``begin_txn``.
        """
        ...

    def execute(self, sql: str, params: tuple) -> None:
        """Run a statement inside the caller's open transaction, without committing."""
        ...

    def insert_event(
        self,
        sprint_id: int,
        actor: str,
        event_type: str,
        *,
        source_type: str,
        work_item_id: int,
        payload: dict,
    ) -> int:
        """Insert an event row inside the caller's open transaction, returning its id."""
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


def _edit_revision_sql(conn: WorkItemConn, *, for_update: bool) -> str:
    ph = conn.ph
    suffix = conn.for_update_of("wi") if for_update else ""
    return (
        "SELECT wi.*, ("
        f" SELECT COUNT(*) FROM event e"
        f" WHERE e.work_item_id = wi.id AND e.event_type = {ph}"
        f"{' AND e.repo_id = wi.repo_id' if conn.tenant_params() else ''}"
        ") AS edit_version"
        f" FROM work_item wi{_where(conn, f'wi.id = {ph}')}"
        f"{suffix}"
    )


def get_work_item_with_edit_revision(
    conn: WorkItemConn, item_id: int
) -> tuple[dict, str] | None:
    """Read an item and its description revision from one storage snapshot.

    A plain, unlocked read — safe to call standalone (application.py/cli.py
    do, to preview the current revision before constructing an edit
    request). ``update_work_item_description`` uses its own locked variant
    internally instead of this function.
    """
    sql = _edit_revision_sql(conn, for_update=False)
    row = conn.query_one(
        sql, (_contracts.ITEM_EDITED_EVENT_TYPE,) + conn.tenant_params() + (item_id,)
    )
    if row is None:
        return None
    item = dict(row)
    version = int(item.pop("edit_version"))
    return item, _description_revision(item, version)


def _lock_work_item_with_edit_revision(
    conn: WorkItemConn, item_id: int
) -> tuple[dict, str] | None:
    """Locked variant of :func:`get_work_item_with_edit_revision`.

    Caller must have already called ``conn.begin_txn()``. See the module
    docstring for the SQLite-whole-DB-lock vs PostgreSQL-row-lock
    distinction this does not attempt to hide.
    """
    sql = _edit_revision_sql(conn, for_update=True)
    row = conn.query_one(
        sql, (_contracts.ITEM_EDITED_EVENT_TYPE,) + conn.tenant_params() + (item_id,)
    )
    if row is None:
        return None
    item = dict(row)
    version = int(item.pop("edit_version"))
    return item, _description_revision(item, version)


def update_work_item_description(
    conn: WorkItemConn,
    item_id: int,
    description: str,
    *,
    expected_revision: str | None = None,
    actor: str = "actor",
) -> dict:
    """Atomically edit an item and append its immutable audit revision.

    ``expected_revision`` is optional only for compatibility with direct
    backend callers. Catalog and CLI callers always provide the revision read
    from :func:`get_work_item_with_edit_revision`.
    """
    description = validate_work_item_description(description)
    if expected_revision is not None:
        expected_revision = validate_item_edit_revision(expected_revision)
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    try:
        conn.begin_txn()
        current = _lock_work_item_with_edit_revision(conn, item_id)
        if current is None:
            raise ValueError(f"Item #{item_id} not found")
        item, previous_revision = current
        if expected_revision is not None and expected_revision != previous_revision:
            raise EditConflict(
                f"Item #{item_id} description revision mismatch: "
                f"expected {expected_revision}, current {previous_revision}"
            )
        if item.get("description") == description:
            raise ValueError(f"Item #{item_id} description unchanged: no-op edit rejected")

        version = int(previous_revision.split("@description:v", 1)[1].split("@", 1)[0])
        revision = _description_revision({**item, "description": description}, version + 1)

        ph = conn.ph
        sql = (
            f"UPDATE work_item SET description = {ph}, updated_at = {conn.updated_at_sql}"
            f"{_where(conn, f'id = {ph}')}"
        )
        conn.execute(sql, (description,) + conn.tenant_params() + (item_id,))
        event_id = conn.insert_event(
            item["sprint_id"],
            actor.strip(),
            _contracts.ITEM_EDITED_EVENT_TYPE,
            source_type="actor",
            work_item_id=item_id,
            payload={
                "summary": f"Item #{item_id} description edited",
                "field": "description",
                "previous_description": item.get("description") or "",
                "description": description,
                "previous_revision": previous_revision,
                "revision": revision,
            },
        )
        updated = get_work_item(conn, item_id)
        assert updated is not None
        conn.commit()
        return {
            "item": updated,
            "event_id": event_id,
            "previous_revision": previous_revision,
            "revision": revision,
        }
    except Exception:
        conn.rollback()
        raise
