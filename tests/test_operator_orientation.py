"""Operator orientation: status, grouped help, generated usage, bounds, fallback."""

from __future__ import annotations

import json
import re
import sys

import click
import pytest
from click.testing import CliRunner

from sprintctl import cli_help, db
from sprintctl.cli import cli


@pytest.fixture
def world(conn):
    """Active sprint with a done item, a stale reservation, a fresh one, free work."""
    sid = db.create_sprint(conn, "S1", "goal", None, None, "active")
    other = db.create_sprint(conn, "S0", "old", None, None, "planned")
    track = db.get_or_create_track(conn, sid, "main")
    ids = [db.create_work_item(conn, sid, track, f"item {i}", "d", None, i) for i in range(1, 6)]
    foreign = db.create_work_item(
        conn, other, db.get_or_create_track(conn, other, "t"), "elsewhere", "d", None, 1
    )
    decision = conn.execute(
        "INSERT INTO work_decision (kind, work_item_id, actor) VALUES ('accept', ?, 'tester')", (ids[4],)
    ).lastrowid
    conn.execute(
        "UPDATE work_item SET status='done', resolution='accepted', terminal_decision_id=? WHERE id=?",
        (decision, ids[4]),
    )
    conn.commit()
    stale = db.reserve(conn, ids[0], actor="old-agent", session_id="s-old")
    db.reserve(conn, ids[1], actor="busy", session_id="s-busy")
    conn.execute(
        "UPDATE reservation SET last_activity_at='2026-01-01T00:00:00Z' WHERE id=?", (stale["id"],)
    )
    conn.commit()
    return {"sprint": sid, "items": ids, "foreign": foreign, "stale": stale["id"]}


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_status_sections_ages_and_next_commands(world):
    result = run("status")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "▲ needs you (1)" in out
    assert re.search(rf"reservation {world['stale']} on #{world['items'][0]} idle \d+d", out)
    assert "◐ in flight (2)" in out
    assert "(2 free, 2 already in flight)" in out
    assert f"next: sprintctl reservation show --id {world['stale']}" in out
    assert f"next: sprintctl reservation reserve --item-id {world['items'][2]}" in out
    assert len(out.splitlines()) <= 24


def test_status_json_has_schema_and_splits_ready(world):
    payload = json.loads(run("status", "--json").output)
    assert payload["schema"] == "sprintctl.status/v1"
    assert [r["reserved_by"] for r in payload["ready"]["reserved"]] == ["busy", "old-agent"] or {
        r["reserved_by"] for r in payload["ready"]["reserved"]
    } == {"busy", "old-agent"}
    assert {i["id"] for i in payload["ready"]["free"]} == set(world["items"][2:4])
    assert payload["needs_you"][0]["kind"] == "stale-reservation"
    assert payload["context"]["fallback"] is None


def test_usage_context_marks_reserved_and_states_real_idle_age(world):
    out = run("usage", "--context").output
    assert "[reserved: busy]" in out
    assert re.search(r"idle up to \d+d \(stale after 4h\)", out)
    assert f"next: sprintctl reservation show --id {world['stale']}" in out


def test_usage_context_json_shape_unchanged(world):
    payload = json.loads(run("usage", "--context", "--json").output)
    assert {"sprint", "summary", "active_reservations", "ready_items", "next_action", "conflicts"} <= set(payload)
    assert "schema" not in payload


def test_next_work_marks_reserved_and_help_states_order(world):
    out = run("next-work").output
    assert "[reserved: busy]" in out
    help_text = " ".join(run("next-work", "--help").output.split())
    assert "by priority (p1 first), then creation order" in help_text


def test_item_list_hides_done_and_other_sprints(world):
    out = run("item", "list").output
    assert "item 5" not in out and "elsewhere" not in out
    assert "… 1 done items hidden (--all)" in out
    assert "… 1 items in other sprints hidden (--all)" in out
    everything = run("item", "list", "--all").output
    assert "item 5" in everything and "elsewhere" in everything


def test_item_list_limit_and_json_compat(world):
    out = run("item", "list", "--limit", "2").output
    assert "… 2 more open items not shown" in out
    full = json.loads(run("item", "list", "--json").output)
    assert len(full) == 6  # bare array, unbounded, done and other sprints included
    assert len(json.loads(run("item", "list", "--json", "--limit", "2").output)) == 2
    assert "\n" not in run("item", "list", "--json").output.strip()


