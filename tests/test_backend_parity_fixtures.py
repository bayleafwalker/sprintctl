"""Parity fixtures: same canonical data across local, remote, and served backends.

Every field in the canonical data participates in assertions. No parity
assertion uses an empty array or merely checks that the output is a list.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sprintctl import db
import sprintctl.cli as cli_module
from sprintctl.cli import cli

_requires_312 = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="served mode requires Python 3.12+",
)

# ---------------------------------------------------------------------------
# Canonical fixture data — one shared contract across every backend
# ---------------------------------------------------------------------------

TRACK_NAME = "build"

CANONICAL_SPRINTS = [
    {
        "id": 1,
        "name": "S1",
        "goal": "Goal 1",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "status": "active",
        "kind": "active_sprint",
        "created_at": "2026-03-01T00:00:00Z",
        "aggregate_uuid": "11111111-1111-4111-8111-111111111111",
    },
    {
        "id": 2,
        "name": "S2",
        "goal": "Goal 2",
        "start_date": None,
        "end_date": None,
        "status": "planned",
        "kind": "backlog",
        "created_at": "2026-03-02T00:00:00Z",
        "aggregate_uuid": "22222222-2222-4222-8222-222222222222",
    },
]

CANONICAL_ITEMS = [
    {
        "id": 1,
        "sprint_id": 1,
        "track_id": 1,
        "title": "Item 1",
        "description": "",
        "status": "active",
        "assignee": None,
        "priority": None,
        "created_at": "2026-03-01T00:02:00Z",
        "updated_at": "2026-03-01T00:02:00Z",
        "aggregate_uuid": "33333333-3333-4333-8333-333333333333",
        "track_name": TRACK_NAME,
    },
    {
        "id": 2,
        "sprint_id": 1,
        "track_id": 1,
        "title": "Item 2",
        "description": "",
        "status": "active",
        "assignee": "alice",
        "priority": 1,
        "created_at": "2026-03-01T00:01:00Z",
        "updated_at": "2026-03-01T00:01:00Z",
        "aggregate_uuid": "44444444-4444-4444-8444-444444444444",
        "track_name": TRACK_NAME,
    },
    {
        "id": 3,
        "sprint_id": 2,
        "track_id": 2,
        "title": "Wrong sprint",
        "description": "",
        "status": "active",
        "assignee": None,
        "priority": None,
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-01T00:00:00Z",
        "aggregate_uuid": "55555555-5555-4555-8555-555555555555",
        "track_name": TRACK_NAME,
    },
    {
        "id": 4,
        "sprint_id": 1,
        "track_id": 3,
        "title": "Wrong track",
        "description": "",
        "status": "active",
        "assignee": None,
        "priority": None,
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-01T00:00:00Z",
        "aggregate_uuid": "66666666-6666-4666-8666-666666666666",
        "track_name": "other",
    },
    {
        "id": 5,
        "sprint_id": 1,
        "track_id": 1,
        "title": "Wrong status",
        "description": "",
        "status": "pending",
        "assignee": None,
        "priority": None,
        "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-01T00:00:00Z",
        "aggregate_uuid": "77777777-7777-4777-8777-777777777777",
        "track_name": TRACK_NAME,
    },
]

# ---------------------------------------------------------------------------
# Expected backend-independent output shapes
# ---------------------------------------------------------------------------

EXPECTED_SPRINT_LIST = [CANONICAL_SPRINTS[0]]  # only active_sprint kind by default

EXPECTED_ITEM_LIST = [CANONICAL_ITEMS[1], CANONICAL_ITEMS[0]]


def _normalize(items: list[dict]) -> list[dict]:
    """Strip backend-specific fields for cross-backend comparison."""
    if not items:
        return items
    strip = {"repo_id"}
    return [{k: v for k, v in item.items() if k not in strip} for item in items]


# ---------------------------------------------------------------------------
# Local SQLite setup
# ---------------------------------------------------------------------------


def _populate_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    db.init_db(conn)

    for sprint in CANONICAL_SPRINTS:
        conn.execute(
            "INSERT INTO sprint (id, name, goal, start_date, end_date, status, kind, created_at, aggregate_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sprint["id"], sprint["name"], sprint["goal"],
             sprint["start_date"], sprint["end_date"],
             sprint["status"], sprint["kind"],
             sprint["created_at"], sprint["aggregate_uuid"]),
        )

    conn.executemany(
        "INSERT INTO track (id, sprint_id, name) VALUES (?, ?, ?)",
        [(1, 1, TRACK_NAME), (2, 2, TRACK_NAME), (3, 1, "other")],
    )

    for item in CANONICAL_ITEMS:
        conn.execute(
            "INSERT INTO work_item (id, sprint_id, track_id, title, description, status, "
            "assignee, priority, created_at, updated_at, aggregate_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item["sprint_id"], item["track_id"],
             item["title"], item["description"], item["status"],
             item["assignee"], item["priority"],
             item["created_at"], item["updated_at"], item["aggregate_uuid"]),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Remote PG mock setup
# ---------------------------------------------------------------------------


def _mock_remote_pg(monkeypatch, repo_id: str) -> None:
    """Run the real PG read functions over a deterministic cursor fixture."""
    from sprintctl import pg

    class Cursor:
        def __init__(self):
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            if "FROM sprint " in query:
                self.rows = sorted(
                    (dict(row) for row in reversed(CANONICAL_SPRINTS)),
                    key=lambda row: row["created_at"],
                    reverse=True,
                )
                return
            assert "FROM work_item" in query
            sprint_id, track_name, status = params[1:]
            self.rows = sorted(
                (
                    dict(row)
                    for row in reversed(CANONICAL_ITEMS)
                    if row["sprint_id"] == sprint_id
                    and row["track_name"] == track_name
                    and row["status"] == status
                ),
                key=lambda row: row["created_at"],
            )

        def fetchall(self):
            return self.rows

    connection = MagicMock()
    connection.cursor.side_effect = Cursor
    store = pg.PgStore(conn=connection, repo_id=repo_id)
    monkeypatch.setattr(pg, "get_connection", lambda url: store)
    monkeypatch.setattr(pg, "superseded_marker_message", lambda s: None)

    import sprintctl.pg_migrations as pgm

    monkeypatch.setattr(pgm, "startup_schema_handshake", lambda s, e: {})

# ---------------------------------------------------------------------------
# Served mode setup
# ---------------------------------------------------------------------------


def _configure_served(tmp_path, monkeypatch) -> None:
    """Create served-mode marker, profile, and identity stub."""
    (tmp_path / ".git").mkdir()
    marker_dir = tmp_path / ".sprintctl"
    marker_dir.mkdir()
    (marker_dir / "backend.json").write_text(
        json.dumps({"backend": "served", "repo_id": tmp_path.name}),
        encoding="utf-8",
    )
    (tmp_path / "sprintctl.dispatch.json").write_text(
        json.dumps({"schema_version": 1, "repo_id": "test-repo-uuid"}),
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({
            "schema_version": "vuoro-client-profile/v1",
            "id": "test-profile",
            "target": {
                "environment_id": "test",
                "environment_class": "development",
                "endpoint": "https://vuoro.example.test/",
            },
            "credential_ref": "file:~/.config/vuoro/test",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile_path))
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)
    monkeypatch.setattr(
        cli_module._served,
        "identity_current",
        lambda profile, *, repo_id=None: {"repo_id": repo_id, "actor": "parity-actor"},
    )


# ---------------------------------------------------------------------------
# Sprint list parity
# ---------------------------------------------------------------------------


class TestSprintListParity:
    """sprint list --json produces identical output across every backend."""

    @pytest.mark.parametrize(
        "backend,setup_fn_group",
        [
            pytest.param("local", "local", id="local"),
            pytest.param("remote", "remote", id="remote"),
            pytest.param("served", "served", id="served", marks=_requires_312),
        ],
    )
    def test_sprint_list_json(self, runner, tmp_path, monkeypatch, backend, setup_fn_group):
        if setup_fn_group == "local":
            monkeypatch.setenv("SPRINTCTL_BACKEND", "local")
            _populate_sqlite(tmp_path / "test.db")
        elif setup_fn_group == "remote":
            marker_dir = tmp_path / ".sprintctl"
            marker_dir.mkdir()
            (marker_dir / "backend.json").write_text(
                json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
                encoding="utf-8",
            )
            monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
            monkeypatch.setenv("SPRINTCTL_URL", "postgresql://test@localhost/test")
            _mock_remote_pg(monkeypatch, tmp_path.name)
        elif setup_fn_group == "served":
            _configure_served(tmp_path, monkeypatch)
            monkeypatch.setattr(
                cli_module._served,
                "read_sprints",
                lambda profile, *, repo_id=None, include_backlog=False,
                include_archive=False, active_only=False, **kw: {
                    "sprints": [
                        dict(x)
                        for x in sorted(
                            reversed(CANONICAL_SPRINTS),
                            key=lambda row: row["created_at"],
                            reverse=True,
                        )
                        if include_backlog or x["kind"] != "backlog"
                    ],
                },
            )

        result = runner.invoke(cli, ["sprint", "list", "--json"])
        assert result.exit_code == 0, f"[{backend}] {result.output}"
        normalized = _normalize(json.loads(result.output))
        assert normalized == EXPECTED_SPRINT_LIST, (
            f"[{backend}] {json.dumps(normalized, indent=2)}"
        )


# ---------------------------------------------------------------------------
# Item list parity
# ---------------------------------------------------------------------------


class TestItemListParity:
    """item list --json produces identical output across every backend."""

    @pytest.mark.parametrize(
        "backend,setup_fn_group",
        [
            pytest.param("local", "local", id="local"),
            pytest.param("remote", "remote", id="remote"),
            pytest.param("served", "served", id="served", marks=_requires_312),
        ],
    )
    def test_item_list_json(self, runner, tmp_path, monkeypatch, backend, setup_fn_group):
        if setup_fn_group == "local":
            monkeypatch.setenv("SPRINTCTL_BACKEND", "local")
            _populate_sqlite(tmp_path / "test.db")
        elif setup_fn_group == "remote":
            marker_dir = tmp_path / ".sprintctl"
            marker_dir.mkdir()
            (marker_dir / "backend.json").write_text(
                json.dumps({"backend": "remote", "repo_id": tmp_path.name}),
                encoding="utf-8",
            )
            monkeypatch.setenv("SPRINTCTL_BACKEND", "remote")
            monkeypatch.setenv("SPRINTCTL_URL", "postgresql://test@localhost/test")
            _mock_remote_pg(monkeypatch, tmp_path.name)
        elif setup_fn_group == "served":
            _configure_served(tmp_path, monkeypatch)
            monkeypatch.setattr(
                cli_module._served,
                "read_items",
                lambda profile, *, repo_id=None, sprint_id=None,
                track_name=None, status=None, **kw: {
                    "items": [
                        dict(x) for x in CANONICAL_ITEMS
                        if (sprint_id is None or x["sprint_id"] == sprint_id)
                        and (track_name is None or x["track_name"] == track_name)
                        and (status is None or x["status"] == status)
                    ][::-1],
                },
            )

        result = runner.invoke(
            cli,
            [
                "item", "list", "--json",
                "--sprint-id", "1",
                "--track", TRACK_NAME,
                "--status", "active",
            ],
        )
        assert result.exit_code == 0, f"[{backend}] {result.output}"
        normalized = _normalize(json.loads(result.output))
        assert normalized == EXPECTED_ITEM_LIST, (
            f"[{backend}] {json.dumps(normalized, indent=2)}"
        )


# ---------------------------------------------------------------------------
# Served unavailable commands
# ---------------------------------------------------------------------------


class TestServedUnavailableParity:
    """Unavailable served commands fail before constructing any store."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["sprint", "kind", "--id", "1", "--kind", "backlog"],
            ["db", "vacuum"],
            ["maintain", "check"],
            ["render"],
        ],
    )
    @_requires_312
    def test_served_unavailable_fails_closed(self, runner, tmp_path, monkeypatch, argv):
        _configure_served(tmp_path, monkeypatch)
        monkeypatch.setattr(
            cli_module, "_get_store",
            lambda obj: pytest.fail("served unavailable command opened a store"),
        )

        result = runner.invoke(cli, argv)

        assert result.exit_code == 1, result.output
        assert "served-operation-unavailable" in result.output
        assert "PostgreSQL" not in result.output
