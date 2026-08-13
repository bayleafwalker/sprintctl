import json
import os
import posixpath
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from uuid import uuid4

from . import claimcore as _claimcore
from . import contracts as _contracts
from . import depcore as _depcore
from . import eventcore as _eventcore
from . import refcore as _refcore
from . import rows as _rows
from . import sprintcore as _sprintcore
from . import trackcore as _trackcore
from . import workitemcore as _workitemcore
from .claimcore import CLAIM_TYPES, ClaimConflict
from .eventcore import (
    KNOWLEDGE_EVENT_TYPES,
    TAKEUP_EVENT_TYPES,
    _decode_event_payload,
    _takeup_actor_key,
    _takeup_key,
    process_takeup_events,
)
from .workitemcore import (
    PRIORITY_MAX,
    PRIORITY_MIN,
    EditConflict,
    StatusConflict,
    _description_revision,
    effective_priority,
    item_status_revision,
    validate_item_edit_revision,
    validate_item_status_revision,
    validate_priority,
    validate_work_item_description,
)


_WAL_BUSY_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08)


class InvalidTransition(ValueError):
    pass


VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"active"},
    "active": {"done", "blocked"},
    "done": set(),
    "blocked": {"active"},
}

SPRINT_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"active"},
    "active": {"closed"},
    "closed": set(),
}

SPRINT_KINDS = ("active_sprint", "backlog", "archive")

# Single source of truth for the local schema version; init_db() must end by
# migrating to exactly this version, and doctor compares databases against it.
CURRENT_SCHEMA_VERSION = 17

_MIGRATIONS: list[str] = [
    # Migration 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS sprint (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        goal        TEXT    NOT NULL DEFAULT '',
        start_date  TEXT    NOT NULL,
        end_date    TEXT    NOT NULL,
        status      TEXT    NOT NULL DEFAULT 'planned'
                            CHECK (status IN ('active', 'closed', 'planned')),
        created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS track (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sprint_id   INTEGER NOT NULL REFERENCES sprint(id) ON DELETE CASCADE,
        name        TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        UNIQUE (sprint_id, name)
    );

    CREATE TABLE IF NOT EXISTS work_item (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id    INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
        sprint_id   INTEGER NOT NULL REFERENCES sprint(id) ON DELETE CASCADE,
        title       TEXT    NOT NULL,
        description TEXT    NOT NULL DEFAULT '',
        status      TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'active', 'done', 'blocked')),
        assignee    TEXT,
        created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS event (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        sprint_id    INTEGER NOT NULL REFERENCES sprint(id) ON DELETE CASCADE,
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        source_type  TEXT    NOT NULL
                             CHECK (source_type IN ('actor', 'daemon', 'system')),
        actor        TEXT    NOT NULL,
        event_type   TEXT    NOT NULL,
        payload      TEXT    NOT NULL DEFAULT '{}',
        created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );
    """,
    # Migration 2: claim table
    """
    CREATE TABLE IF NOT EXISTS claim (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
        agent        TEXT    NOT NULL,
        claim_type   TEXT    NOT NULL DEFAULT 'execute'
                             CHECK (claim_type IN ('inspect', 'execute', 'review', 'coordinate')),
        exclusive    INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        expires_at   TEXT    NOT NULL,
        heartbeat    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    # Migration 3: sprint.kind classification column
    """
    ALTER TABLE sprint ADD COLUMN kind TEXT NOT NULL DEFAULT 'active_sprint'
        CHECK (kind IN ('active_sprint', 'backlog', 'archive'))
    """,
    # Migration 4: workspace metadata columns on claim
    """
    ALTER TABLE claim ADD COLUMN branch TEXT;
    ALTER TABLE claim ADD COLUMN worktree_path TEXT;
    ALTER TABLE claim ADD COLUMN commit_sha TEXT;
    ALTER TABLE claim ADD COLUMN pr_ref TEXT
    """,
    # Migration 5: make start_date and end_date nullable on sprint.
    # Sprint is a generic execution container; dates are optional metadata.
    # SQLite does not support ALTER COLUMN DROP NOT NULL, so we recreate the table.
    """
    PRAGMA foreign_keys = OFF;

    CREATE TABLE sprint_new (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        goal        TEXT    NOT NULL DEFAULT '',
        start_date  TEXT,
        end_date    TEXT,
        status      TEXT    NOT NULL DEFAULT 'planned'
                            CHECK (status IN ('active', 'closed', 'planned')),
        created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        kind        TEXT    NOT NULL DEFAULT 'active_sprint'
                            CHECK (kind IN ('active_sprint', 'backlog', 'archive'))
    );

    INSERT INTO sprint_new SELECT * FROM sprint;

    DROP TABLE sprint;

    ALTER TABLE sprint_new RENAME TO sprint;

    PRAGMA foreign_keys = ON
    """,
    # Migration 6: token-backed claim identity and runtime metadata.
    """
    ALTER TABLE claim ADD COLUMN claim_token TEXT;
    ALTER TABLE claim ADD COLUMN runtime_session_id TEXT;
    ALTER TABLE claim ADD COLUMN instance_id TEXT;
    ALTER TABLE claim ADD COLUMN hostname TEXT;
    ALTER TABLE claim ADD COLUMN pid INTEGER;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_token
        ON claim(claim_token)
        WHERE claim_token IS NOT NULL
    """,
    # Migration 7: external refs attached to work items.
    """
    CREATE TABLE IF NOT EXISTS ref (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
        ref_type     TEXT    NOT NULL DEFAULT 'other'
                             CHECK (ref_type IN ('pr', 'issue', 'doc', 'other')),
        url          TEXT    NOT NULL,
        label        TEXT    NOT NULL DEFAULT '',
        created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    # Migration 8: work item dependencies.
    # item_id must complete (done) before blocked_item_id can be started.
    """
    CREATE TABLE IF NOT EXISTS dep (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id         INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
        blocked_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
        created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        UNIQUE (item_id, blocked_item_id)
    )
    """,
    # Migration 9: sprint takeup event lookup index.
    """
    CREATE INDEX IF NOT EXISTS idx_event_sprint_type_ts
        ON event(sprint_id, event_type, created_at)
    """,
]

SCOPE_REF_TYPES = ("file", "glob", "manifest")
REF_TYPES = ("pr", "issue", "doc", "other", *SCOPE_REF_TYPES, "command")
_GLOB_MAGIC = frozenset("*?[")


def _is_repo_relative_doc_path(url: str) -> bool:
    if not url or url != url.strip() or url.startswith(("/", "~")):
        return False
    if urlparse(url).scheme:
        return False
    if "/" not in url and not url.endswith(".md"):
        return False
    parts = url.split("/")
    return ".." not in parts and "" not in parts


def _normalize_scope_path(value: str, *, require_glob: bool = False) -> str:
    if not value or value != value.strip() or value.startswith(("/", "~")):
        raise ValueError("Scope refs require a non-empty repo-relative path.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Scope refs must not contain control characters.")
    if "\\" in value or urlparse(value).scheme:
        raise ValueError("Scope refs require portable POSIX repo-relative paths.")
    parts = value.split("/")
    if ".." in parts or any(not part for part in parts):
        raise ValueError("Scope refs must not contain empty or parent path segments.")
    normalized = posixpath.normpath(value)
    if normalized in {"", "."} or normalized.startswith("../"):
        raise ValueError("Scope refs require a path below the repository root.")
    has_glob = any(char in normalized for char in _GLOB_MAGIC)
    if require_glob and not has_glob:
        raise ValueError("Glob refs must contain at least one glob expression (*, ?, or []).")
    if not require_glob and has_glob:
        raise ValueError("File and manifest refs must be exact paths without glob expressions.")
    return normalized


def _normalize_command_target(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Command refs require a non-empty shell command.")
    if "\x00" in command:
        raise ValueError("Command refs must not contain NUL bytes.")
    return command.strip()


def _normalize_ref_target(url: str, ref_type: str = "other") -> str:
    if ref_type == "command":
        return _normalize_command_target(url)
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        if ref_type in SCOPE_REF_TYPES:
            raise ValueError("Scope refs require repo-relative paths, not remote URLs.")
        return url
    if ref_type in {"file", "manifest"}:
        return _normalize_scope_path(url)
    if ref_type == "glob":
        return _normalize_scope_path(url, require_glob=True)
    if ref_type == "doc":
        if _is_repo_relative_doc_path(url):
            return posixpath.normpath(url)
        raise ValueError(
            "Invalid ref URL. Doc refs accept an absolute http(s) URL "
            "or a repo-relative path (e.g. docs/plans/my-plan.md)."
        )
    raise ValueError("Invalid ref URL. Use an absolute http(s) URL.")


def _validate_ref_url(url: str, ref_type: str = "other") -> None:
    _normalize_ref_target(url, ref_type)


def _serialize_ref(row: sqlite3.Row | dict) -> dict:
    ref = dict(row)
    ref_type = ref["ref_type"]
    url = ref["url"]
    if ref_type in SCOPE_REF_TYPES or (ref_type == "doc" and _is_repo_relative_doc_path(url)):
        ref["scope"] = {"kind": ref_type, "value": _normalize_ref_target(url, ref_type)}
    return ref


def scope_ref_matches_path(ref: dict, candidate_path: str) -> bool:
    """Return whether a normalized scope ref overlaps one repository path."""
    try:
        candidate = _normalize_scope_path(candidate_path)
    except ValueError:
        return False
    scope = ref.get("scope")
    if not isinstance(scope, dict):
        try:
            scope = _serialize_ref(ref).get("scope")
        except (KeyError, ValueError):
            return False
    if not isinstance(scope, dict):
        return False
    if scope.get("kind") == "glob":
        return PurePosixPath(candidate).match(scope["value"])
    return candidate == scope.get("value")


def get_db_path() -> Path:
    env = os.environ.get("SPRINTCTL_DB")
    if env:
        return Path(env)
    return Path.home() / ".sprintctl" / "sprintctl.db"


def _is_busy_or_locked(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(error_code, int) and error_code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(len(_WAL_BUSY_RETRY_DELAYS_SECONDS) + 1):
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            conn.close()
            raise

        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except Exception as exc:
            conn.close()
            if not _is_busy_or_locked(exc) or attempt == len(
                _WAL_BUSY_RETRY_DELAYS_SECONDS
            ):
                raise
            time.sleep(_WAL_BUSY_RETRY_DELAYS_SECONDS[attempt])
            continue
        return conn
    raise AssertionError("unreachable WAL initialization retry state")


def _execute_statements(conn: sqlite3.Connection, sql: str) -> None:
    for statement in sql.split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)


def _column_info(conn: sqlite3.Connection, table_name: str, column_name: str) -> tuple | None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    for row in rows:
        if row[1] == column_name:
            return row
    return None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return _column_info(conn, table_name, column_name) is not None


def _column_is_nullable(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    row = _column_info(conn, table_name, column_name)
    return row is not None and row[3] == 0


def _ensure_schema_version_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO schema_version(version)
        SELECT 0
        WHERE NOT EXISTS (SELECT 1 FROM schema_version)
        """
    )


