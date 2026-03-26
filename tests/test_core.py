import json
import os

import pytest

from sprintctl import db
from sprintctl.cli import cli
from sprintctl.render import render_sprint_doc


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
        result = runner.invoke(cli, ["sprint", "create"])
        assert result.exit_code == 2

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

    def test_valid_transition_pending_to_active(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_valid_transition_active_to_done(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "done"])
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "done"

    def test_valid_transition_active_to_blocked(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "blocked"])
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "blocked"

    def test_invalid_transition_pending_to_done(self, runner, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "done"])
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_invalid_transition_pending_to_blocked(self, runner, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "blocked"])
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_invalid_transition_done_is_terminal(self, runner, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "done"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_invalid_transition_blocked_is_terminal(self, runner, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "blocked"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        assert result.exit_code == 1
        assert "cannot transition" in result.output

    def test_item_status_unknown_item_id(self, runner, db_path):
        result = runner.invoke(cli, ["item", "status", "--id", "9999", "--status", "active"])
        assert result.exit_code == 1


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
        assert version == 1

    def test_db_path_from_env(self, tmp_path, monkeypatch):
        custom = str(tmp_path / "custom.db")
        monkeypatch.setenv("SPRINTCTL_DB", custom)
        assert str(db.get_db_path()) == custom

    def test_db_path_default(self, monkeypatch):
        monkeypatch.delenv("SPRINTCTL_DB", raising=False)
        path = db.get_db_path()
        assert path.name == "sprintctl.db"
        assert ".sprintctl" in str(path)
