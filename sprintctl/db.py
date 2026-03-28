import json
import os
import secrets
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
    goal: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
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


def set_work_item_status(
    conn: sqlite3.Connection,
    item_id: int,
    new_status: str,
    actor: str | None = None,
    claim_id: int | None = None,
    claim_token: str | None = None,
) -> None:
    item = get_work_item(conn, item_id)
    if item is None:
        raise ValueError(f"Item #{item_id} not found")
    current = item["status"]
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
        if claim_id != active_claim["id"]:
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
        _require_claim_proof(active_claim, claim_token)
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


def list_events_limited(conn: sqlite3.Connection, sprint_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM event WHERE sprint_id = ? ORDER BY created_at DESC LIMIT ?",
        (sprint_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- Claim ---

CLAIM_TYPES = ("inspect", "execute", "review", "coordinate")


class ClaimConflict(ValueError):
    pass


CLAIM_IDENTITY_STATUS_PROVEN = "proven"
CLAIM_IDENTITY_STATUS_LEGACY = "legacy_ambiguous"


def _generate_claim_token() -> str:
    return secrets.token_urlsafe(24)


def _claim_identity_status(row: sqlite3.Row | dict) -> str:
    return (
        CLAIM_IDENTITY_STATUS_PROVEN
        if row["claim_token"]
        else CLAIM_IDENTITY_STATUS_LEGACY
    )


def _claim_event_identity(row: sqlite3.Row | dict) -> dict:
    return {
        "claim_id": row["id"],
        "actor": row["agent"],
        "runtime_session_id": row["runtime_session_id"],
        "instance_id": row["instance_id"],
        "branch": row["branch"],
        "worktree_path": row["worktree_path"],
        "commit_sha": row["commit_sha"],
        "pr_ref": row["pr_ref"],
        "hostname": row["hostname"],
        "pid": row["pid"],
        "claim_token_present": bool(row["claim_token"]),
        "identity_status": _claim_identity_status(row),
    }


def _claim_attempt_identity(
    *,
    actor: str | None = None,
    claim_id: int | None = None,
    claim_token_present: bool = False,
    runtime_session_id: str | None = None,
    instance_id: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    commit_sha: str | None = None,
    pr_ref: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> dict:
    return {
        "claim_id": claim_id,
        "actor": actor,
        "runtime_session_id": runtime_session_id,
        "instance_id": instance_id,
        "branch": branch,
        "worktree_path": worktree_path,
        "commit_sha": commit_sha,
        "pr_ref": pr_ref,
        "hostname": hostname,
        "pid": pid,
        "claim_token_present": claim_token_present,
    }


def _serialize_claim(row: sqlite3.Row | dict, *, include_secret: bool = False) -> dict:
    raw = dict(row)
    claim_token = raw.get("claim_token")
    identity_status = _claim_identity_status(row)
    if not include_secret:
        raw.pop("claim_token", None)
    claim = {
        **raw,
        "claim_id": raw["id"],
        "actor": raw["agent"],
        "claim_token_present": bool(claim_token),
        "claim_token_redacted": bool(claim_token) and not include_secret,
        "identity_status": identity_status,
        "identity": {
            "claim_id": raw["id"],
            "actor": raw["agent"],
            "runtime_session_id": raw.get("runtime_session_id"),
            "instance_id": raw.get("instance_id"),
            "advisory": {
                "branch": raw.get("branch"),
                "worktree_path": raw.get("worktree_path"),
                "commit_sha": raw.get("commit_sha"),
                "pr_ref": raw.get("pr_ref"),
                "hostname": raw.get("hostname"),
                "pid": raw.get("pid"),
            },
        },
        "ownership_proof": {
            "type": "claim_id+claim_token",
            "claim_id": raw["id"],
            "claim_token_required": bool(raw["exclusive"]),
            "claim_token_present": bool(claim_token),
            "status": (
                "verified-capable"
                if claim_token
                else "ambiguous-legacy-claim"
            ),
        },
    }
    if include_secret:
        claim["claim_token"] = claim_token
        claim["ownership_proof"]["claim_token"] = claim_token
    return claim


def get_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    *,
    include_secret: bool = False,
) -> dict | None:
    row = conn.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    return _serialize_claim(row, include_secret=include_secret) if row else None


def _get_active_exclusive_claim_row(
    conn: sqlite3.Connection,
    work_item_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM claim
        WHERE work_item_id = ? AND exclusive = 1
          AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (work_item_id,),
    ).fetchone()


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


def _require_claim_proof(row: sqlite3.Row | dict, claim_token: str | None) -> None:
    if not row["claim_token"]:
        raise ValueError(
            f"Claim #{row['id']} is a legacy ambiguous claim with no claim_token. "
            "Use explicit handoff to adopt it or wait for expiry."
        )
    if not claim_token:
        raise ValueError(f"Claim #{row['id']} requires --claim-token")
    if row["claim_token"] != claim_token:
        raise ValueError(f"Invalid claim_token for claim #{row['id']}")


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
    if exclusive:
        conflict = _get_active_exclusive_claim_row(conn, work_item_id)
        if conflict:
            # Allow sub-agent claim if the conflict IS the coordinate claim being delegated under.
            if (
                conflict["claim_type"] == "coordinate"
                and coordinate_claim_id is not None
                and coordinate_claim_id == conflict["id"]
            ):
                coord_row = conn.execute(
                    "SELECT * FROM claim WHERE id = ?", (coordinate_claim_id,)
                ).fetchone()
                if coord_row is None:
                    raise ValueError(f"Coordinate claim #{coordinate_claim_id} not found")
                _require_claim_proof(coord_row, coordinate_claim_token)
                # Permit the sub-agent claim — fall through to INSERT below.
            else:
                raise ClaimConflict(
                    f"Item #{work_item_id} is exclusively claimed by '{conflict['agent']}' (claim #{conflict['id']})"
                )
    claim_token = _generate_claim_token()
    cur = conn.execute(
        """
        INSERT INTO claim
            (work_item_id, agent, claim_type, exclusive, expires_at,
             branch, worktree_path, commit_sha, pr_ref,
             claim_token, runtime_session_id, instance_id, hostname, pid)
        VALUES (?, ?, ?, ?,
                strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' seconds'),
                ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (work_item_id, agent, claim_type, 1 if exclusive else 0, ttl_seconds,
         branch, worktree_path, commit_sha, pr_ref,
         claim_token, runtime_session_id, instance_id, hostname, pid),
    )
    conn.commit()
    return cur.lastrowid


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
    row = conn.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"Claim #{claim_id} not found")
    try:
        _require_claim_proof(row, claim_token)
    except ValueError as exc:
        if row["claim_token"]:
            event_type = "coordination-failure"
            summary = f"Claim heartbeat rejected for claim #{claim_id}"
            detail = str(exc)
            tags = ["claims", "coordination", "heartbeat"]
        else:
            event_type = "claim-ambiguity-detected"
            summary = f"Legacy claim ambiguity detected for claim #{claim_id}"
            detail = str(exc)
            tags = ["claims", "coordination", "ambiguity", "legacy"]
        _emit_claim_event(
            conn,
            row,
            event_type=event_type,
            actor=actor or "system",
            payload={
                "summary": summary,
                "detail": detail,
                "tags": tags,
                "operation": "heartbeat",
                "reason": "invalid-claim-proof" if row["claim_token"] else "legacy-ambiguous-claim",
                "claim": _claim_event_identity(row),
                "attempted_by": _claim_attempt_identity(
                    actor=actor,
                    claim_id=claim_id,
                    claim_token_present=claim_token is not None,
                    runtime_session_id=runtime_session_id,
                    instance_id=instance_id,
                    branch=branch,
                    worktree_path=worktree_path,
                    commit_sha=commit_sha,
                    pr_ref=pr_ref,
                    hostname=hostname,
                    pid=pid,
                ),
            },
        )
        raise
    conn.execute(
        """
        UPDATE claim
        SET heartbeat = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
            expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' seconds'),
            runtime_session_id = COALESCE(?, runtime_session_id),
            instance_id = COALESCE(?, instance_id),
            branch = COALESCE(?, branch),
            worktree_path = COALESCE(?, worktree_path),
            commit_sha = COALESCE(?, commit_sha),
            pr_ref = COALESCE(?, pr_ref),
            hostname = COALESCE(?, hostname),
            pid = COALESCE(?, pid)
        WHERE id = ?
        """,
        (
            ttl_seconds,
            runtime_session_id,
            instance_id,
            branch,
            worktree_path,
            commit_sha,
            pr_ref,
            hostname,
            pid,
            claim_id,
        ),
    )
    conn.commit()


def release_claim(
    conn: sqlite3.Connection,
    claim_id: int,
    claim_token: str | None,
    actor: str | None = None,
) -> None:
    """Release (delete) a claim. Only the owning agent may release it."""
    row = conn.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"Claim #{claim_id} not found")
    try:
        _require_claim_proof(row, claim_token)
    except ValueError as exc:
        _emit_claim_event(
            conn,
            row,
            event_type=(
                "claim-ambiguity-detected"
                if not row["claim_token"]
                else "coordination-failure"
            ),
            actor=actor or "system",
            payload={
                "summary": f"Claim release rejected for claim #{claim_id}",
                "detail": str(exc),
                "tags": (
                    ["claims", "coordination", "ambiguity", "legacy"]
                    if not row["claim_token"]
                    else ["claims", "coordination", "release"]
                ),
                "operation": "release",
                "reason": "invalid-claim-proof" if row["claim_token"] else "legacy-ambiguous-claim",
                "claim": _claim_event_identity(row),
                "attempted_by": _claim_attempt_identity(
                    actor=actor,
                    claim_id=claim_id,
                    claim_token_present=claim_token is not None,
                ),
            },
        )
        raise
    conn.execute("DELETE FROM claim WHERE id = ?", (claim_id,))
    conn.commit()


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
    row = conn.execute("SELECT * FROM claim WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"Claim #{claim_id} not found")
    if mode not in {"transfer", "rotate"}:
        raise ValueError("mode must be 'transfer' or 'rotate'")

    legacy_ambiguous = not bool(row["claim_token"])
    if legacy_ambiguous:
        if not allow_legacy_adopt:
            _emit_claim_event(
                conn,
                row,
                event_type="claim-ambiguity-detected",
                actor=performed_by or actor,
                payload={
                    "summary": f"Legacy claim ambiguity detected for claim #{claim_id}",
                    "detail": (
                        "An explicit handoff was attempted for a legacy claim without a "
                        "claim_token. Re-run with legacy adoption enabled to mint a new proof."
                    ),
                    "tags": ["claims", "coordination", "ambiguity", "legacy"],
                    "operation": "handoff",
                    "reason": "legacy-ambiguous-claim",
                    "claim": _claim_event_identity(row),
                    "attempted_by": _claim_attempt_identity(
                        actor=performed_by or actor,
                        claim_id=claim_id,
                        claim_token_present=claim_token is not None,
                        runtime_session_id=runtime_session_id,
                        instance_id=instance_id,
                        branch=branch,
                        worktree_path=worktree_path,
                        commit_sha=commit_sha,
                        pr_ref=pr_ref,
                        hostname=hostname,
                        pid=pid,
                    ),
                },
            )
            raise ValueError(
                f"Claim #{claim_id} is a legacy ambiguous claim with no claim_token. "
                "Use allow_legacy_adopt to mint a new ownership proof."
            )
        mode = "rotate"
    else:
        try:
            _require_claim_proof(row, claim_token)
        except ValueError as exc:
            _emit_claim_event(
                conn,
                row,
                event_type="coordination-failure",
                actor=performed_by or actor,
                payload={
                    "summary": f"Claim handoff rejected for claim #{claim_id}",
                    "detail": str(exc),
                    "tags": ["claims", "coordination", "handoff"],
                    "operation": "handoff",
                    "reason": "invalid-claim-proof",
                    "claim": _claim_event_identity(row),
                    "attempted_by": _claim_attempt_identity(
                        actor=performed_by or actor,
                        claim_id=claim_id,
                        claim_token_present=claim_token is not None,
                        runtime_session_id=runtime_session_id,
                        instance_id=instance_id,
                        branch=branch,
                        worktree_path=worktree_path,
                        commit_sha=commit_sha,
                        pr_ref=pr_ref,
                        hostname=hostname,
                        pid=pid,
                    ),
                },
            )
            raise

    from_identity = _claim_event_identity(row)
    next_claim_token = row["claim_token"]
    if mode == "rotate" or not next_claim_token:
        next_claim_token = _generate_claim_token()

    conn.execute(
        """
        UPDATE claim
        SET agent = ?,
            claim_token = ?,
            expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' seconds'),
            runtime_session_id = ?,
            instance_id = ?,
            branch = ?,
            worktree_path = ?,
            commit_sha = ?,
            pr_ref = ?,
            hostname = ?,
            pid = ?,
            heartbeat = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        WHERE id = ?
        """,
        (
            actor,
            next_claim_token,
            ttl_seconds,
            runtime_session_id,
            instance_id,
            branch,
            worktree_path,
            commit_sha,
            pr_ref,
            hostname,
            pid,
            claim_id,
        ),
    )
    conn.commit()

    updated = get_claim(conn, claim_id, include_secret=True)
    assert updated is not None
    event_type = "claim-ownership-corrected" if legacy_ambiguous else "claim-handoff"
    _emit_claim_event(
        conn,
        updated,
        event_type=event_type,
        actor=performed_by or actor,
        payload={
            "summary": (
                f"Claim #{claim_id} ownership corrected"
                if legacy_ambiguous
                else f"Claim #{claim_id} handed off to {actor}"
            ),
            "detail": note
            or (
                "A legacy ambiguous claim was explicitly adopted and re-issued with a new token."
                if legacy_ambiguous
                else f"Claim ownership was transferred with mode={mode}."
            ),
            "tags": ["claims", "handoff", "coordination"],
            "operation": "handoff",
            "mode": mode,
            "legacy_adopted": legacy_ambiguous,
            "token_rotated": mode == "rotate" or legacy_ambiguous,
            "from_identity": from_identity,
            "to_identity": _claim_event_identity(updated),
        },
    )
    return updated


def list_claims_by_sprint(
    conn: sqlite3.Connection,
    sprint_id: int,
    active_only: bool = True,
    expiring_within_seconds: int | None = None,
) -> list[dict]:
    """List all claims for items in a sprint, optionally filtered to active or expiring soon."""
    base = """
        SELECT c.*, wi.title AS item_title, wi.status AS item_status
        FROM claim c
        JOIN work_item wi ON c.work_item_id = wi.id
        WHERE wi.sprint_id = ?
    """
    params: list = [sprint_id]
    if active_only:
        base += " AND c.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    if expiring_within_seconds is not None:
        base += " AND c.expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' seconds')"
        params.append(expiring_within_seconds)
    base += " ORDER BY c.expires_at ASC"
    rows = conn.execute(base, params).fetchall()
    return [_serialize_claim(r) for r in rows]


def list_claims(conn: sqlite3.Connection, work_item_id: int, active_only: bool = True) -> list[dict]:
    """List claims for a work item; active_only filters to non-expired claims."""
    if active_only:
        rows = conn.execute(
            """
            SELECT * FROM claim
            WHERE work_item_id = ? AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')
            ORDER BY created_at ASC
            """,
            (work_item_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM claim WHERE work_item_id = ? ORDER BY created_at ASC",
            (work_item_id,),
        ).fetchall()
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
    if not any([instance_id, runtime_session_id, (hostname and pid is not None)]):
        raise ValueError(
            "At least one of --instance-id, --runtime-session-id, or "
            "--hostname + --pid must be provided to resume a claim."
        )
    conditions = []
    params: list = []
    if active_only:
        conditions.append("expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')")
    if instance_id:
        conditions.append("instance_id = ?")
        params.append(instance_id)
    if runtime_session_id:
        conditions.append("runtime_session_id = ?")
        params.append(runtime_session_id)
    if hostname and pid is not None:
        conditions.append("(hostname = ? AND pid = ?)")
        params.extend([hostname, pid])
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT * FROM claim WHERE {where} ORDER BY created_at DESC",
        params,
    ).fetchall()
    return [_serialize_claim(r) for r in rows]


def _get_active_coordinate_claim_row(
    conn: sqlite3.Connection,
    work_item_id: int,
) -> sqlite3.Row | None:
    """Return the first active exclusive coordinate claim on the item, if any."""
    return conn.execute(
        """
        SELECT * FROM claim
        WHERE work_item_id = ? AND exclusive = 1 AND claim_type = 'coordinate'
          AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (work_item_id,),
    ).fetchone()