def _get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY rowid LIMIT 1"
    ).fetchone()
    return row[0] if row is not None else 0


def _migration_1(conn: sqlite3.Connection) -> None:
    _execute_statements(conn, _MIGRATIONS[0])


def _migration_2(conn: sqlite3.Connection) -> None:
    _execute_statements(conn, _MIGRATIONS[1])


def _migration_3(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "sprint", "kind"):
        conn.execute(
            """
            ALTER TABLE sprint ADD COLUMN kind TEXT NOT NULL DEFAULT 'active_sprint'
                CHECK (kind IN ('active_sprint', 'backlog', 'archive'))
            """
        )


def _add_column_if_missing(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    if not _column_exists(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _migration_4(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "claim", "branch", "branch TEXT")
    _add_column_if_missing(conn, "claim", "worktree_path", "worktree_path TEXT")
    _add_column_if_missing(conn, "claim", "commit_sha", "commit_sha TEXT")
    _add_column_if_missing(conn, "claim", "pr_ref", "pr_ref TEXT")


def _migration_5(conn: sqlite3.Connection) -> None:
    if (
        _column_is_nullable(conn, "sprint", "start_date")
        and _column_is_nullable(conn, "sprint", "end_date")
    ):
        return
    _execute_statements(conn, _MIGRATIONS[4])


def _migration_6(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "claim", "claim_token", "claim_token TEXT")
    _add_column_if_missing(conn, "claim", "runtime_session_id", "runtime_session_id TEXT")
    _add_column_if_missing(conn, "claim", "instance_id", "instance_id TEXT")
    _add_column_if_missing(conn, "claim", "hostname", "hostname TEXT")
    _add_column_if_missing(conn, "claim", "pid", "pid INTEGER")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_token
            ON claim(claim_token)
            WHERE claim_token IS NOT NULL
        """
    )


def _migration_7(conn: sqlite3.Connection) -> None:
    _execute_statements(conn, _MIGRATIONS[6])


def _migration_8(conn: sqlite3.Connection) -> None:
    _execute_statements(conn, _MIGRATIONS[7])


def _migration_9(conn: sqlite3.Connection) -> None:
    _execute_statements(conn, _MIGRATIONS[8])


def _migration_10(conn: sqlite3.Connection) -> None:
    """Add portable aggregate UUIDs without changing integer key semantics."""
    for table in ("sprint", "work_item"):
        _add_column_if_missing(conn, table, "aggregate_uuid", "aggregate_uuid TEXT")
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE aggregate_uuid IS NULL OR aggregate_uuid = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                f"UPDATE {table} SET aggregate_uuid = ? WHERE id = ?",
                (str(uuid4()), row[0]),
            )
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_aggregate_uuid "
            f"ON {table}(aggregate_uuid)"
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_aggregate_uuid_required
            BEFORE INSERT ON {table}
            WHEN NEW.aggregate_uuid IS NULL OR NEW.aggregate_uuid = ''
            BEGIN
                SELECT RAISE(ABORT, '{table} aggregate_uuid is required');
            END
            """
        )


def _migration_11(conn: sqlite3.Connection) -> None:
    """Native work item priority (1 = highest). NULL = unprioritized, which
    sorts after any explicit priority in next-work ordering."""
    _add_column_if_missing(
        conn,
        "work_item",
        "priority",
        "priority INTEGER CHECK (priority IS NULL OR (priority >= 1 AND priority <= 9))",
    )


def _migration_12(conn: sqlite3.Connection) -> None:
    """Add explicit claim status for retained remote expiry history."""
    _add_column_if_missing(
        conn, "claim", "status",
        "status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired'))",
    )


def _migration_13(conn: sqlite3.Connection) -> None:
    """Add the future fencing epoch without changing local claim behavior."""
    _add_column_if_missing(
        conn, "claim", "lease_epoch",
        "lease_epoch INTEGER NOT NULL DEFAULT 1 CHECK (lease_epoch >= 1)",
    )


def _migration_14(conn: sqlite3.Connection) -> None:
    """Add explicit scope-ref kinds while retaining every legacy ref row."""
    _execute_statements(
        conn,
        """
        CREATE TABLE ref_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
            ref_type     TEXT    NOT NULL DEFAULT 'other'
                                 CHECK (ref_type IN (
                                     'pr', 'issue', 'doc', 'other',
                                     'file', 'glob', 'manifest'
                                 )),
            url          TEXT    NOT NULL,
            label        TEXT    NOT NULL DEFAULT '',
            created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        INSERT INTO ref_new (id, work_item_id, ref_type, url, label, created_at)
        SELECT id, work_item_id, ref_type, url, label, created_at FROM ref;
        DROP TABLE ref;
        ALTER TABLE ref_new RENAME TO ref;
        """,
    )


def _migration_15(conn: sqlite3.Connection) -> None:
    """Add the 'command' ref kind (validation-command refs) to the ref table."""
    _execute_statements(
        conn,
        """
        CREATE TABLE ref_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
            ref_type     TEXT    NOT NULL DEFAULT 'other'
                                 CHECK (ref_type IN (
                                     'pr', 'issue', 'doc', 'other',
                                     'file', 'glob', 'manifest', 'command'
                                 )),
            url          TEXT    NOT NULL,
            label        TEXT    NOT NULL DEFAULT '',
            created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        INSERT INTO ref_new (id, work_item_id, ref_type, url, label, created_at)
        SELECT id, work_item_id, ref_type, url, label, created_at FROM ref;
        DROP TABLE ref;
        ALTER TABLE ref_new RENAME TO ref;
        """,
    )


def _migration_16(conn: sqlite3.Connection) -> None:
    """Install the exact-plan-bound maintenance capability ledger."""
    _execute_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS maintenance_capability (
            capability_id text PRIMARY KEY,
            envelope_id text NOT NULL UNIQUE,
            envelope_digest text NOT NULL CHECK (length(envelope_digest) = 64),
            envelope_json text NOT NULL,
            plan_ref text NOT NULL,
            operator_identity text NOT NULL,
            not_before text NOT NULL,
            expires_at text NOT NULL,
            state text NOT NULL CHECK (state IN ('prepared','attested','active','observing','reconciled','aborted','revoked','expired')),
            revision integer NOT NULL CHECK (revision >= 1),
            next_sequence integer NOT NULL DEFAULT 1 CHECK (next_sequence >= 1),
            created_at text NOT NULL,
            updated_at text NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_capability_receipt (
            capability_id text NOT NULL REFERENCES maintenance_capability(capability_id) ON DELETE RESTRICT,
            request_id text NOT NULL,
            action text NOT NULL CHECK (action IN ('prepare','attest','activate','observe','reconcile','abort','revoke')),
            outcome text NOT NULL CHECK (outcome IN ('accepted','expired')),
            from_state text,
            to_state text NOT NULL,
            result_revision text NOT NULL,
            step_id text,
            command_ref text,
            effect_ref text,
            audit_bundle_json text,
            request_digest text NOT NULL CHECK (length(request_digest) = 64),
            actor text NOT NULL,
            created_at text NOT NULL,
            PRIMARY KEY (capability_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS maintenance_capability_recovery (
            capability_id text NOT NULL REFERENCES maintenance_capability(capability_id) ON DELETE RESTRICT,
            record_id text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('observation','requested-command')),
            payload_ref text NOT NULL,
            actor text NOT NULL,
            created_at text NOT NULL,
            PRIMARY KEY (capability_id, record_id)
        );
        """,
    )
    for table, label in (
        ("maintenance_capability_receipt", "maintenance capability receipts"),
        ("maintenance_capability_recovery", "maintenance capability recovery records"),
    ):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{label} are immutable'); END"
            )


def _migration_17(conn: sqlite3.Connection) -> None:
    """Install the maintenance observable-resource owner ledger."""
    _execute_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS maintenance_resource (
            resource_ref TEXT PRIMARY KEY,
            capability_id TEXT NOT NULL UNIQUE REFERENCES maintenance_capability(capability_id) ON DELETE RESTRICT,
            recovery_floor INTEGER NOT NULL DEFAULT 0 CHECK (recovery_floor >= 0),
            current_position INTEGER NOT NULL DEFAULT 0 CHECK (current_position >= recovery_floor)
        );
        CREATE TABLE IF NOT EXISTS maintenance_resource_event (
            resource_ref TEXT NOT NULL REFERENCES maintenance_resource(resource_ref) ON DELETE RESTRICT,
            position INTEGER NOT NULL CHECK (position >= 1),
            state TEXT NOT NULL CHECK (state IN ('prepared','attested','active','observing','reconciled','aborted','revoked','expired')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(resource_ref, position)
        );
        """,
    )


def _run_migration(
    conn: sqlite3.Connection,
    target_version: int,
    migrate: Callable[[sqlite3.Connection], None],
    *,
    foreign_keys_off: bool = False,
) -> None:
    try:
        if foreign_keys_off:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        _ensure_schema_version_row(conn)
        if _get_schema_version(conn) >= target_version:
            conn.commit()
            return
        migrate(conn)
        conn.execute("UPDATE schema_version SET version = ?", (target_version,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if foreign_keys_off:
            conn.execute("PRAGMA foreign_keys = ON")


def init_db(conn: sqlite3.Connection) -> None:
    _run_migration(conn, 1, _migration_1)
    _run_migration(conn, 2, _migration_2)
    _run_migration(conn, 3, _migration_3)
    _run_migration(conn, 4, _migration_4)
    _run_migration(conn, 5, _migration_5, foreign_keys_off=True)
    _run_migration(conn, 6, _migration_6)
    _run_migration(conn, 7, _migration_7)
    _run_migration(conn, 8, _migration_8)
    _run_migration(conn, 9, _migration_9)
    _run_migration(conn, 10, _migration_10)
    _run_migration(conn, 11, _migration_11)
    _run_migration(conn, 12, _migration_12)
    _run_migration(conn, 13, _migration_13)
    _run_migration(conn, 14, _migration_14, foreign_keys_off=True)
    _run_migration(conn, 15, _migration_15, foreign_keys_off=True)
    _run_migration(conn, 16, _migration_16)
    _run_migration(conn, CURRENT_SCHEMA_VERSION, _migration_17)


# --- Sprint ---
#
# Sprint-table query shapes live in ``sprintctl.sprintcore`` and are shared
# with the PostgreSQL backend; this module supplies only the SQLite execution
# adapter.  Public signatures are unchanged.


class _SprintSqlite:
    """SQLite execution adapter for ``sprintcore`` sprint operations."""

    ph = "?"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_one(self, sql: str, params: tuple) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mutate(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def insert_id(self, sql: str, params: tuple) -> int:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.lastrowid


def create_sprint(
    conn: sqlite3.Connection,
    name: str,
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    status: str = "planned",
    kind: str = "active_sprint",
) -> int:
    return _sprintcore.create_sprint(
        _SprintSqlite(conn), name, goal, start_date, end_date, status, kind
    )


def get_sprint(conn: sqlite3.Connection, sprint_id: int) -> dict | None:
    return _sprintcore.get_sprint(_SprintSqlite(conn), sprint_id)


def get_active_sprint(conn: sqlite3.Connection) -> dict | None:
    """Return the newest active sprint.

    Multiple active sprints are allowed. Callers that need to reason about
    ambiguity should use list_active_sprints instead.
    """
    return _sprintcore.get_active_sprint(_SprintSqlite(conn))


def list_active_sprints(conn: sqlite3.Connection) -> list[dict]:
    return _sprintcore.list_active_sprints(_SprintSqlite(conn))


def set_sprint_kind(conn: sqlite3.Connection, sprint_id: int, kind: str) -> None:
    _sprintcore.set_sprint_kind(
        _SprintSqlite(conn), sprint_id, kind, valid_kinds=SPRINT_KINDS
    )


def list_sprints(conn: sqlite3.Connection) -> list[dict]:
    return _sprintcore.list_sprints(_SprintSqlite(conn))


# --- Track ---
#
# Track-table query shapes live in ``sprintctl.trackcore`` and are shared
# with the PostgreSQL backend; this module supplies only the SQLite execution
# adapter.  Public signatures are unchanged.


class _TrackSqlite:
    """SQLite execution adapter for ``trackcore`` track operations."""

    ph = "?"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_one(self, sql: str, params: tuple) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def insert_ignore(
        self, columns: tuple[str, ...], values: tuple, conflict_cols: tuple[str, ...]
    ) -> None:
        col_list = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(values))
        self._conn.execute(
            f"INSERT OR IGNORE INTO track ({col_list}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()


def get_or_create_track(
    conn: sqlite3.Connection,
    sprint_id: int,
    name: str,
    description: str = "",
) -> int:
    return _trackcore.get_or_create_track(_TrackSqlite(conn), sprint_id, name, description)


def list_tracks(conn: sqlite3.Connection, sprint_id: int) -> list[dict]:
    return _trackcore.list_tracks(_TrackSqlite(conn), sprint_id)


def get_track(conn: sqlite3.Connection, track_id: int) -> dict | None:
    return _trackcore.get_track(_TrackSqlite(conn), track_id)


# --- WorkItem ---
#
# Work-item CRUD query shapes live in ``sprintctl.workitemcore`` and are
# shared with the PostgreSQL backend; this module supplies only the SQLite
# execution adapter.  Public signatures are unchanged.  CAS/conflict
# operations (update_work_item_description, set_work_item_status) stay here:
# their transactional locking is backend-specific.


class _WorkItemSqlite:
    """SQLite execution adapter for ``workitemcore`` work-item operations."""

    ph = "?"
    updated_at_sql = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_one(self, sql: str, params: tuple) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def insert_id(self, sql: str, params: tuple) -> int:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.lastrowid

    def update_one(self, sql: str, params: tuple) -> bool:
        cur = self._conn.execute(sql, params)
        if cur.rowcount == 0:
            self._conn.rollback()
            return False
        self._conn.commit()
        return True

    def join_tenant_clause(self, left_alias: str, right_alias: str) -> str:
        return ""

    def begin_txn(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def for_update_of(self, alias: str) -> str:
        return ""

    def lock_for_update(self, table: str, id_: int, extra_where: str = "") -> dict | None:
        sql = f"SELECT * FROM {table} WHERE id = ?{extra_where}"
        row = self._conn.execute(sql, (id_,)).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)

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
        return _insert_event(
            self._conn,
            sprint_id,
            actor,
            event_type,
            source_type=source_type,
            work_item_id=work_item_id,
            payload=payload,
        )


def create_work_item(
    conn: sqlite3.Connection,
    sprint_id: int,
    track_id: int,
    title: str,
    description: str = "",
    assignee: str | None = None,
    priority: int | None = None,
) -> int:
    return _workitemcore.create_work_item(
        _WorkItemSqlite(conn), sprint_id, track_id, title, description, assignee, priority
    )


def set_work_item_priority(
    conn: sqlite3.Connection,
    item_id: int,
    priority: int | None,
) -> None:
    _workitemcore.set_work_item_priority(_WorkItemSqlite(conn), item_id, priority)


def get_work_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    return _workitemcore.get_work_item(_WorkItemSqlite(conn), item_id)


_SPRINT_STATUS_REVISION_RE = re.compile(
    r"^sprint:[0-9a-fA-F-]{36}@status:(planned|active|closed)$"
)


def sprint_status_revision(sprint: dict) -> str:
    """Return the CAS token for the status-transition state of one sprint."""
    return f"sprint:{sprint['aggregate_uuid']}@status:{sprint['status']}"


def validate_sprint_status_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _SPRINT_STATUS_REVISION_RE.fullmatch(revision):
        raise ValueError("expected_revision must be a valid sprint status revision")
    return revision


def get_work_item_with_edit_revision(
    conn: sqlite3.Connection, item_id: int
) -> tuple[dict, str] | None:
    """Read an item and its description revision from one SQLite snapshot."""
    return _workitemcore.get_work_item_with_edit_revision(_WorkItemSqlite(conn), item_id)


def update_work_item_description(
    conn: sqlite3.Connection,
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
    return _workitemcore.update_work_item_description(
        _WorkItemSqlite(conn), item_id, description,
        expected_revision=expected_revision, actor=actor,
    )


def list_work_items(
    conn: sqlite3.Connection,
    sprint_id: int | None = None,
    track_name: str | None = None,
    status: str | None = None,
) -> list[dict]:
    return _workitemcore.list_work_items(_WorkItemSqlite(conn), sprint_id, track_name, status)


def set_work_item_status(
    conn: sqlite3.Connection,
    item_id: int,
    new_status: str,
    actor: str | None = None,
    claim_id: int | None = None,
    claim_token: str | None = None,
    *,
    expected_revision: str | None = None,
) -> None:
    if expected_revision is not None:
        expected_revision = validate_item_status_revision(expected_revision)
    wi = _WorkItemSqlite(conn)
    try:
        # SQLite's write transaction is the transition's linearization point.
        # Re-read the item after acquiring it so a stale basis cannot create a
        # coordination event or update a row.
        wi.begin_txn()
        item = wi.lock_for_update("work_item", item_id)
        if item is None:
            raise ValueError(f"Item #{item_id} not found")
        current = item["status"]
        current_revision = item_status_revision(item)
        if expected_revision is not None and expected_revision != current_revision:
            raise StatusConflict(
                f"Item #{item_id} status revision mismatch: "
                f"expected {expected_revision}, current {current_revision}"
            )
        if new_status not in VALID_TRANSITIONS[current]:
            allowed = sorted(VALID_TRANSITIONS[current]) or "none (terminal)"
            raise InvalidTransition(
                f"cannot transition {current} -> {new_status}. Allowed: {allowed}"
            )
        active_claim = _get_active_exclusive_claim_row(conn, item_id)
        if active_claim is not None:
            if claim_id is None or claim_token is None:
                _emit_claim_event(
                    conn,
                    active_claim,
                    event_type="coordination-failure",
                    actor=actor or "system",
                    payload={
                        "summary": f"Item transition rejected for item #{item_id}",
                        "detail": (
                            "An exclusive claim blocked the transition because no "
                            "valid claim proof was supplied."
                        ),
                        "tags": ["claims", "coordination", "ownership-proof"],
                        "operation": "item-status",
                        "reason": "missing-claim-proof",
                        "required_claim": _claim_event_identity(active_claim),
                        "attempted_by": _claim_attempt_identity(actor=actor),
                    },
                )
                raise ClaimConflict(
                    f"Item #{item_id} is exclusively claimed by '{active_claim['agent']}' "
                    f"(claim #{active_claim['id']}). Provide --claim-id and --claim-token."
                )
            selected_claim = conn.execute(
                "SELECT * FROM claim WHERE id = ?", (claim_id,)
            ).fetchone()
            if (
                selected_claim is None
                or selected_claim["work_item_id"] != item_id
                or not selected_claim["exclusive"]
                or selected_claim["claim_type"] != "execute"
                or selected_claim["status"] != "active"
                or selected_claim["expires_at"] <= conn.execute(
                    "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                ).fetchone()[0]
            ):
                _emit_claim_event(
                    conn,
                    active_claim,
                    event_type="coordination-failure",
                    actor=actor or "system",
                    payload={
                        "summary": f"Item transition rejected for item #{item_id}",
                        "detail": (
                            "A transition supplied a claim proof for the wrong claim id "
                            "while another exclusive claim was active."
                        ),
                        "tags": ["claims", "coordination", "ownership-proof"],
                        "operation": "item-status",
                        "reason": "wrong-claim-id",
                        "required_claim": _claim_event_identity(active_claim),
                        "attempted_by": _claim_attempt_identity(
                            actor=actor,
                            claim_id=claim_id,
                            claim_token_present=claim_token is not None,
                        ),
                    },
                )
                raise ClaimConflict(
                    f"Item #{item_id} is exclusively claimed by '{active_claim['agent']}' "
                    f"(claim #{active_claim['id']})."
                )
            _require_claim_proof(selected_claim, claim_token)
        if new_status == "active":
            unresolved = [
                blocker for blocker in list_deps_blocking(conn, item_id)
                if blocker["blocker_status"] != "done"
            ]
            if unresolved:
                blocker_ids = [blocker["item_id"] for blocker in unresolved]
                raise InvalidTransition(
                    f"cannot transition {current} -> active while blockers remain unresolved: {blocker_ids}"
                )
        wi.execute(
            f"UPDATE work_item SET status = ?, updated_at = {wi.updated_at_sql} WHERE id = ?",
            (new_status, item_id),
        )
        wi.commit()
    except Exception:
        wi.rollback()
        raise


def _validate_sprint_transition(current: str, new_status: str) -> None:
    if new_status not in SPRINT_TRANSITIONS[current]:
        allowed = sorted(SPRINT_TRANSITIONS[current]) or "none (terminal)"
        raise InvalidTransition(
            f"cannot transition sprint {current} -> {new_status}. Allowed: {allowed}"
        )


def set_sprint_status(
    conn: sqlite3.Connection,
    sprint_id: int,
    new_status: str,
    *,
    expected_revision: str | None = None,
) -> None:
    if expected_revision is not None:
        expected_revision = validate_sprint_status_revision(expected_revision)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sprint = get_sprint(conn, sprint_id)
        if sprint is None:
            raise ValueError(f"Sprint #{sprint_id} not found")
        current_revision = sprint_status_revision(sprint)
        if expected_revision is not None and expected_revision != current_revision:
            raise StatusConflict(
                f"Sprint #{sprint_id} status revision mismatch: "
                f"expected {expected_revision}, current {current_revision}"
            )
        _validate_sprint_transition(sprint["status"], new_status)
        conn.execute("UPDATE sprint SET status = ? WHERE id = ?", (new_status, sprint_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- Event ---
#
# Event-table query shapes live in ``sprintctl.eventcore`` and are shared
# with the PostgreSQL backend; this module supplies only the SQLite
# execution adapter.  Public signatures are unchanged.  Transactional
# operations (create_event, create_archive_import_event,
# close_sprint_with_boundary_event) stay here: they control their own
# commit/rollback boundary and call other backend-specific functions.


class _EventSqlite:
    """SQLite execution adapter for ``eventcore`` event operations."""

    ph = "?"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def insert_uncommitted(self, sql: str, params: tuple) -> int:
        cur = self._conn.execute(sql, params)
        return int(cur.lastrowid)


def _insert_event(
    conn: sqlite3.Connection,
    sprint_id: int,
    actor: str,
    event_type: str,
    source_type: str = "actor",
    work_item_id: int | None = None,
    payload: dict | None = None,
) -> int:
    canonical_payload = _contracts.canonicalize_event_payload(event_type, payload)
    payload_str = json.dumps(canonical_payload)
    return _eventcore.insert_event_row(
        _EventSqlite(conn), sprint_id, actor, event_type, source_type, work_item_id, payload_str
    )


def create_event(
    conn: sqlite3.Connection,
    sprint_id: int,
    actor: str,
    event_type: str,
    source_type: str = "actor",
    work_item_id: int | None = None,
    payload: dict | None = None,
    expected_project: str | None = None,
) -> int:
    try:
        _contracts.require_generic_event_write_allowed(event_type)
        canonical_payload = _contracts.canonicalize_event_payload(event_type, payload)
        if event_type == _contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE:
            if (
                expected_project is not None
                and canonical_payload["project"] != expected_project
            ):
                raise ValueError(
                    "capability receipt project must match the owning repository "
                    f"{expected_project!r}"
                )
            boundary_event_id = _require_receipt_close_boundary(conn, sprint_id)
            _contracts.verify_capability_receipt_draft_pointer(
                canonical_payload,
                sprint_id=sprint_id,
                boundary_event_id=boundary_event_id,
            )
        event_id = _insert_event(
            conn,
            sprint_id,
            actor,
            event_type,
            source_type=source_type,
            work_item_id=work_item_id,
            payload=canonical_payload,
        )
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise


def _require_receipt_close_boundary(conn: sqlite3.Connection, sprint_id: int) -> int:
    sprint = get_sprint(conn, sprint_id)
    if sprint is None:
        raise ValueError(f"Sprint #{sprint_id} not found")
    if sprint["status"] != "closed":
        raise ValueError("capability-receipt-drafted requires a closed sprint")
    rows = conn.execute(
        "SELECT id FROM event WHERE sprint_id = ? AND event_type = ? ORDER BY id",
        (sprint_id, _contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            "capability-receipt-drafted requires exactly one local sprint-close-boundary"
        )
    return int(rows[0]["id"])


def create_archive_import_event(
    conn: sqlite3.Connection,
    sprint_id: int,
    actor: str,
    event_type: str,
    source_event_id: int,
    work_item_id: int | None = None,
    payload: dict | None = None,
) -> int:
    """Insert a typed event as explicit non-authoritative imported history."""
    imported_type, imported_payload = _contracts.canonicalize_event_for_archive_import(
        event_type,
        payload,
        source_event_id,
    )
    if imported_type == event_type and not _contracts.is_archive_only_event_type(event_type):
        raise ValueError(f"{event_type} is not a reserved typed import event")
    try:
        event_id = _insert_event(
            conn,
            sprint_id,
            actor,
            imported_type,
            source_type="system",
            work_item_id=work_item_id,
            payload=imported_payload,
        )
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise


def close_sprint_with_boundary_event(
    conn: sqlite3.Connection,
    sprint_id: int,
    actor: str,
    *,
    expected_revision: str | None = None,
) -> int:
    """Atomically close an active sprint and append its local boundary event."""
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must be a non-empty string")
    if expected_revision is not None:
        expected_revision = validate_sprint_status_revision(expected_revision)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sprint WHERE id = ?", (sprint_id,)).fetchone()
        if row is None:
            raise ValueError(f"Sprint #{sprint_id} not found")
        sprint = dict(row)
        current = sprint["status"]
        current_revision = sprint_status_revision(sprint)
        if expected_revision is not None and expected_revision != current_revision:
            raise StatusConflict(
                f"Sprint #{sprint_id} status revision mismatch: "
                f"expected {expected_revision}, current {current_revision}"
            )
        _validate_sprint_transition(current, "closed")
        conn.execute("UPDATE sprint SET status = 'closed' WHERE id = ?", (sprint_id,))
        event_id = _insert_event(
            conn,
            sprint_id,
            actor.strip(),
            _contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
            source_type="actor",
            payload={"previous_status": current, "status": "closed"},
        )
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise


def list_events(conn: sqlite3.Connection, sprint_id: int) -> list[dict]:
    return _eventcore.list_events(_EventSqlite(conn), sprint_id)


def list_events_limited(conn: sqlite3.Connection, sprint_id: int, limit: int = 50) -> list[dict]:
    return _eventcore.list_events_limited(_EventSqlite(conn), sprint_id, limit)


def list_takeup_history(
    conn: sqlite3.Connection,
    sprint_id: int | None = None,
) -> dict[str, list[dict]]:
    """Return active and released sprint takeup rows derived from events."""
    return _eventcore.list_takeup_history(_EventSqlite(conn), sprint_id)


def list_active_takeups(
    conn: sqlite3.Connection,
    sprint_id: int | None = None,
) -> list[dict]:
    return _eventcore.list_active_takeups(_EventSqlite(conn), sprint_id)


def list_knowledge_candidates(conn: sqlite3.Connection, sprint_id: int) -> list[dict]:
    """Return events of knowledge types for a sprint, with payload deserialized."""
    return _eventcore.list_knowledge_candidates(_EventSqlite(conn), sprint_id)


# --- Claim ---
#
# Claim query shapes -- including create_claim/handoff_claim's transactional
# bodies as of sub-increments 4d/4e -- live in ``sprintctl.claimcore`` and are
# shared with the PostgreSQL backend; this module supplies only the SQLite
# execution adapter for them.  heartbeat_claim/release_claim stay here: their
# error-path event emission is a thin wrapper around backend-neutral shapes.

CLAIM_IDENTITY_STATUS_PROVEN = _rows.CLAIM_IDENTITY_STATUS_PROVEN
CLAIM_IDENTITY_STATUS_LEGACY = _rows.CLAIM_IDENTITY_STATUS_LEGACY
# Re-exported from claimcore so both backends and existing callers (cli.py,
# pg.py) see one definition; see claimcore.py's module docstring.
MAX_CLAIM_TOKEN_INSERT_RETRIES = _claimcore.MAX_CLAIM_TOKEN_INSERT_RETRIES

# Backend-neutral claim serialization lives in ``sprintctl.rows`` so the
# SQLite and PostgreSQL backends cannot drift apart.  These aliases keep the
# historical ``db`` import surface stable for existing consumers.
_claim_identity_status = _rows.claim_identity_status
_claim_event_identity = _rows.claim_event_identity
_claim_attempt_identity = _rows.claim_attempt_identity
_serialize_claim = _rows.serialize_claim


class _ClaimSqlite:
    """SQLite execution adapter for ``claimcore`` claim operations."""

    ph = "?"
    true_literal = "1"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_one(self, sql: str, params: tuple) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mutate(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def now_sql(self) -> str:
        return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

    def expires_at_offset_sql(self) -> str:
        return f"strftime('%Y-%m-%dT%H:%M:%SZ', 'now', {self.ph} || ' seconds')"

    def join_tenant_clause(self, left_alias: str, right_alias: str) -> str:
        return ""

    def begin_txn(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def execute(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)

    def insert_row(self, sql: str, params: tuple) -> int:
        cur = self._conn.execute(sql, params)
        return cur.lastrowid

    def lock_capability_arbitration(self) -> None:
        pass  # BEGIN IMMEDIATE's whole-DB write lock already covers this

    def lock_work_item_row(self, work_item_id: int) -> dict | None:
        row = self._conn.execute("SELECT id FROM work_item WHERE id = ?", (work_item_id,)).fetchone()
        return dict(row) if row else None

    def is_claim_token_collision(self, exc: BaseException) -> bool:
        if not isinstance(exc, sqlite3.IntegrityError):
            return False
        msg = str(exc).lower()
        return "claim_token" in msg or "idx_claim_token" in msg

    def maintenance_capability_active_sql(self) -> str:
        return "state IN ('active','observing') AND julianday(expires_at) > julianday('now')"

    def emit_claim_event(
        self, claim_row: dict, *, event_type: str, actor: str, payload: dict
    ) -> None:
        _emit_claim_event(self._conn, claim_row, event_type=event_type, actor=actor, payload=payload)


def get_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    *,
    include_secret: bool = False,
) -> dict | None:
    row = _claimcore.get_claim_row(_ClaimSqlite(conn), claim_id)
    return _serialize_claim(row, include_secret=include_secret) if row else None


def _get_active_exclusive_claim_row(
    conn: sqlite3.Connection,
    work_item_id: int,
) -> dict | None:
    return _claimcore.get_active_exclusive_claim_row(_ClaimSqlite(conn), work_item_id)


def _emit_claim_event(
    conn: sqlite3.Connection,
    claim_row: sqlite3.Row | dict,
    *,
    event_type: str,
    actor: str,
    payload: dict,
) -> None:
    item = get_work_item(conn, claim_row["work_item_id"])
    if item is None:
        return
    create_event(
        conn,
        sprint_id=item["sprint_id"],
        actor=actor,
        event_type=event_type,
        source_type="system",
        work_item_id=item["id"],
        payload=payload,
    )


# Re-exported from claimcore so both backends and existing callers (cli.py,
# pg.py's own wrapper) see one definition; see claimcore.py's module docstring.
_require_claim_proof = _claimcore.require_claim_proof


def create_claim(
    conn: sqlite3.Connection,
    work_item_id: int,
    agent: str,
    claim_type: str = "execute",
    exclusive: bool = True,
    ttl_seconds: int = 300,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
    coordinate_claim_id: int | None = None,
    coordinate_claim_token: str | None = None,
) -> int:
    """Create a claim on a work item, enforcing exclusivity for exclusive claim types.

    Sub-agents spawned by a coordinator may pass coordinate_claim_id +
    coordinate_claim_token to create an execute/inspect/review claim under an
    existing coordinate claim without triggering a ClaimConflict.
    """
    if claim_type not in CLAIM_TYPES:
        raise ValueError(f"Invalid claim_type '{claim_type}'. Must be one of: {', '.join(CLAIM_TYPES)}")
    item = get_work_item(conn, work_item_id)
    if item is None:
        raise ValueError(f"Work item #{work_item_id} not found")
    return _claimcore.create_claim(
        _ClaimSqlite(conn), work_item_id, agent, claim_type, exclusive, ttl_seconds,
        branch, worktree_path, commit_sha, pr_ref,
        runtime_session_id, instance_id, hostname, pid,
        coordinate_claim_id, coordinate_claim_token,
    )


def heartbeat_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    claim_token: str | None,
    ttl_seconds: int = 300,
    actor: str | None = None,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> None:
    """Refresh a claim's expiry and heartbeat timestamp."""
    row = _claimcore.get_claim_row(_ClaimSqlite(conn), claim_id)
    if row is None:
        raise ValueError(f"Claim #{claim_id} not found")
    try:
        _require_claim_proof(row, claim_token)
    except ValueError as exc:
        event_type, payload = _claimcore.heartbeat_rejection_event(
            claim_id,
            row,
            str(exc),
            actor=actor,
            claim_token=claim_token,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
        )
        _emit_claim_event(conn, row, event_type=event_type, actor=actor or "system", payload=payload)
        raise
    _claimcore.heartbeat_update(
        _ClaimSqlite(conn),
        claim_id,
        ttl_seconds,
        runtime_session_id,
        instance_id,
        branch,
        worktree_path,
        commit_sha,
        pr_ref,
        hostname,
        pid,
    )


def release_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    claim_token: str | None,
    actor: str | None = None,
) -> None:
    """Release (delete) a claim. Only the owning agent may release it."""
    row = _claimcore.get_claim_row(_ClaimSqlite(conn), claim_id)
    if row is None:
        raise ValueError(f"Claim #{claim_id} not found")
    try:
        _require_claim_proof(row, claim_token)
    except ValueError as exc:
        event_type, payload = _claimcore.release_rejection_event(
            claim_id, row, str(exc), actor=actor, claim_token=claim_token
        )
        _emit_claim_event(conn, row, event_type=event_type, actor=actor or "system", payload=payload)
        raise
    _claimcore.release_delete(_ClaimSqlite(conn), claim_id)


def handoff_claim(
    conn: sqlite3.Connection,
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
    return _claimcore.handoff_claim(
        _ClaimSqlite(conn), claim_id, claim_token,
        actor=actor, mode=mode, ttl_seconds=ttl_seconds,
        runtime_session_id=runtime_session_id, instance_id=instance_id,
        branch=branch, worktree_path=worktree_path, commit_sha=commit_sha, pr_ref=pr_ref,
        hostname=hostname, pid=pid, performed_by=performed_by, note=note,
        allow_legacy_adopt=allow_legacy_adopt,
    )


def list_claims_by_sprint(
    conn: sqlite3.Connection,
    sprint_id: int,
    active_only: bool = True,
    expiring_within_seconds: int | None = None,
) -> list[dict]:
    """List all claims for items in a sprint, optionally filtered to active or expiring soon."""
    rows = _claimcore.list_claims_by_sprint(
        _ClaimSqlite(conn), sprint_id, active_only, expiring_within_seconds
    )
    return [_serialize_claim(r) for r in rows]


def list_claims(conn: sqlite3.Connection, work_item_id: int, active_only: bool = True) -> list[dict]:
    """List claims for a work item; active_only filters to non-expired claims."""
    rows = _claimcore.list_claims(_ClaimSqlite(conn), work_item_id, active_only)
    return [_serialize_claim(r) for r in rows]


def find_claim_by_identity(
    conn: sqlite3.Connection,
    *,
    instance_id: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
    runtime_session_id: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    """Find active claims matching the given identity fields.

    Useful for session resumption when the claim_token is lost but the agent
    knows its own instance_id, runtime_session_id, or hostname+pid.
    At least one of instance_id, runtime_session_id, or (hostname+pid) must be provided.
    Returns serialized claims without the secret token.
    """
    rows = _claimcore.find_claim_by_identity(
        _ClaimSqlite(conn),
        instance_id=instance_id,
        hostname=hostname,
        pid=pid,
        runtime_session_id=runtime_session_id,
        active_only=active_only,
    )
    return [_serialize_claim(r) for r in rows]


def _get_active_coordinate_claim_row(
    conn: sqlite3.Connection,
    work_item_id: int,
) -> dict | None:
    """Return the first active exclusive coordinate claim on the item, if any."""
    return _claimcore.get_active_coordinate_claim_row(_ClaimSqlite(conn), work_item_id)


# --- Ref ---
#
# Ref-table query shapes live in ``sprintctl.refcore`` and are shared with
# the PostgreSQL backend; this module supplies only the SQLite execution
# adapter.  Public signatures are unchanged.


class _RefSqlite:
    """SQLite execution adapter for ``refcore`` ref operations."""

    ph = "?"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_one(self, sql: str, params: tuple) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mutate(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def insert_id(self, sql: str, params: tuple) -> int:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.lastrowid


def add_ref(
    conn: sqlite3.Connection,
    work_item_id: int,
    ref_type: str,
    url: str,
    label: str = "",
) -> int:
    if ref_type not in REF_TYPES:
        raise ValueError(f"Invalid ref_type '{ref_type}'. Must be one of: {', '.join(REF_TYPES)}")
    url = _normalize_ref_target(url, ref_type)
    item = get_work_item(conn, work_item_id)
    if item is None:
        raise ValueError(f"Work item #{work_item_id} not found")
    return _refcore.add_ref(_RefSqlite(conn), work_item_id, ref_type, url, label)


def list_refs(conn: sqlite3.Connection, work_item_id: int) -> list[dict]:
    return [_serialize_ref(r) for r in _refcore.list_refs(_RefSqlite(conn), work_item_id)]


def list_refs_for_items(conn: sqlite3.Connection, work_item_ids: list[int]) -> dict[int, list[dict]]:
    rows = _refcore.list_refs_for_items(_RefSqlite(conn), work_item_ids)
    refs_by_item: dict[int, list[dict]] = {}
    for r in rows:
        refs_by_item.setdefault(r["work_item_id"], []).append(_serialize_ref(r))
    return refs_by_item


def remove_ref(conn: sqlite3.Connection, ref_id: int, work_item_id: int) -> None:
    _refcore.remove_ref(_RefSqlite(conn), ref_id, work_item_id)


# --- Dep ---
#
# Dep-table query shapes live in ``sprintctl.depcore`` and are shared with
# the PostgreSQL backend; this module supplies only the SQLite execution
# adapter.  Public signatures are unchanged.


class _DepSqlite:
    """SQLite execution adapter for ``depcore`` dep operations."""

    ph = "?"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def tenant_params(self) -> tuple:
        return ()

    def query_one(self, sql: str, params: tuple) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def query_all(self, sql: str, params: tuple) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mutate(self, sql: str, params: tuple) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def insert_ignore(
        self, columns: tuple[str, ...], values: tuple, conflict_cols: tuple[str, ...]
    ) -> None:
        col_list = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(values))
        self._conn.execute(
            f"INSERT OR IGNORE INTO dep ({col_list}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()

    def join_tenant_clause(self, left_alias: str, right_alias: str) -> str:
        return ""


def add_dep(
    conn: sqlite3.Connection,
    item_id: int,
    blocked_item_id: int,
) -> int:
    """Record that item_id must be done before blocked_item_id can start.

    Both items must exist. An item cannot depend on itself.
    Duplicate deps are ignored (returns existing dep id).
    """
    if item_id == blocked_item_id:
        raise ValueError("An item cannot depend on itself")
    if get_work_item(conn, item_id) is None:
        raise ValueError(f"Work item #{item_id} not found")
    if get_work_item(conn, blocked_item_id) is None:
        raise ValueError(f"Work item #{blocked_item_id} not found")
    return _depcore.add_dep(_DepSqlite(conn), item_id, blocked_item_id)


def list_deps_blocking(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """Return deps where item_id is blocked — i.e. items that must complete first."""
    return _depcore.list_deps_blocking(_DepSqlite(conn), item_id)


def list_deps_blocked_by(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """Return deps where item_id is the blocker — i.e. items waiting on it."""
    return _depcore.list_deps_blocked_by(_DepSqlite(conn), item_id)


def remove_dep(conn: sqlite3.Connection, dep_id: int, item_id: int) -> None:
    """Remove a dep. item_id must match either item_id or blocked_item_id."""
    _depcore.remove_dep(_DepSqlite(conn), dep_id, item_id)


def get_ready_items(
    conn: sqlite3.Connection,
    sprint_id: int,
) -> list[dict]:
    """Return pending items in the sprint that have no unresolved blocking deps.

    An item is ready if all its blockers (items that must complete first) are done.
    Items with no deps at all are always ready.
    """
    items = list_work_items(conn, sprint_id=sprint_id, status="pending")
    ready = []
    for item in items:
        blockers = list_deps_blocking(conn, item["id"])
        unresolved = [b for b in blockers if b["blocker_status"] != "done"]
        if not unresolved:
            ready.append({
                **item,
                "blockers_resolved": len(blockers),
                "unresolved_blockers": 0,
                "effective_priority": effective_priority(item),
            })
        else:
            # Include items with unresolved blockers so caller can inspect
            item_with_deps = {
                **item,
                "blockers_resolved": len(blockers) - len(unresolved),
                "unresolved_blockers": len(unresolved),
                "unresolved_blocker_ids": [b["item_id"] for b in unresolved],
            }
            _ = item_with_deps  # not included in ready list
    ready.sort(
        key=lambda it: (
            it["effective_priority"] is None,
            it["effective_priority"] or 0,
            it["created_at"],
            it["id"],
        )
    )
    return ready


def force_item_done_for_carryover(conn: sqlite3.Connection, item_id: int) -> None:
    """Set item status to 'done' bypassing the state machine. Carryover use only."""
    conn.execute(
        "UPDATE work_item SET status = 'done', "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (item_id,),
    )


# --- Backlog seeding ---

def backlog_seed_from_candidates(
    conn: sqlite3.Connection,
    source_sprint_id: int,
    target_sprint_id: int,
    actor: str = "system",
) -> list[dict]:
    """Create backlog items in target_sprint from knowledge candidates in source_sprint.

    Returns only the newly created items. Candidates already seeded (tracked via
    backlog-seeded events in target_sprint) are skipped — making the call idempotent.
    """
    if get_sprint(conn, source_sprint_id) is None:
        raise ValueError(f"Source sprint #{source_sprint_id} not found")
    if get_sprint(conn, target_sprint_id) is None:
        raise ValueError(f"Target sprint #{target_sprint_id} not found")

    candidates = list_knowledge_candidates(conn, source_sprint_id)
    if not candidates:
        return []

    # Find already-seeded source event IDs to avoid duplicates.
    existing_events = list_events(conn, target_sprint_id)
    already_seeded: set[int] = set()
    for ev in existing_events:
        if ev["event_type"] == "backlog-seeded":
            p = json.loads(ev.get("payload") or "{}")
            if "source_event_id" in p:
                already_seeded.add(p["source_event_id"])

    track_id = get_or_create_track(conn, target_sprint_id, "knowledge")
    seeded = []
    for candidate in candidates:
        if candidate["id"] in already_seeded:
            continue
        summary = candidate["payload"].get("summary", f"Knowledge item from event #{candidate['id']}")
        title = f"[knowledge] {summary}"
        item_id = create_work_item(conn, target_sprint_id, track_id, title)
        create_event(
            conn,
            target_sprint_id,
            actor=actor,
            event_type="backlog-seeded",
            source_type="system",
            work_item_id=item_id,
            payload={
                "source_sprint_id": source_sprint_id,
                "source_item_id": candidate.get("work_item_id"),
                "source_event_id": candidate["id"],
                "source_event_type": candidate["event_type"],
            },
        )
        seeded.append(get_work_item(conn, item_id))
    return seeded


# --- Database maintenance ---

_RECOVERY_TABLE_ORDER = ("sprint", "track", "work_item", "event", "claim", "ref", "dep")


class RecoverySchemaMismatch(Exception):
    """A snapshot row's column set does not exactly match the local SQLite table."""

    def __init__(self, table: str, missing: list[str], unexpected: list[str]):
        self.table = table
        self.missing = missing
        self.unexpected = unexpected
        super().__init__(
            f"recovery schema mismatch on '{table}': "
            f"missing={missing} unexpected={unexpected}"
        )


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def write_recovery_snapshot(
    conn: sqlite3.Connection,
    snapshot: dict[str, list[dict]],
    *,
    provenance: dict | None = None,
) -> dict[str, int]:
    """Bulk-insert a repo-scoped Postgres snapshot into a fresh local SQLite
    database, preserving original row IDs. Bypasses status-transition and
    claim-lifecycle validation by design: this restores a prior authoritative
    state rather than replaying business operations.

    Ownership is not restored: claim_token is stripped from every claim row
    and active claims are closed as 'expired'. A recovered database is a new
    authority instance — pre-recovery credentials must not work against it,
    and the file must never carry usable secrets.

    Every snapshot row must match the local table's column set exactly
    (modulo the Postgres-only repo_id); any drift raises
    RecoverySchemaMismatch instead of silently writing partial rows.

    When provenance is given, one synthetic recovery.completed event is
    written per recovered sprint inside the same transaction, so provenance
    is all-or-nothing with the data. Returned counts cover source rows only.
    """
    if provenance is not None:
        _contracts.require_generic_event_write_allowed("recovery.completed")
    counts: dict[str, int] = {}
    claims_closed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in _RECOVERY_TABLE_ORDER:
            rows = snapshot.get(table, [])
            table_cols = set(_table_columns(conn, table))
            n = 0
            for row in rows:
                row_cols = set(row) - {"repo_id"}
                if row_cols != table_cols:
                    raise RecoverySchemaMismatch(
                        table=table,
                        missing=sorted(table_cols - row_cols),
                        unexpected=sorted(row_cols - table_cols),
                    )
                insert_cols = [c for c in row if c != "repo_id"]
                values = []
                for col in insert_cols:
                    value = row[col]
                    if table == "event" and col == "payload" and not isinstance(value, str):
                        value = json.dumps(value)
                    elif table == "claim" and col == "exclusive":
                        value = 1 if value else 0
                    elif table == "claim" and col == "claim_token":
                        value = None
                    elif table == "claim" and col == "status" and value == "active":
                        value = "expired"
                        claims_closed += 1
                    values.append(value)
                placeholders = ",".join("?" for _ in insert_cols)
                conn.execute(
                    f"INSERT INTO {table} ({','.join(insert_cols)}) VALUES ({placeholders})",
                    values,
                )
                n += 1
            counts[table] = n
        if provenance is not None:
            payload = dict(provenance)
            payload["source_row_counts"] = counts
            payload["claims_closed"] = claims_closed
            for sprint_row in snapshot.get("sprint", []):
                _insert_event(
                    conn,
                    sprint_row["id"],
                    "sprintctl",
                    "recovery.completed",
                    source_type="system",
                    payload=payload,
                )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


def vacuum_database(conn: sqlite3.Connection) -> dict:
    """Run VACUUM and report page counts and reclaimed bytes."""
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.commit()  # VACUUM cannot run inside a transaction
    conn.execute("VACUUM")
    pages_after = conn.execute("PRAGMA page_count").fetchone()[0]
    return {
        "backend": "local",
        "page_size": page_size,
        "pages_before": pages_before,
        "pages_after": pages_after,
        "freelist_pages_before": freelist_before,
        "bytes_reclaimed": max(0, (pages_before - pages_after) * page_size),
    }


def check_integrity(conn: sqlite3.Connection) -> dict:
    """Run integrity_check and foreign_key_check; report structural health."""
    integrity_rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    fk_rows = [
        {"table": r[0], "rowid": r[1], "parent": r[2], "fk_index": r[3]}
        for r in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]
    table_counts = {}
    for table in ("sprint", "track", "work_item", "event", "claim", "ref", "dep"):
        table_counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table}"  # noqa: S608 — fixed identifier set
        ).fetchone()[0]
    ok = integrity_rows == ["ok"] and not fk_rows
    return {
        "backend": "local",
        "ok": ok,
        "integrity_check": integrity_rows,
        "foreign_key_violations": fk_rows,
        "table_counts": table_counts,
    }
