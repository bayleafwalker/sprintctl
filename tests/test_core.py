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

    def test_blocked_can_revive_to_active(self, runner, conn, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "blocked"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        assert result.exit_code == 0, result.output
        assert db.get_work_item(conn, iid)["status"] == "active"

    def test_invalid_transition_blocked_to_done(self, runner, active_sprint):
        iid = self._add_item(runner, active_sprint["id"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "active"])
        runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "blocked"])
        result = runner.invoke(cli, ["item", "status", "--id", str(iid), "--status", "done"])
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
        assert version == len(db._MIGRATIONS)

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
    def test_item_show_basic(self, runner, conn, active_sprint, db_path):
        tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Build API")
        result = runner.invoke(cli, ["item", "show", "--id", str(iid)])
        assert result.exit_code == 0, result.output
        assert "Build API" in result.output
        assert "pending" in result.output

    def test_item_show_json(self, runner, conn, active_sprint, db_path):
        tid = db.get_or_create_track(conn, active_sprint["id"], "backend")
        iid = db.create_work_item(conn, active_sprint["id"], tid, "Build API")
        result = runner.invoke(cli, ["item", "show", "--id", str(iid), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["item"]["title"] == "Build API"
        assert "events" in data
        assert "active_claims" in data

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
