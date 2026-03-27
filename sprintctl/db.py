import json
import os
import sqlite3
from pathlib import Path


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
    # Migration 2: claim table (schema present, no CLI exposure — Phase 2.5)
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
]


def get_db_path() -> Path:
    env = os.environ.get("SPRINTCTL_DB")
    if env:
        return Path(env)
    return Path.home() / ".sprintctl" / "sprintctl.db"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (0)")
        current = 0
    else:
        current = row[0]

    for i, migration_sql in enumerate(_MIGRATIONS):
        target_version = i + 1
        if current < target_version:
            for statement in migration_sql.split(";"):
                stmt = statement.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute("UPDATE schema_version SET version = ?", (target_version,))
            current = target_version

    conn.commit()


# --- Sprint ---

def create_sprint(
    conn: sqlite3.Connection,
    name: str,
    goal: str,
    start_date: str,
    end_date: str,
    status: str = "planned",
    kind: str = "active_sprint",
) -> int:
    cur = conn.execute(
        "INSERT INTO sprint (name, goal, start_date, end_date, status, kind) VALUES (?, ?, ?, ?, ?, ?)",
        (name, goal, start_date, end_date, status, kind),
    )
    conn.commit()
    return cur.lastrowid


def get_sprint(conn: sqlite3.Connection, sprint_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sprint WHERE id = ?", (sprint_id,)).fetchone()
    return dict(row) if row else None


def get_active_sprint(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sprint WHERE status = 'active' AND kind = 'active_sprint' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def set_sprint_kind(conn: sqlite3.Connection, sprint_id: int, kind: str) -> None:
    if kind not in SPRINT_KINDS:
        raise ValueError(f"Invalid kind '{kind}'. Must be one of: {', '.join(SPRINT_KINDS)}")
    sprint = get_sprint(conn, sprint_id)
    if sprint is None:
        raise ValueError(f"Sprint #{sprint_id} not found")
    conn.execute("UPDATE sprint SET kind = ? WHERE id = ?", (kind, sprint_id))
    conn.commit()


def list_sprints(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sprint ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# --- Track ---

def get_or_create_track(
    conn: sqlite3.Connection,
    sprint_id: int,
    name: str,
    description: str = "",
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO track (sprint_id, name, description) VALUES (?, ?, ?)",
        (sprint_id, name, description),
    )
    row = conn.execute(
        "SELECT id FROM track WHERE sprint_id = ? AND name = ?", (sprint_id, name)
    ).fetchone()
    conn.commit()
    return row[0]


def list_tracks(conn: sqlite3.Connection, sprint_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM track WHERE sprint_id = ? ORDER BY created_at ASC", (sprint_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# --- WorkItem ---

def create_work_item(
    conn: sqlite3.Connection,
    sprint_id: int,
    track_id: int,
    title: str,
    description: str = "",
    assignee: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO work_item (sprint_id, track_id, title, description, assignee) VALUES (?, ?, ?, ?, ?)",
        (sprint_id, track_id, title, description, assignee),
    )
    conn.commit()
    return cur.lastrowid


def get_work_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM work_item WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_work_items(
    conn: sqlite3.Connection,
    sprint_id: int | None = None,
    track_name: str | None = None,
    status: str | None = None,
) -> list[dict]:
    query = """
        SELECT wi.*, t.name AS track_name
        FROM work_item wi
        JOIN track t ON wi.track_id = t.id
        WHERE 1=1
    """
    params: list = []
    if sprint_id is not None:
        query += " AND wi.sprint_id = ?"
        params.append(sprint_id)
    if track_name is not None:
        query += " AND t.name = ?"
        params.append(track_name)
    if status is not None:
        query += " AND wi.status = ?"
        params.append(status)
    query += " ORDER BY wi.created_at ASC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def set_work_item_status(conn: sqlite3.Connection, item_id: int, new_status: str) -> None:
    item = get_work_item(conn, item_id)
    if item is None:
        raise ValueError(f"Item #{item_id} not found")
    current = item["status"]
    if new_status not in VALID_TRANSITIONS[current]:
        allowed = sorted(VALID_TRANSITIONS[current]) or "none (terminal)"
        raise InvalidTransition(
            f"cannot transition {current} -> {new_status}. Allowed: {allowed}"
        )
    conn.execute(
        "UPDATE work_item SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (new_status, item_id),
    )
    conn.commit()


def set_sprint_status(conn: sqlite3.Connection, sprint_id: int, new_status: str) -> None:
    sprint = get_sprint(conn, sprint_id)
    if sprint is None:
        raise ValueError(f"Sprint #{sprint_id} not found")
    current = sprint["status"]
    if new_status not in SPRINT_TRANSITIONS[current]:
        allowed = sorted(SPRINT_TRANSITIONS[current]) or "none (terminal)"
        raise InvalidTransition(
            f"cannot transition sprint {current} -> {new_status}. Allowed: {allowed}"
        )
    conn.execute("UPDATE sprint SET status = ? WHERE id = ?", (new_status, sprint_id))
    conn.commit()


# --- Event ---

def create_event(
    conn: sqlite3.Connection,
    sprint_id: int,
    actor: str,
    event_type: str,
    source_type: str = "actor",
    work_item_id: int | None = None,
    payload: dict | None = None,
) -> int:
    payload_str = json.dumps(payload or {})
    cur = conn.execute(
        "INSERT INTO event (sprint_id, work_item_id, source_type, actor, event_type, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (sprint_id, work_item_id, source_type, actor, event_type, payload_str),
    )
    conn.commit()
    return cur.lastrowid


def list_events(conn: sqlite3.Connection, sprint_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM event WHERE sprint_id = ? ORDER BY created_at ASC", (sprint_id,)
    ).fetchall()
    return [dict(r) for r in rows]