def test_fallback_target_is_announced(tmp_path, monkeypatch):
    monkeypatch.delenv("SPRINTCTL_DB", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = run("item", "list").output
    assert "No items found." in out
    assert "fallback target (no .sprintctl/ marker)" in out and "fix:" in out
    err = run("status")
    assert err.exit_code == 1 and "fallback target" in err.output


def test_handoff_help_and_dry_run_change_nothing(world, tmp_path):
    help_text = run("handoff", "--help").output
    assert "records an event" in help_text and "overwriting" in help_text
    result = run("handoff", "--dry-run")
    assert result.exit_code == 0
    assert "would record a handoff-generated event" in result.output
    assert not list(tmp_path.glob("handoff-*"))
    events = db.list_events(db.get_connection(db.get_db_path()), world["sprint"])
    assert not [e for e in events if e["event_type"] == "handoff-generated"]


def test_root_help_is_grouped_complete_and_untruncated():
    result = run()
    assert result.exit_code == 0
    out = result.output
    assert out.index("Start here:") < out.index("Plumbing:") < out.index("Examples:")
    assert not [ln for ln in out.splitlines() if re.search(r"\w(\.\.\.|…)\s*$", ln)]
    placed = {n for _, names in cli_help.GROUPS for n in names}
    assert placed == set(cli.commands) and set(cli_help.SHORT_HELP) == set(cli.commands)


def test_misspelled_command_suggests():
    result = run("itme")
    assert result.exit_code == 2 and "did you mean item?" in result.output


def test_generated_usage_covers_every_leaf_and_marks_unavailable(monkeypatch):
    def leaves(group, prefix=()):
        for name, cmd in group.commands.items():
            if isinstance(cmd, click.Group):
                yield from leaves(cmd, (*prefix, name))
            else:
                yield " ".join((*prefix, name))

    out = run("usage").output
    for path in leaves(cli):
        assert re.search(rf"^  {re.escape(path)}( |$)", out, re.M), path
    assert "(n/a)" not in out
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", "/nonexistent")
    # Unresolvable served config falls back to the plain reference rather than failing.
    assert run("usage").exit_code == 0


def _snapshot(**over):
    snap = {
        "sprint": {"id": 7, "name": "S", "goal": "", "status": "active"},
        "summary": {"total": 1, "done": 0, "active": 1, "pending": 0, "blocked": 0, "stale": 0,
                    "ready": 0, "waiting_on_dependencies": 0, "active_reservations": 1, "active_unreserved": 0},
        "active_reservations": [], "active_unreserved_items": [], "conflicts": [], "ready_items": [],
        "blocked_items": [], "stale_items": [], "recent_decisions": [],
        "next_action": {"kind": "no-action", "summary": "", "reason": ""},
    }
    snap.update(over)
    return snap


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served mode requires Python 3.12+")
def test_served_status_titles_in_flight_items_and_omits_actor(tmp_path, monkeypatch):
    import json as _json
    import sprintctl.cli as cli_module

    (tmp_path / ".git").mkdir()
    (tmp_path / ".sprintctl").mkdir()
    (tmp_path / ".sprintctl" / "backend.json").write_text(_json.dumps({"backend": "served", "repo_id": "r"}))
    profile = tmp_path / "profile.json"
    profile.write_text(_json.dumps({
        "schema_version": "vuoro-client-profile/v1", "id": "p",
        "target": {"environment_id": "t", "environment_class": "development", "endpoint": "https://v.example.test/"},
        "credential_ref": "file:~/.config/vuoro/test",
    }))
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile))
    monkeypatch.delenv("SPRINTCTL_DB", raising=False)
    reservation = {"id": 3, "work_item_id": 9, "actor": "bob", "session_id": "s", "role": "execution",
                   "activity_age_seconds": 180, "stale": False}
    snap = _snapshot(
        active_reservations=[reservation],
        ready_items=[{"id": 4, "title": "free one", "track": "t"}],
        next_action={"kind": "start-ready-item", "summary": "", "reason": "", "item_id": 4},
    )
    monkeypatch.setattr(cli_module._served, "read_context", lambda profile, *, repo_id, sprint_id=None: snap)
    monkeypatch.setattr(
        cli_module._served, "read_items",
        lambda profile, *, repo_id, sprint_id=None, **kw: {"items": [{"id": 9, "title": "Active reserved work"}]},
    )
    out = run("status")
    assert out.exit_code == 0, out.output
    assert "#9 Active reserved work  bob  3m" in out.output
    assert "next: sprintctl reservation reserve --item-id 4\n" in out.output + "\n"
    assert "--actor" not in out.output
    assert json.loads(run("status", "--json").output)["in_flight"][0]["title"] == "Active reserved work"


def test_local_reserve_command_is_pasteable(world):
    out = run("status").output
    line = next(ln for ln in out.splitlines() if "reservation reserve" in ln)
    assert '--actor "$USER"' in line and "<you>" not in line


def test_status_never_exceeds_24_lines():
    from sprintctl import orientation

    res = [{"id": i, "work_item_id": i, "actor": "a", "session_id": "s", "role": "execution",
            "activity_age_seconds": 99999, "stale": i < 12} for i in range(1, 15)]
    ready = [{"id": 100 + i, "title": f"ready {i}", "track": "t"} for i in range(15)]
    snap = _snapshot(active_reservations=res, ready_items=ready,
                     blocked_items=[{"id": 300, "title": "b", "track": "t"}],
                     summary={**_snapshot()["summary"], "total": 40, "waiting_on_dependencies": 2})
    ctx = {"repo_id": "r", "repo_source": "marker", "backend": "local", "target": "x", "fallback": None}
    text = orientation.render_status(orientation.build_status(snap, ctx))
    assert len(text.splitlines()) <= 24
    assert "more (--all)" in text
    assert len(orientation.render_status(orientation.build_status(snap, ctx), show_all=True).splitlines()) > 24


def test_status_next_matches_usage_context_and_empty_sprint_suggests_item_add(conn, db_path):
    sid = db.create_sprint(conn, "Empty", "g", None, None, "active")
    out = run("status").output
    assert f"next: sprintctl item add --sprint-id {sid}" in out
    assert "next: sprintctl next-work" not in out


def test_item_list_all_with_limit_message_and_empty_open(world, conn):
    out = run("item", "list", "--all", "--limit", "3").output
    assert "… 3 more items not shown (--limit N)" in out
    assert "--limit N or --all" not in out
    for item_id in world["items"][:4]:
        conn.execute("UPDATE work_item SET status='blocked' WHERE id=?", (item_id,))
    # no open items: a sprint of only done work
    sid = db.create_sprint(conn, "Done only", "g", None, None, "active")
    conn.execute("UPDATE sprint SET status='closed' WHERE id=?", (world["sprint"],))
    conn.commit()
    out = run("item", "list").output
    assert f"No open items in sprint #{sid}." in out
    assert "ID " not in out
