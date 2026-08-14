import io
import json
import os
import sqlite3
import threading
import time

import pytest

from sprintctl import authority, contracts, db
import sprintctl.cli as cli_module
from sprintctl.cli import cli
from sprintctl.render import render_sprint_doc
from tests.conftest import seed_legacy_claim


def _seed_version_5_schema_with_claim_identity_columns(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (5);

        CREATE TABLE sprint (
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

        CREATE TABLE track (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sprint_id   INTEGER NOT NULL REFERENCES sprint(id) ON DELETE CASCADE,
            name        TEXT    NOT NULL,
            description TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE (sprint_id, name)
        );

        CREATE TABLE work_item (
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

        CREATE TABLE event (
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

        CREATE TABLE claim (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id       INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
            agent              TEXT    NOT NULL,
            claim_type         TEXT    NOT NULL DEFAULT 'execute'
                                       CHECK (claim_type IN ('inspect', 'execute', 'review', 'coordinate')),
            exclusive          INTEGER NOT NULL DEFAULT 1,
            created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            expires_at         TEXT    NOT NULL,
            heartbeat          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            branch             TEXT,
            worktree_path      TEXT,
            commit_sha         TEXT,
            pr_ref             TEXT,
            claim_token        TEXT,
            runtime_session_id TEXT,
            instance_id        TEXT,
            hostname           TEXT,
            pid                INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Group 1: Sprint round-trip
# ---------------------------------------------------------------------------

class TestSprintRoundTrip:
    def test_sprint_create_and_show_by_id(self, runner, db_path):
        result = runner.invoke(
            cli,
            ["sprint", "create", "--name", "Alpha", "--goal", "Ship it",
             "--start", "2026-04-01", "--end", "2026-04-30"],
        )
        assert result.exit_code == 0, result.output
        # Extract id from "Created sprint #N: Alpha"
        sid = int(result.output.split("#")[1].split(":")[0])

        result = runner.invoke(cli, ["sprint", "show", "--id", str(sid)])
        assert result.exit_code == 0, result.output
        assert "Alpha" in result.output
        assert "Ship it" in result.output
        assert "2026-04-01" in result.output
        assert "2026-04-30" in result.output

    def test_sprint_show_active_fallback(self, runner, conn):
        db.create_sprint(conn, "ActiveOne", "Active goal", "2026-04-01", "2026-04-30", "active")
        result = runner.invoke(cli, ["sprint", "show"])
        assert result.exit_code == 0, result.output
        assert "ActiveOne" in result.output

    def test_sprint_list_multiple(self, runner, db_path):
        for name in ["Alpha", "Beta", "Gamma"]:
            runner.invoke(
                cli,
                ["sprint", "create", "--name", name, "--goal", "", "--start", "2026-04-01", "--end", "2026-04-30"],
            )
        result = runner.invoke(cli, ["sprint", "list"])
        assert result.exit_code == 0, result.output
        assert "Alpha" in result.output
        assert "Beta" in result.output
        assert "Gamma" in result.output

    def test_sprint_create_missing_required_args(self, runner, db_path):
        # Only --name is required; --start and --end are optional
        result = runner.invoke(cli, ["sprint", "create"])
        assert result.exit_code == 2

    def test_sprint_create_without_dates(self, runner, db_path):
        # Sprint can be created as a generic execution container without dates
        result = runner.invoke(
            cli,
            ["sprint", "create", "--name", "Dateless", "--status", "active"],
        )
        assert result.exit_code == 0, result.output
        assert "Dateless" in result.output

    def test_sprint_create_json_output(self, runner, conn, db_path):
        result = runner.invoke(
            cli,
            [
                "sprint",
                "create",
                "--name",
                "Json Sprint",
                "--goal",
                "Test json create",
                "--status",
                "active",
                "--kind",
                "backlog",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["name"] == "Json Sprint"
        assert data["goal"] == "Test json create"
        assert data["status"] == "active"
        assert data["kind"] == "backlog"
        assert isinstance(data["id"], int)

    def test_sprint_show_dateless_omits_dates_line(self, runner, conn):
        from sprintctl import db
        sid = db.create_sprint(conn, "Dateless Show", status="active")
        result = runner.invoke(cli, ["sprint", "show", "--id", str(sid)])
        assert result.exit_code == 0, result.output
        assert "Dates:" not in result.output
        assert "Dateless Show" in result.output

    def test_sprint_show_no_active_sprint(self, runner, db_path):
        result = runner.invoke(cli, ["sprint", "show"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 2: Item CRUD
# ---------------------------------------------------------------------------

class TestItemCRUD:
    def test_item_add_creates_track_on_first_use(self, runner, conn, active_sprint):
        result = runner.invoke(
            cli,
            ["item", "add", "--sprint-id", str(active_sprint["id"]),
             "--track", "backend", "--title", "Write API"],
        )
        assert result.exit_code == 0, result.output
        tracks = db.list_tracks(conn, active_sprint["id"])
        assert any(t["name"] == "backend" for t in tracks)

    def test_item_add_reuses_existing_track(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "backend", "--title", "Item 1"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "backend", "--title", "Item 2"])
        tracks = db.list_tracks(conn, active_sprint["id"])
        backend_tracks = [t for t in tracks if t["name"] == "backend"]
        assert len(backend_tracks) == 1

    def test_item_list_all(self, runner, active_sprint):
        sid = str(active_sprint["id"])
        for title, track in [("API work", "backend"), ("UI work", "frontend"), ("Docs", "frontend")]:
            runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", track, "--title", title])
        result = runner.invoke(cli, ["item", "list", "--sprint-id", sid])
        assert result.exit_code == 0, result.output
        assert "API work" in result.output
        assert "UI work" in result.output
        assert "Docs" in result.output

    def test_item_list_accepts_scoped_sprint_reference(self, runner, active_sprint, tmp_path):
        sid = active_sprint["id"]
        runner.invoke(
            cli,
            ["item", "add", "--sprint-id", str(sid), "--track", "backend", "--title", "API work"],
        )

        result = runner.invoke(
            cli, ["item", "list", "--sprint-id", f"{tmp_path.name}#{sid}"]
        )

        assert result.exit_code == 0, result.output
        assert "API work" in result.output

    def test_item_list_filter_by_track(self, runner, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "backend", "--title", "BE item"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "frontend", "--title", "FE item"])
        result = runner.invoke(cli, ["item", "list", "--sprint-id", sid, "--track", "backend"])
        assert result.exit_code == 0, result.output
        assert "BE item" in result.output
        assert "FE item" not in result.output

    def test_item_list_filter_by_status(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "t", "--title", "Pending 1"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "t", "--title", "Pending 2"])
        # Create an active item directly in DB
        tid = db.get_or_create_track(conn, active_sprint["id"], "t")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Active 1")
        db.set_work_item_status(conn, iid, "active")

        result = runner.invoke(cli, ["item", "list", "--sprint-id", sid, "--status", "active"])
        assert result.exit_code == 0, result.output
        assert "Active 1" in result.output
        assert "Pending 1" not in result.output
        assert "Pending 2" not in result.output

    def test_item_add_invalid_sprint(self, runner, db_path):
        result = runner.invoke(
            cli, ["item", "add", "--sprint-id", "9999", "--track", "t", "--title", "Orphan"]
        )
        assert result.exit_code == 1

    def test_item_add_json_output(self, runner, conn, active_sprint):
        result = runner.invoke(
            cli,
            [
                "item",
                "add",
                "--sprint-id",
                str(active_sprint["id"]),
                "--track",
                "backend",
                "--title",
                "JSON item",
                "--assignee",
                "alice",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["sprint_id"] == active_sprint["id"]
        assert data["track_name"] == "backend"
        assert data["title"] == "JSON item"
        assert data["description"] == ""
        assert data["assignee"] == "alice"
        assert data["status"] == "pending"
        assert isinstance(data["id"], int)

    def test_item_add_persists_description(self, runner, conn, active_sprint):
        result = runner.invoke(
            cli,
            [
                "item",
                "add",
                "--sprint-id",
                str(active_sprint["id"]),
                "--track",
                "backend",
                "--title",
                "Described item",
                "--description",
                "Implement the API and its contract tests.",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["description"] == "Implement the API and its contract tests."
        assert db.get_work_item(conn, data["id"])["description"] == data["description"]

    @pytest.mark.parametrize("description", ["", "  \n\t  "])
    def test_item_add_rejects_empty_explicit_description(
        self, runner, conn, active_sprint, description
    ):
        result = runner.invoke(
            cli,
            [
                "item",
                "add",
                "--sprint-id",
                str(active_sprint["id"]),
                "--track",
                "backend",
                "--title",
                "Invalid description",
                "--description",
                description,
            ],
        )
        assert result.exit_code == 2
        assert "must contain non-whitespace text" in result.output
        assert not db.list_work_items(conn, sprint_id=active_sprint["id"])

    def test_item_edit_replaces_description_and_returns_item_json(
        self, runner, conn, active_sprint
    ):
        track_id = db.get_or_create_track(conn, active_sprint["id"], "backend")
        item_id = db.create_work_item(
            conn,
            active_sprint["id"],
            track_id,
            "Editable item",
            "Old scope",
        )

        result = runner.invoke(
            cli,
            [
                "item",
                "edit",
                "--id",
                str(item_id),
                "--description",
                "New shaped scope",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["id"] == item_id
        assert data["description"] == "New shaped scope"
        assert data["edit_revision"].startswith(
            f"item:{data['aggregate_uuid']}@description:v1@sha256:"
        )
        assert db.get_work_item(conn, item_id)["description"] == "New shaped scope"

        events = db.list_events(conn, active_sprint["id"])
        edit_events = [e for e in events if e["event_type"] == "item-edited"]
        assert len(edit_events) == 1
        payload = json.loads(edit_events[0]["payload"])
        assert payload["previous_description"] == "Old scope"
        assert payload["description"] == "New shaped scope"
        assert payload["previous_revision"] != payload["revision"]
        assert set(payload) == {
            "summary",
            "field",
            "previous_description",
            "description",
            "previous_revision",
            "revision",
        }
        assert edit_events[0]["work_item_id"] == item_id

    def test_item_edit_rejects_empty_description_without_mutation(
        self, runner, conn, active_sprint
    ):
        track_id = db.get_or_create_track(conn, active_sprint["id"], "backend")
        item_id = db.create_work_item(
            conn,
            active_sprint["id"],
            track_id,
            "Editable item",
            "Keep this scope",
        )

        result = runner.invoke(
            cli,
            ["item", "edit", "--id", str(item_id), "--description", "   "],
        )

        assert result.exit_code == 2
        assert "must contain non-whitespace text" in result.output
        assert db.get_work_item(conn, item_id)["description"] == "Keep this scope"

    def test_item_edit_unknown_item_fails(self, runner, db_path):
        result = runner.invoke(
            cli,
            ["item", "edit", "--id", "9999", "--description", "Valid scope"],
        )
        assert result.exit_code == 1
        assert "Item #9999 not found" in result.output

    def test_update_work_item_description_validates_and_updates(
        self, conn, active_sprint
    ):
        track_id = db.get_or_create_track(conn, active_sprint["id"], "backend")
        item_id = db.create_work_item(conn, active_sprint["id"], track_id, "Direct API")

        db.update_work_item_description(conn, item_id, "Directly updated scope")

        assert db.get_work_item(conn, item_id)["description"] == "Directly updated scope"
        with pytest.raises(ValueError, match="non-whitespace"):
            db.update_work_item_description(conn, item_id, "\n\t")
        with pytest.raises(ValueError, match="NUL"):
            db.update_work_item_description(conn, item_id, "invalid\x00scope")
        with pytest.raises(ValueError, match="Item #9999 not found"):
            db.update_work_item_description(conn, 9999, "Valid scope")

    def test_two_readers_cannot_apply_the_same_edit_revision(
        self, conn, db_path, active_sprint
    ):
        track_id = db.get_or_create_track(conn, active_sprint["id"], "backend")
        item_id = db.create_work_item(
            conn, active_sprint["id"], track_id, "CAS item", "Original scope"
        )
        other = db.get_connection(db_path)
        try:
            first = db.get_work_item_with_edit_revision(conn, item_id)
            second = db.get_work_item_with_edit_revision(other, item_id)
            assert first is not None and second is not None
            assert first[1] == second[1]

            db.update_work_item_description(
                conn,
                item_id,
                "First correction",
                expected_revision=first[1],
                actor="first",
            )
            with pytest.raises(db.EditConflict, match="revision mismatch"):
                db.update_work_item_description(
                    other,
                    item_id,
                    "Stale correction",
                    expected_revision=second[1],
                    actor="second",
                )
        finally:
            other.close()

        assert db.get_work_item(conn, item_id)["description"] == "First correction"
        edits = [
            event
            for event in db.list_events(conn, active_sprint["id"])
            if event["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
        ]
        assert len(edits) == 1

    def test_overlapping_edit_writers_accept_exactly_one_revision(
        self, conn, db_path, active_sprint
    ):
        """Two real concurrent connections race the same CAS edit.

        Mirrors tests/pg/test_work_item.py's
        test_overlapping_edit_writers_accept_exactly_one_revision, proving
        SQLite's ``BEGIN IMMEDIATE`` (workitemcore.py's ``begin_txn``) still
        serializes concurrent writers on the same item after the P2.1
        transaction-scaffold extraction, the same way PostgreSQL's
        ``SELECT ... FOR UPDATE`` does. Unlike
        test_two_readers_cannot_apply_the_same_edit_revision above (which
        calls the two writes sequentially from one thread), this uses a
        real threading.Barrier so both writers attempt BEGIN IMMEDIATE at
        the same instant — the PG suite had this coverage already; the
        SQLite suite did not.
        """
        track_id = db.get_or_create_track(conn, active_sprint["id"], "backend")
        item_id = db.create_work_item(
            conn, active_sprint["id"], track_id, "Concurrent edit", "Original scope"
        )
        current = db.get_work_item_with_edit_revision(conn, item_id)
        assert current is not None
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []

        def edit(label: str) -> None:
            independent = db.get_connection(db_path)
            try:
                barrier.wait(timeout=10)
                try:
                    db.update_work_item_description(
                        independent,
                        item_id,
                        f"{label} scope",
                        expected_revision=current[1],
                        actor=label,
                    )
                except db.EditConflict:
                    outcomes.append((label, "conflict"))
                else:
                    outcomes.append((label, "accepted"))
            finally:
                independent.close()

        threads = [
            threading.Thread(target=edit, args=("writer-a",)),
            threading.Thread(target=edit, args=("writer-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcome for _label, outcome in outcomes) == [
            "accepted",
            "conflict",
        ]

        item = db.get_work_item(conn, item_id)
        assert item is not None
        accepted_actor = next(
            label for label, outcome in outcomes if outcome == "accepted"
        )
        assert item["description"] == f"{accepted_actor} scope"
        edits = [
            event
            for event in db.list_events(conn, active_sprint["id"])
            if event["work_item_id"] == item_id
            and event["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
        ]
        assert len(edits) == 1
        assert edits[0]["actor"] == accepted_actor

    def test_item_edit_rolls_back_description_when_audit_insert_fails(
        self, conn, active_sprint, monkeypatch
    ):
        track_id = db.get_or_create_track(conn, active_sprint["id"], "backend")
        item_id = db.create_work_item(
            conn, active_sprint["id"], track_id, "Atomic edit", "Original scope"
        )
        current = db.get_work_item_with_edit_revision(conn, item_id)
        assert current is not None

        def fail_event_insert(*args, **kwargs):
            raise RuntimeError("injected audit insert failure")

        monkeypatch.setattr(db, "_insert_event", fail_event_insert)
        with pytest.raises(RuntimeError, match="injected audit insert failure"):
            db.update_work_item_description(
                conn,
                item_id,
                "Must roll back",
                expected_revision=current[1],
                actor="editor",
            )

        assert db.get_work_item(conn, item_id)["description"] == "Original scope"
        assert db.list_events(conn, active_sprint["id"]) == []


# ---------------------------------------------------------------------------
# Group 3: Status transitions
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    def _add_item(self, runner, sid):
        result = runner.invoke(
            cli, ["item", "add", "--sprint-id", str(sid), "--track", "t", "--title", "Item"]
        )
        assert result.exit_code == 0, result.output
        return int(result.output.split("#")[1].split(":")[0])

    def _status(self, runner, conn, item_id, status, *extra):
        item = db.get_work_item(conn, item_id)
        assert item is not None
        return runner.invoke(
            cli,
            [
                "item", "status", "--id", str(item_id), "--status", status,
                "--expected-revision", db.item_status_revision(item), *extra,
            ],
        )

    def test_valid_transition_pending_to_active(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = self._status(runner, conn, iid, "active")
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_valid_transition_active_to_done(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        self._status(runner, conn, iid, "active")
        result = self._status(runner, conn, iid, "done")
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "done"

    def test_valid_transition_active_to_blocked(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        self._status(runner, conn, iid, "active")
        result = self._status(runner, conn, iid, "blocked")
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "blocked"

    def test_invalid_transition_pending_to_done(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = self._status(runner, conn, iid, "done")
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_invalid_transition_pending_to_blocked(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = self._status(runner, conn, iid, "blocked")
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_invalid_transition_done_is_terminal(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        self._status(runner, conn, iid, "active")
        self._status(runner, conn, iid, "done")
        result = self._status(runner, conn, iid, "active")
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_blocked_can_revive_to_active(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        self._status(runner, conn, iid, "active")
        self._status(runner, conn, iid, "blocked")
        result = self._status(runner, conn, iid, "active")
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_invalid_transition_blocked_to_done(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        self._status(runner, conn, iid, "active")
        self._status(runner, conn, iid, "blocked")
        result = self._status(runner, conn, iid, "done")
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_item_status_unknown_item_id(self, runner, db_path):
        result = runner.invoke(
            cli,
            [
                "item", "status", "--id", "9999", "--status", "active",
                "--expected-revision", "item:00000000-0000-0000-0000-000000000000@status:pending",
            ],
        )
        assert result.exit_code == 1

    def test_item_status_json_output(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = self._status(runner, conn, iid, "active", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"item_id": iid, "previous": "pending", "status": "active"}

    def test_item_status_requires_expected_revision(self, runner, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = runner.invoke(
            cli, ["item", "status", "--id", str(iid), "--status", "active"]
        )
        assert result.exit_code == 2
        assert "Missing option '--expected-revision'" in result.output

    def test_item_status_rejects_stale_revision_without_row_or_event_effect(
        self, runner, conn, active_sprint
    ):
        iid = self._add_item(runner, active_sprint["id"])
        item = db.get_work_item(conn, iid)
        assert item is not None
        basis = db.item_status_revision(item)
        assert basis == authority.item_revision(item)

        accepted = runner.invoke(
            cli,
            [
                "item", "status", "--id", str(iid), "--status", "active",
                "--expected-revision", basis,
            ],
        )
        assert accepted.exit_code == 0, accepted.output
        events_after_accept = db.list_events(conn, active_sprint["id"])

        stale = runner.invoke(
            cli,
            [
                "item", "status", "--id", str(iid), "--status", "done",
                "--expected-revision", basis,
            ],
        )
        assert stale.exit_code == 1
        assert "status revision mismatch" in stale.output
        assert db.get_work_item(conn, iid)["status"] == "active"
        assert db.list_events(conn, active_sprint["id"]) == events_after_accept

    def test_item_show_exposes_status_revision(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        item = json.loads(result.output)["item"]
        assert item["status_revision"] == db.item_status_revision(item)


# ---------------------------------------------------------------------------
# Group 4: Event logging
# ---------------------------------------------------------------------------

class TestEventLogging:
    def test_event_add_basic(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        result = runner.invoke(
            cli, ["event", "add", "--sprint-id", sid, "--type", "note", "--actor", "agent-1"]
        )
        assert result.exit_code == 0, result.output
        events = db.list_events(conn, active_sprint["id"])
        assert len(events) == 1
        assert events[0]["actor"] == "agent-1"
        assert events[0]["event_type"] == "note"
        assert events[0]["sprint_id"] == active_sprint["id"]

    def test_event_add_with_payload(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        payload = '{"key": "val"}'
        runner.invoke(
            cli,
            ["event", "add", "--sprint-id", sid, "--type", "note",
             "--actor", "agent-1", "--payload", payload],
        )
        events = db.list_events(conn, active_sprint["id"])
        assert json.loads(events[0]["payload"]) == {"key": "val"}

    def test_event_add_with_item_id(self, runner, conn, active_sprint):
        sid = active_sprint["id"]
        tid = db.get_or_create_track(conn, sid, "backend")
        iid = db.create_work_item(conn, sid, tid, "Some work")
        result = runner.invoke(
            cli,
            ["event", "add", "--sprint-id", str(sid), "--type", "progress",
             "--actor", "agent-1", "--item-id", str(iid)],
        )
        assert result.exit_code == 0, result.output
        events = db.list_events(conn, sid)
        assert events[0]["work_item_id"] == iid

    def test_event_add_invalid_item_id(self, runner, active_sprint):
        sid = str(active_sprint["id"])
        result = runner.invoke(
            cli,
            ["event", "add", "--sprint-id", sid, "--type", "note",
             "--actor", "agent-1", "--item-id", "9999"],
        )
        assert result.exit_code == 1

    def test_event_list_accepts_scoped_sprint_and_item_references(
        self, runner, conn, active_sprint, tmp_path
    ):
        sid = active_sprint["id"]
        tid = db.get_or_create_track(conn, sid, "backend")
        iid = db.create_work_item(conn, sid, tid, "Some work")
        db.create_event(conn, sid, "agent-1", "progress", work_item_id=iid)

        result = runner.invoke(
            cli,
            [
                "event", "list", "--sprint-id", f"{tmp_path.name}#{sid}",
                "--item-id", f"{tmp_path.name}#{iid}", "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert [event["work_item_id"] for event in json.loads(result.output)] == [iid]

    def test_context_candidates_accepts_scoped_sprint_and_item_references(
        self, runner, conn, active_sprint, tmp_path
    ):
        sid = active_sprint["id"]
        tid = db.get_or_create_track(conn, sid, "backend")
        iid = db.create_work_item(conn, sid, tid, "Scoped candidate")

        result = runner.invoke(
            cli,
            [
                "context-candidates", "--sprint-id", f"{tmp_path.name}#{sid}",
                "--item-id", f"{tmp_path.name}#{iid}", "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["explicit_target"]["item_id"] == iid


# ---------------------------------------------------------------------------
# Group 5: Render
# ---------------------------------------------------------------------------

class TestRender:
    def test_render_contains_sprint_header(self, runner, active_sprint):
        result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "S1" in result.output
        assert "Ship Phase 1" in result.output

    def test_render_contains_track_section(self, runner, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "backend", "--title", "API"])
        result = runner.invoke(cli, ["render", "--sprint-id", sid])
        assert result.exit_code == 0, result.output
        assert "Track: backend" in result.output

    def test_render_items_under_correct_track(self, runner, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "backend", "--title", "BE work"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "frontend", "--title", "FE work"])
        result = runner.invoke(cli, ["render", "--sprint-id", sid])
        assert result.exit_code == 0, result.output
        output = result.output
        be_pos = output.index("Track: backend")
        fe_pos = output.index("Track: frontend")
        be_item_pos = output.index("BE work")
        fe_item_pos = output.index("FE work")
        assert be_pos < be_item_pos < fe_pos
        assert fe_pos < fe_item_pos

    def test_render_contains_timestamp(self, runner, active_sprint):
        result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Rendered:" in result.output

    def test_render_idempotent(self, conn, active_sprint):
        sprint = active_sprint
        tracks = db.list_tracks(conn, sprint["id"])
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        items_by_track: dict[int, list[dict]] = {}
        for it in items:
            items_by_track.setdefault(it["track_id"], []).append(it)
        ts = "2026-03-26T10:00:00Z"
        doc1 = render_sprint_doc(sprint, tracks, items_by_track, ts)
        doc2 = render_sprint_doc(sprint, tracks, items_by_track, ts)
        assert doc1 == doc2

    def test_render_no_active_sprint_exits(self, runner, db_path):
        result = runner.invoke(cli, ["render"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 6: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_init_db_idempotent(self, conn):
        db.init_db(conn)  # second call
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 19

    @pytest.mark.parametrize("_history", range(32))
    def test_init_db_handles_concurrent_version_lag_after_upgrade(
        self, tmp_path, _history
    ):
        db_path = tmp_path / "lagged.db"
        _seed_version_5_schema_with_claim_identity_columns(db_path)

        errors = []
        barrier = threading.Barrier(8)

        def worker():
            conn = None
            try:
                barrier.wait(timeout=5)
                conn = db.get_connection(db_path)
                db.init_db(conn)
            except Exception as exc:  # pragma: no cover - exercised on failure
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 30
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

        assert all(not thread.is_alive() for thread in threads)
        assert not errors, [repr(exc) for exc in errors]

        conn = db.get_connection(db_path)
        try:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND sql IS NOT NULL"
                )
            }
            foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()

        assert version == 19
        assert tables == {
            "claim",
            "claim_history",
            "dep",
            "event",
            "maintenance_capability",
            "maintenance_capability_receipt",
            "maintenance_capability_recovery",
            "maintenance_resource",
            "maintenance_resource_event",
            "ref",
            "reservation",
            "schema_version",
            "sprint",
            "track",
            "work_item",
        }
        assert indexes == {
            "idx_claim_history_claim_id",
            "idx_claim_token",
            "idx_event_sprint_type_ts",
            "idx_reservation_active_execute",
            "idx_reservation_item_state",
            "idx_sprint_aggregate_uuid",
            "idx_work_item_aggregate_uuid",
        }
        assert foreign_keys == 1
        assert journal_mode == "wal"

    def test_claim_archive_retries_only_missing_historic_rows(self, conn, active_sprint):
        track_id = db.get_or_create_track(conn, active_sprint["id"], "archive")
        first_item = db.create_work_item(conn, active_sprint["id"], track_id, "First")
        second_item = db.create_work_item(conn, active_sprint["id"], track_id, "Second")
        first_claim = seed_legacy_claim(conn, first_item, "first")
        second_claim = seed_legacy_claim(conn, second_item, "second")
        conn.execute("INSERT INTO claim_history SELECT * FROM claim WHERE id = ?", (first_claim,))
        conn.commit()

        db._migration_19(conn)
        db._migration_19(conn)

        archived = conn.execute(
            "SELECT id FROM claim_history WHERE id IN (?, ?) ORDER BY id",
            (first_claim, second_claim),
        ).fetchall()
        assert [row["id"] for row in archived] == [first_claim, second_claim]

    class _StubConnection:
        def __init__(
            self,
            wal_error: Exception | None = None,
            foreign_key_error: Exception | None = None,
        ) -> None:
            self.row_factory = None
            self.wal_error = wal_error
            self.foreign_key_error = foreign_key_error
            self.closed = False
            self.statements: list[str] = []

        def execute(self, statement: str):
            self.statements.append(statement)
            if (
                statement == "PRAGMA foreign_keys = ON"
                and self.foreign_key_error is not None
            ):
                raise self.foreign_key_error
            if statement == "PRAGMA journal_mode = WAL" and self.wal_error is not None:
                raise self.wal_error
            return self

        def close(self) -> None:
            self.closed = True

    @staticmethod
    def _sqlite_error(error_code: int) -> sqlite3.OperationalError:
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = error_code
        return error

    @pytest.mark.parametrize(
        "error_code",
        [
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_BUSY_RECOVERY,
            sqlite3.SQLITE_BUSY_SNAPSHOT,
            sqlite3.SQLITE_BUSY_TIMEOUT,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_LOCKED_SHAREDCACHE,
        ],
    )
    def test_connection_retries_primary_and_extended_busy_or_locked_wal_errors(
        self, tmp_path, monkeypatch, error_code
    ):
        first = self._StubConnection(self._sqlite_error(error_code))
        second = self._StubConnection()
        connections = iter((first, second))
        delays = []
        monkeypatch.setattr(
            db.sqlite3, "connect", lambda *_args, **_kwargs: next(connections)
        )
        monkeypatch.setattr(db.time, "sleep", delays.append)

        connected = db.get_connection(tmp_path / "sprintctl.db")

        assert connected is second
        assert first.closed is True
        assert second.closed is False
        assert first.statements == [
            "PRAGMA foreign_keys = ON",
            "PRAGMA journal_mode = WAL",
        ]
        assert second.statements == first.statements
        assert delays == [db._WAL_BUSY_RETRY_DELAYS_SECONDS[0]]

    def test_connection_stops_after_bounded_busy_wal_retries(
        self, tmp_path, monkeypatch
    ):
        failures = [
            self._sqlite_error(sqlite3.SQLITE_BUSY)
            for _ in range(len(db._WAL_BUSY_RETRY_DELAYS_SECONDS) + 1)
        ]
        connections = [self._StubConnection(failure) for failure in failures]
        pending_connections = iter(connections)
        delays = []
        monkeypatch.setattr(
            db.sqlite3,
            "connect",
            lambda *_args, **_kwargs: next(pending_connections),
        )
        monkeypatch.setattr(db.time, "sleep", delays.append)

        with pytest.raises(sqlite3.OperationalError) as caught:
            db.get_connection(tmp_path / "sprintctl.db")

        assert caught.value is failures[-1]
        assert all(connection.closed for connection in connections)
        assert delays == list(db._WAL_BUSY_RETRY_DELAYS_SECONDS)

    @pytest.mark.parametrize(
        "failure",
        [
            sqlite3.OperationalError("read-only database"),
            RuntimeError("unexpected WAL failure"),
        ],
    )
    def test_connection_propagates_non_busy_wal_error_without_retry(
        self, tmp_path, monkeypatch, failure
    ):
        if isinstance(failure, sqlite3.OperationalError):
            failure.sqlite_errorcode = sqlite3.SQLITE_READONLY
        failed = self._StubConnection(failure)
        connect_calls = 0

        def connect(*_args, **_kwargs):
            nonlocal connect_calls
            connect_calls += 1
            return failed

        monkeypatch.setattr(db.sqlite3, "connect", connect)

        with pytest.raises(type(failure)) as caught:
            db.get_connection(tmp_path / "sprintctl.db")

        assert caught.value is failure
        assert connect_calls == 1
        assert failed.closed is True

    def test_connection_closes_foreign_key_setup_failure_without_retry(
        self, tmp_path, monkeypatch
    ):
        failure = RuntimeError("foreign key setup failed")
        failed = self._StubConnection(foreign_key_error=failure)
        connect_calls = 0

        def connect(*_args, **_kwargs):
            nonlocal connect_calls
            connect_calls += 1
            return failed

        monkeypatch.setattr(db.sqlite3, "connect", connect)

        with pytest.raises(RuntimeError) as caught:
            db.get_connection(tmp_path / "sprintctl.db")

        assert caught.value is failure
        assert connect_calls == 1
        assert failed.closed is True
        assert failed.statements == ["PRAGMA foreign_keys = ON"]

    def test_db_path_from_env(self, tmp_path, monkeypatch):
        custom = str(tmp_path / "custom.db")
        monkeypatch.setenv("SPRINTCTL_DB", custom)
        assert str(db.get_db_path()) == custom

    def test_db_path_default(self, monkeypatch):
        monkeypatch.delenv("SPRINTCTL_DB", raising=False)
        path = db.get_db_path()
        assert path.name == "sprintctl.db"
        assert ".sprintctl" in str(path)


# ---------------------------------------------------------------------------
# Group 7: Sprint kind
# ---------------------------------------------------------------------------

class TestSprintKind:
    def test_sprint_default_kind_is_active_sprint(self, conn):
        sid = db.create_sprint(conn, "K1", "", "2026-04-01", "2026-04-30", "active")
        s = db.get_sprint(conn, sid)
        assert s["kind"] == "active_sprint"

    def test_sprint_create_with_backlog_kind(self, runner, db_path):
        result = runner.invoke(
            cli,
            ["sprint", "create", "--name", "Backlog", "--goal", "", "--start", "2026-04-01",
             "--end", "2026-04-30", "--kind", "backlog"],
        )
        assert result.exit_code == 0, result.output
        sid = int(result.output.split("#")[1].split(":")[0])
        assert db.get_sprint(db.get_connection(db_path), sid)["kind"] == "backlog"

    def test_sprint_kind_cmd_sets_kind(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "K2", "", "2026-04-01", "2026-04-30", "active")
        result = runner.invoke(cli, ["sprint", "kind", "--id", str(sid), "--kind", "archive"])
        assert result.exit_code == 0, result.output
        assert db.get_sprint(conn, sid)["kind"] == "archive"

    def test_sprint_kind_accepts_scoped_reference(self, runner, conn, db_path, tmp_path):
        sid = db.create_sprint(conn, "K3", "", "2026-04-01", "2026-04-30", "active")
        result = runner.invoke(
            cli,
            ["sprint", "kind", "--id", f"{tmp_path.name}#{sid}", "--kind", "archive"],
        )
        assert result.exit_code == 0, result.output
        assert db.get_sprint(conn, sid)["kind"] == "archive"

    def test_get_active_sprint_ignores_backlog(self, conn):
        db.create_sprint(conn, "Backlog S", "", "2026-04-01", "2026-04-30", "active", kind="backlog")
        s = db.get_active_sprint(conn)
        assert s is None

    def test_get_active_sprint_returns_active_sprint_kind(self, conn):
        db.create_sprint(conn, "Backlog S", "", "2026-04-01", "2026-04-30", "active", kind="backlog")
        sid = db.create_sprint(conn, "Active S", "", "2026-04-01", "2026-04-30", "active", kind="active_sprint")
        s = db.get_active_sprint(conn)
        assert s is not None
        assert s["id"] == sid

    def test_sprint_list_hides_backlog_by_default(self, runner, conn, db_path):
        db.create_sprint(conn, "BS", "", "2026-04-01", "2026-04-30", "active", kind="backlog")
        db.create_sprint(conn, "AS", "", "2026-04-01", "2026-04-30", "active", kind="active_sprint")
        result = runner.invoke(cli, ["sprint", "list"])
        assert result.exit_code == 0, result.output
        assert "AS" in result.output
        assert "BS" not in result.output

    def test_sprint_list_shows_backlog_with_flag(self, runner, conn, db_path):
        db.create_sprint(conn, "BS", "", "2026-04-01", "2026-04-30", "active", kind="backlog")
        result = runner.invoke(cli, ["sprint", "list", "--include-backlog"])
        assert result.exit_code == 0, result.output
        assert "BS" in result.output

    def test_sprint_show_includes_kind(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "ShowMe", "", "2026-04-01", "2026-04-30", "active", kind="backlog")
        result = runner.invoke(cli, ["sprint", "show", "--id", str(sid)])
        assert result.exit_code == 0, result.output
        assert "backlog" in result.output


# ---------------------------------------------------------------------------
# Group 8: blocked → active revival
# ---------------------------------------------------------------------------

class TestBlockedRevival:
    def _add_active_item(self, runner, conn, sprint_id):
        tid = db.get_or_create_track(conn, sprint_id, "t")
        iid = db.create_work_item(conn, sprint_id, tid, "Task")
        db.set_work_item_status(conn, iid, "active")
        return iid

    def test_blocked_to_active_allowed(self, conn, active_sprint):
        iid = self._add_active_item(None, conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "blocked")
        db.set_work_item_status(conn, iid, "active")
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_blocked_to_done_not_allowed(self, conn, active_sprint):
        import pytest
        iid = self._add_active_item(None, conn, active_sprint["id"])
        db.set_work_item_status(conn, iid, "blocked")
        with pytest.raises(db.InvalidTransition):
            db.set_work_item_status(conn, iid, "done")

    def test_active_legacy_claim_does_not_override_status_cas(self, conn, active_sprint):
        iid = self._add_active_item(None, conn, active_sprint["id"])
        seed_legacy_claim(conn, iid, "legacy-worker")
        basis = db.item_status_revision(db.get_work_item(conn, iid))
        db.set_work_item_status(conn, iid, "done", expected_revision=basis)
        assert db.get_work_item(conn, iid)["status"] == "done"

    def test_sweep_blocked_item_can_be_revived(self, conn, active_sprint):
        from datetime import datetime, timedelta, timezone
        from sprintctl import maintain as maint
        iid = self._add_active_item(None, conn, active_sprint["id"])
        maint.sweep(conn, active_sprint["id"], datetime.now(timezone.utc), threshold=timedelta(hours=0))
        assert db.get_work_item(conn, iid)["status"] == "blocked"
        # Can revive
        db.set_work_item_status(conn, iid, "active")
        assert db.get_work_item(conn, iid)["status"] == "active"


# ---------------------------------------------------------------------------
# Group 9: export / import
# ---------------------------------------------------------------------------

class TestExportImport:
    def _build_sprint(self, runner, conn, db_path):
        sid = db.create_sprint(conn, "Expo", "Export goal", "2026-04-01", "2026-04-30", "active")
        tid = db.get_or_create_track(conn, sid, "backend")
        iid = db.create_work_item(conn, sid, tid, "Do the thing", assignee="alice")
        db.set_work_item_status(conn, iid, "active")
        db.create_event(conn, sid, "alice", "progress", work_item_id=iid, payload={"note": "started"})
        return sid, iid

    def test_export_creates_file(self, runner, conn, db_path, tmp_path):
        sid, _ = self._build_sprint(runner, conn, db_path)
        out = str(tmp_path / "export.json")
        result = runner.invoke(cli, ["export", "--sprint-id", str(sid), "--output", out])
        assert result.exit_code == 0, result.output
        import os
        assert os.path.exists(out)

    def test_export_json_structure(self, runner, conn, db_path, tmp_path):
        sid, iid = self._build_sprint(runner, conn, db_path)
        out = str(tmp_path / "export.json")
        runner.invoke(cli, ["export", "--sprint-id", str(sid), "--output", out])
        with open(out) as f:
            data = json.load(f)
        assert "sprintctl_version" in data
        assert "exported_at" in data
        assert data["sprint"]["id"] == sid
        assert len(data["tracks"]) == 1
        assert len(data["items"]) == 1
        assert len(data["events"]) == 1

    def test_export_import_preserves_reservations_and_archived_claims(self, runner, conn, db_path, tmp_path):
        sid, iid = self._build_sprint(runner, conn, db_path)
        db.reserve(conn, iid, actor="alice", session_id="export-session")
        claim_id = seed_legacy_claim(conn, iid, "legacy-alice")
        db._migration_19(conn)
        conn.commit()
        out = str(tmp_path / "export.json")
        exported = runner.invoke(cli, ["export", "--sprint-id", str(sid), "--output", out])
        assert exported.exit_code == 0, exported.output
        with open(out) as file:
            envelope = json.load(file)
        assert envelope["reservations"][0]["session_id"] == "export-session"
        assert envelope["claim_history"][0]["id"] == claim_id

        imported = runner.invoke(cli, ["import", "--file", out])
        assert imported.exit_code == 0, imported.output
        new_sid = int(imported.output.split(" as #")[1].split(" ")[0])
        new_item = db.list_work_items(conn, sprint_id=new_sid)[0]
        assert db.list_reservations(conn, new_item["id"])[0]["session_id"] == "export-session"
        assert conn.execute("SELECT count(*) FROM claim_history WHERE work_item_id = ?", (new_item["id"],)).fetchone()[0] == 1

    def test_import_creates_sprint_with_new_id(self, runner, conn, db_path, tmp_path):
        sid, _ = self._build_sprint(runner, conn, db_path)
        out = str(tmp_path / "export.json")
        runner.invoke(cli, ["export", "--sprint-id", str(sid), "--output", out])
        result = runner.invoke(cli, ["import", "--file", out])
        assert result.exit_code == 0, result.output
        # Extract new sprint ID from output "Imported sprint 'Expo' as #N"
        new_sid = int(result.output.split(" as #")[1].split(" ")[0])
        assert new_sid != sid
        fresh = db.get_connection(db_path)
        new_sprint = db.get_sprint(fresh, new_sid)
        fresh.close()
        assert new_sprint is not None
        assert new_sprint["name"] == "Expo"

    def test_import_preserves_items_and_events(self, runner, conn, db_path, tmp_path):
        sid, _ = self._build_sprint(runner, conn, db_path)
        out = str(tmp_path / "export.json")
        runner.invoke(cli, ["export", "--sprint-id", str(sid), "--output", out])
        result = runner.invoke(cli, ["import", "--file", out])
        new_sid = int(result.output.split(" as #")[1].split(" ")[0])
        fresh = db.get_connection(db_path)
        items = db.list_work_items(fresh, sprint_id=new_sid)
        assert len(items) == 1
        assert items[0]["title"] == "Do the thing"
        assert items[0]["assignee"] == "alice"
        events = db.list_events(fresh, new_sid)
        fresh.close()
        assert any(e["event_type"] == "progress" for e in events)
        # source_id traceback is embedded in payload
        assert any(
            json.loads(e["payload"]).get("source_id") is not None
            for e in events if e["event_type"] == "progress"
        )

    def test_import_missing_file_exits(self, runner, db_path):
        result = runner.invoke(cli, ["import", "--file", "/does/not/exist.json"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 10: maintain check --json
# ---------------------------------------------------------------------------

class TestMaintainCheckJson:
    def test_check_json_valid_output(self, runner, conn, active_sprint):
        result = runner.invoke(
            cli, ["maintain", "check", "--sprint-id", str(active_sprint["id"]), "--json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "sprint" in data
        assert "risk" in data
        assert "stale_items" in data
        assert "track_health" in data
        assert "threshold_hours" in data

    def test_check_json_risk_fields(self, runner, conn, active_sprint):
        result = runner.invoke(
            cli, ["maintain", "check", "--sprint-id", str(active_sprint["id"]), "--json"]
        )
        data = json.loads(result.output)
        risk = data["risk"]
        assert "days_remaining" in risk
        assert "active_items" in risk
        assert "at_risk" in risk
        assert "overdue" in risk


# ---------------------------------------------------------------------------
# Group 11: sprint show --detail
# ---------------------------------------------------------------------------

class TestSprintShowDetail:
    def test_detail_flag_shows_health_line(self, runner, conn, active_sprint):
        result = runner.invoke(cli, ["sprint", "show", "--id", str(active_sprint["id"]), "--detail"])
        assert result.exit_code == 0, result.output
        assert "Health:" in result.output
        assert "days remaining" in result.output

    def test_detail_flag_json_includes_health(self, runner, conn, active_sprint):
        result = runner.invoke(
            cli,
            ["sprint", "show", "--id", str(active_sprint["id"]), "--detail", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["id"] == active_sprint["id"]
        assert "detail" in data
        assert "risk" in data["detail"]
        assert "stale_count" in data["detail"]
        assert "track_health" in data["detail"]

    def test_detail_flag_shows_track_health(self, runner, conn, active_sprint):
        tid = db.get_or_create_track(conn, active_sprint["id"], "mytrack")
        db.create_work_item(conn, active_sprint["id"], tid, "Item A")
        result = runner.invoke(cli, ["sprint", "show", "--id", str(active_sprint["id"]), "--detail"])
        assert result.exit_code == 0, result.output
        assert "Track health:" in result.output
        assert "mytrack" in result.output

    def test_show_without_detail_omits_health(self, runner, active_sprint):
        result = runner.invoke(cli, ["sprint", "show", "--id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Health:" not in result.output


class TestSprintShowWatch:
    def test_watch_rejects_json_mode(self, runner, active_sprint):
        result = runner.invoke(
            cli,
            ["sprint", "show", "--id", str(active_sprint["id"]), "--watch", "--json"],
        )
        assert result.exit_code == 1
        assert "--watch cannot be combined with --json" in result.output

    def test_watch_requires_positive_interval(self, runner, active_sprint):
        result = runner.invoke(
            cli,
            ["sprint", "show", "--id", str(active_sprint["id"]), "--watch", "--interval", "0"],
        )
        assert result.exit_code == 1
        assert "--interval must be > 0" in result.output

    def test_watch_renders_and_exits_on_keyboard_interrupt(self, runner, active_sprint, monkeypatch):
        def _interrupt_after_first_sleep(_seconds: float) -> None:
            raise KeyboardInterrupt()

        monkeypatch.setattr("sprintctl.cli.time.sleep", _interrupt_after_first_sleep)
        result = runner.invoke(
            cli,
            ["sprint", "show", "--id", str(active_sprint["id"]), "--watch", "--interval", "0.01"],
        )
        assert result.exit_code == 0, result.output
        assert "watch refresh" in result.output
        assert active_sprint["name"] in result.output
        assert "Watch mode stopped." in result.output

    def test_clear_terminal_helper_uses_tty_clear_sequence(self):
        class _TTYBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = _TTYBuffer()
        cleared = cli_module._clear_terminal_for_watch(
            stdout=stream,
            term="xterm-256color",
        )
        assert cleared is True
        assert stream.getvalue() == "\x1b[2J\x1b[H"


class TestHelpCommands:
    def test_retired_claim_help_does_not_create_db(self, runner, tmp_path, monkeypatch):
        db_path = tmp_path / "help" / "test.db"
        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        result = runner.invoke(cli, ["claim", "--help"])
        assert result.exit_code != 0
        assert "No such command 'claim'" in result.output
        assert not db_path.exists()

    def test_agent_protocol_help_does_not_create_db(self, runner, tmp_path, monkeypatch):
        db_path = tmp_path / "help-protocol" / "test.db"
        monkeypatch.setenv("SPRINTCTL_DB", str(db_path))
        result = runner.invoke(cli, ["agent-protocol", "--help"])
        assert result.exit_code == 0, result.output
        assert not db_path.exists()


# ---------------------------------------------------------------------------
# Group 12: item note
# ---------------------------------------------------------------------------

class TestItemNote:
    def test_note_creates_event(self, runner, conn, active_sprint):
        tid = db.get_or_create_track(conn, active_sprint["id"], "t")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Task")
        result = runner.invoke(
            cli,
            ["item", "note", "--id", str(iid), "--type", "decision",
             "--summary", "Use postgres", "--actor", "alice"],
        )
        assert result.exit_code == 0, result.output
        events = db.list_events(conn, active_sprint["id"])
        assert len(events) == 1
        assert events[0]["event_type"] == "decision"
        assert events[0]["work_item_id"] == iid
        assert events[0]["source_type"] == "actor"
        assert json.loads(events[0]["payload"])["summary"] == "Use postgres"

    def test_note_with_detail_and_tags(self, runner, conn, active_sprint):
        tid = db.get_or_create_track(conn, active_sprint["id"], "t")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Task")
        runner.invoke(
            cli,
            ["item", "note", "--id", str(iid), "--type", "update",
             "--summary", "Halfway done", "--detail", "Extended info",
             "--tags", "arch,performance", "--actor", "bob"],
        )
        events = db.list_events(conn, active_sprint["id"])
        payload = json.loads(events[0]["payload"])
        assert payload["detail"] == "Extended info"
        assert payload["tags"] == ["arch", "performance"]

    def test_note_sprint_id_inferred_from_item(self, runner, conn, active_sprint):
        tid = db.get_or_create_track(conn, active_sprint["id"], "t")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Task")
        result = runner.invoke(
            cli,
            ["item", "note", "--id", str(iid), "--type", "blocker",
             "--summary", "Waiting on infra", "--actor", "charlie"],
        )
        assert result.exit_code == 0, result.output
        events = db.list_events(conn, active_sprint["id"])
        assert events[0]["sprint_id"] == active_sprint["id"]

    def test_note_invalid_item_exits(self, runner, db_path):
        result = runner.invoke(
            cli,
            ["item", "note", "--id", "9999", "--type", "note",
             "--summary", "Ghost", "--actor", "nobody"],
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 13: item show
# ---------------------------------------------------------------------------

class TestItemShow:
    def test_item_show_basic(self, runner, conn, active_sprint, db_path, tmp_path):
        tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
        iid = db.create_work_item(
            conn, active_sprint["id"], tid, "Build API", "Implement the API"
        )
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "Build API" in result.output
        assert "Implement the API" in result.output
        assert "pending" in result.output
        assert f"{tmp_path.name}#{iid}" in result.output
        assert "Context: repo=" in result.output

    def test_item_show_json(self, runner, conn, active_sprint, db_path, tmp_path):
        tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Build API")
        result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["item"]["title"] == "Build API"
        assert "events" in data
        assert "active_reservations" in data
        assert data["resolved_context"] == {
            "repo_id": tmp_path.name,
            "repo_source": "cwd",
            "backend": "local",
            "target": "local SQLite",
        }

    def test_item_show_includes_events(self, runner, conn, active_sprint, db_path):
        tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Auth task")
        db.create_event(
            conn, active_sprint["id"], actor="dev", event_type="decision",
            work_item_id=iid, payload={"summary": "Use RS256"},
        )
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "decision" in result.output

    def test_item_show_unknown_id_exits(self, runner, db_path):
        result = runner.invoke(cli, ["item", "show", "--id", "9999"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Group 10: usage command
# ---------------------------------------------------------------------------

class TestUsageCommand:
    def test_usage_exits_zero(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        assert result.exit_code == 0, result.output

    def test_usage_covers_major_groups(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        for section in ("SPRINT", "ITEM", "RESERVATION", "EVENT", "MAINTAIN", "TOP-LEVEL", "ENV"):
            assert section in result.output, f"Missing section: {section}"

    def test_usage_mentions_key_commands(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        for cmd in ("sprint create", "item add", "item edit", "reservation reserve", "reservation list", "maintain check", "handoff", "render"):
            assert cmd in result.output, f"Missing command: {cmd}"

    def test_usage_mentions_env_vars(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        assert "SPRINTCTL_DB" in result.output
        assert "SPRINTCTL_STALE_THRESHOLD" in result.output

    def test_usage_does_not_create_db(self, runner, tmp_path, monkeypatch):
        db_file = tmp_path / "nodb.db"
        monkeypatch.setenv("SPRINTCTL_DB", str(db_file))
        result = runner.invoke(cli, ["usage"])
        assert result.exit_code == 0
        assert not db_file.exists()


# ---------------------------------------------------------------------------
# Group 11: render --output
# ---------------------------------------------------------------------------

class TestRenderOutput:
    def test_render_output_writes_file(self, runner, active_sprint, tmp_path):
        out = tmp_path / "sprint.txt"
        result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"]), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        content = out.read_text()
        assert "S1" in content
        assert "Rendered:" in content

    def test_render_output_confirms_path(self, runner, active_sprint, tmp_path):
        out = tmp_path / "out.txt"
        result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"]), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert str(out) in result.output

    def test_render_stdout_unchanged_without_output(self, runner, active_sprint):
        result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "SPRINT:" in result.output


# ---------------------------------------------------------------------------
# Group 12: render improvements (items summary + refs in render)
# ---------------------------------------------------------------------------

class TestRenderImprovements:
    def test_render_items_summary_line(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "eng", "--title", "Task A"])
        result = runner.invoke(cli, ["render", "--sprint-id", sid])
        assert result.exit_code == 0, result.output
        assert "Items:" in result.output
        assert "total" in result.output

    def test_render_items_summary_counts(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "eng", "--title", "Task A"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "eng", "--title", "Task B"])
        result = runner.invoke(cli, ["render", "--sprint-id", sid])
        assert result.exit_code == 0, result.output
        assert "2 total" in result.output

    def test_render_no_items_summary_when_empty(self, runner, active_sprint):
        result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"])])
        assert result.exit_code == 0, result.output
        assert "Items:" not in result.output

    def test_render_shows_refs_under_item(self, runner, conn, active_sprint, db_path):
        sid = active_sprint["id"]
        tid = db.get_or_create_track(conn, sid, "eng")
        iid = db.create_work_item(conn, sid, tid, "Auth task")
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/7", "Auth PR")
        result = runner.invoke(cli, ["render", "--sprint-id", str(sid)])
        assert result.exit_code == 0, result.output
        assert "github.com/org/repo/pull/7" in result.output
        assert "Auth PR" in result.output

    def test_render_ref_type_label_shown(self, runner, conn, active_sprint, db_path):
        sid = active_sprint["id"]
        tid = db.get_or_create_track(conn, sid, "eng")
        iid = db.create_work_item(conn, sid, tid, "Spec task")
        db.add_ref(conn, iid, "doc", "https://docs.example.com/spec")
        result = runner.invoke(cli, ["render", "--sprint-id", str(sid)])
        assert result.exit_code == 0, result.output
        assert "doc" in result.output
        assert "docs.example.com" in result.output

    def test_render_no_refs_section_when_item_has_none(self, runner, conn, active_sprint):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "eng", "--title", "Plain task"])
        result = runner.invoke(cli, ["render", "--sprint-id", sid])
        assert result.exit_code == 0, result.output
        assert "ref [" not in result.output


# ---------------------------------------------------------------------------
# Group 13: handoff --format text
# ---------------------------------------------------------------------------

class TestHandoffTextMode:
    def test_handoff_text_format_stdout(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, [
            "handoff", "--sprint-id", str(active_sprint["id"]),
            "--output", "-", "--format", "text",
        ])
        assert result.exit_code == 0, result.output
        assert "HANDOFF:" in result.output
        assert "S1" in result.output

    def test_handoff_text_format_shows_ready_section(self, runner, conn, active_sprint, db_path):
        sid = str(active_sprint["id"])
        runner.invoke(cli, ["item", "add", "--sprint-id", sid, "--track", "eng", "--title", "Pending task"])
        result = runner.invoke(cli, [
            "handoff", "--sprint-id", sid, "--output", "-", "--format", "text",
        ])
        assert result.exit_code == 0, result.output
        assert "READY TO START" in result.output
        assert "Pending task" in result.output

    def test_handoff_text_format_shows_shutdown_protocol(self, runner, active_sprint, db_path):
        result = runner.invoke(cli, [
            "handoff", "--sprint-id", str(active_sprint["id"]),
            "--output", "-", "--format", "text",
        ])
        assert result.exit_code == 0, result.output
        assert "SHUTDOWN PROTOCOL:" in result.output
        assert "RESUME PATH:" in result.output

    def test_handoff_text_format_writes_txt_file(self, runner, active_sprint, db_path, tmp_path):
        out = tmp_path / "handoff.txt"
        result = runner.invoke(cli, [
            "handoff", "--sprint-id", str(active_sprint["id"]),
            "--output", str(out), "--format", "text",
        ])
        assert result.exit_code == 0, result.output
        assert out.exists()
        content = out.read_text()
        assert "HANDOFF:" in content

    def test_handoff_json_format_still_works(self, runner, active_sprint, db_path, tmp_path):
        out = tmp_path / "handoff.json"
        result = runner.invoke(cli, [
            "handoff", "--sprint-id", str(active_sprint["id"]),
            "--output", str(out), "--format", "json",
        ])
        assert result.exit_code == 0, result.output
        import json
        data = json.loads(out.read_text())
        assert "sprint" in data
        assert data["sprint"]["name"] == "S1"

    def test_handoff_text_shows_active_reservations(self, runner, conn, active_sprint, db_path):
        sid = active_sprint["id"]
        tid = db.get_or_create_track(conn, sid, "eng")
        iid = db.create_work_item(conn, sid, tid, "Reserved task")
        db.reserve(conn, iid, actor="agent-x", session_id="session-x")
        result = runner.invoke(cli, [
            "handoff", "--sprint-id", str(sid), "--output", "-", "--format", "text",
        ])
        assert result.exit_code == 0, result.output
        assert "ACTIVE RESERVATIONS" in result.output
        assert "agent-x" in result.output
