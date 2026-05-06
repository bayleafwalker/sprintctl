import json
import subprocess

from sprintctl import db
from sprintctl.cli import cli


def _takeup(conn, sprint_id, actor, instance_id, **payload):
    return db.create_event(
        conn,
        sprint_id,
        actor,
        "sprint-taken-up",
        payload={"instance_id": instance_id, **payload},
    )


def _release(conn, sprint_id, actor, instance_id, **payload):
    return db.create_event(
        conn,
        sprint_id,
        actor,
        "sprint-released",
        payload={"instance_id": instance_id, **payload},
    )


def test_migration_adds_takeup_event_lookup_index(conn):
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'index' AND name = 'idx_event_sprint_type_ts'
        """
    ).fetchone()
    assert row is not None


def test_list_active_sprints_allows_multiple_active_sprints(conn, active_sprint):
    second_id = db.create_sprint(conn, "S2", status="active")

    active = db.list_active_sprints(conn)

    assert {sprint["id"] for sprint in active} == {active_sprint["id"], second_id}


def test_takeup_history_pairs_release_by_matched_event_id(conn, active_sprint):
    first = _takeup(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        hostname="host-a",
        pid=111,
        context="cockpit-realign",
    )
    _release(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        reason="done",
        matched_takeup_event_id=first,
    )

    history = db.list_takeup_history(conn, active_sprint["id"])

    assert history["active_takeups"] == []
    assert history["unmatched_releases"] == []
    assert history["released_takeups"] == [
        {
            "sprint_id": active_sprint["id"],
            "actor": "agent-a",
            "actor_kind": "agent",
            "instance_id": "inst-a",
            "hostname": "host-a",
            "pid": 111,
            "runtime_session_id": None,
            "taken_up_at": history["released_takeups"][0]["taken_up_at"],
            "taken_up_event_id": first,
            "context": "cockpit-realign",
            "forced": False,
            "released_at": history["released_takeups"][0]["released_at"],
            "released_event_id": history["released_takeups"][0]["released_event_id"],
            "reason": "done",
            "matched_takeup_event_id": first,
        }
    ]


def test_active_takeups_are_current_per_actor_instance(conn, active_sprint):
    old = _takeup(conn, active_sprint["id"], "agent-a", "inst-a", context="old")
    new = _takeup(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        context="new",
        forced=True,
    )
    other = _takeup(conn, active_sprint["id"], "agent-b", "inst-b")

    active = db.list_active_takeups(conn, active_sprint["id"])

    assert [row["taken_up_event_id"] for row in active] == [new, other]
    assert old not in [row["taken_up_event_id"] for row in active]
    assert active[0]["context"] == "new"
    assert active[0]["forced"] is True


def test_release_without_prior_takeup_is_reported(conn, active_sprint):
    release_id = _release(conn, active_sprint["id"], "agent-a", "inst-a", reason="cleanup")

    history = db.list_takeup_history(conn, active_sprint["id"])

    assert history["active_takeups"] == []
    assert history["released_takeups"] == []
    assert history["unmatched_releases"] == [
        {
            "sprint_id": active_sprint["id"],
            "actor": "agent-a",
            "actor_kind": "agent",
            "instance_id": "inst-a",
            "hostname": None,
            "pid": None,
            "released_at": history["unmatched_releases"][0]["released_at"],
            "released_event_id": release_id,
            "reason": "cleanup",
            "matched_takeup_event_id": None,
        }
    ]


def test_release_without_instance_matches_latest_takeup_for_actor(conn, active_sprint):
    older = _takeup(conn, active_sprint["id"], "agent-a", "older")
    newer = _takeup(conn, active_sprint["id"], "agent-a", "newer")
    _release(conn, active_sprint["id"], "agent-a", None, reason="latest")

    history = db.list_takeup_history(conn, active_sprint["id"])

    assert [row["taken_up_event_id"] for row in history["active_takeups"]] == [older]
    assert [row["taken_up_event_id"] for row in history["released_takeups"]] == [newer]
    assert history["released_takeups"][0]["reason"] == "latest"


def test_takeup_events_remain_plain_json_payloads(conn, active_sprint):
    event_id = _takeup(conn, active_sprint["id"], "agent-a", "inst-a")
    row = conn.execute("SELECT payload FROM event WHERE id = ?", (event_id,)).fetchone()

    payload = json.loads(row["payload"])

    assert payload["tags"] == ["takeup"]


def test_takeup_take_cli_records_json_event(runner, conn, active_sprint, db_path):
    result = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--runtime-session-id", "sess-a",
            "--hostname", "devbox",
            "--pid", "123",
            "--context", "remote-mode-prep",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["operation"] == "takeup_take"
    assert data["sprint_id"] == active_sprint["id"]
    assert data["actor"] == "agent-a"
    assert data["instance_id"] == "inst-a"
    assert data["hostname"] == "devbox"
    assert data["pid"] == 123
    assert data["forced"] is False
    event = db.list_events(conn, active_sprint["id"])[0]
    assert event["event_type"] == "sprint-taken-up"


def test_takeup_take_cli_rejects_same_actor_instance_without_force(runner, conn, active_sprint, db_path):
    args = [
        "takeup", "take",
        "--sprint-id", str(active_sprint["id"]),
        "--actor", "agent-a",
        "--instance-id", "inst-a",
    ]
    first = runner.invoke(cli, args)
    second = runner.invoke(cli, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 2
    assert "Use --force for crash recovery" in second.output


def test_takeup_take_cli_force_records_second_event(runner, conn, active_sprint, db_path):
    args = [
        "takeup", "take",
        "--sprint-id", str(active_sprint["id"]),
        "--actor", "agent-a",
        "--instance-id", "inst-a",
    ]
    first = runner.invoke(cli, args)
    forced = runner.invoke(cli, [*args, "--force", "--json"])

    assert first.exit_code == 0, first.output
    assert forced.exit_code == 0, forced.output
    data = json.loads(forced.output)
    assert data["forced"] is True
    active = db.list_active_takeups(conn, active_sprint["id"])
    assert len(active) == 1
    assert active[0]["forced"] is True


def test_takeup_release_cli_matches_latest_active_takeup(runner, conn, active_sprint, db_path):
    take = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
        ],
    )
    release = runner.invoke(
        cli,
        [
            "takeup", "release",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--reason", "done",
            "--json",
        ],
    )

    assert take.exit_code == 0, take.output
    assert release.exit_code == 0, release.output
    data = json.loads(release.output)
    assert data["operation"] == "takeup_release"
    assert data["reason"] == "done"
    assert data["matched_takeup_event_id"] is not None
    assert db.list_active_takeups(conn, active_sprint["id"]) == []


def test_takeup_release_cli_without_prior_takeup_warns_and_records(runner, conn, active_sprint, db_path):
    result = runner.invoke(
        cli,
        [
            "takeup", "release",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No matching takeup found" in result.output
    data = json.loads(result.output[result.output.index("{"):])
    assert data["matched_takeup_event_id"] is None
    history = db.list_takeup_history(conn, active_sprint["id"])
    assert len(history["unmatched_releases"]) == 1


def test_takeup_list_cli_json_shows_active_takeups(runner, conn, active_sprint, db_path):
    first = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
        ],
    )
    second_sprint = db.create_sprint(conn, "S2", status="active")
    second = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(second_sprint),
            "--actor", "agent-b",
            "--instance-id", "inst-b",
        ],
    )
    result = runner.invoke(cli, ["takeup", "list", "--json"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["operation"] == "takeup_list"
    assert [row["actor"] for row in data["active_takeups"]] == ["agent-a", "agent-b"]
    assert data["released_takeups"] == []


def test_takeup_list_cli_all_history_includes_released(runner, conn, active_sprint, db_path):
    take = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
        ],
    )
    release = runner.invoke(
        cli,
        [
            "takeup", "release",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--reason", "done",
        ],
    )
    result = runner.invoke(
        cli,
        ["takeup", "list", "--sprint-id", str(active_sprint["id"]), "--all-history", "--json"],
    )

    assert take.exit_code == 0, take.output
    assert release.exit_code == 0, release.output
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["active_takeups"] == []
    assert len(data["released_takeups"]) == 1
    assert data["released_takeups"][0]["reason"] == "done"


def test_takeup_show_cli_json_returns_full_sprint_history(runner, conn, active_sprint, db_path):
    active = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
        ],
    )
    unmatched = runner.invoke(
        cli,
        [
            "takeup", "release",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-b",
            "--instance-id", "inst-b",
            "--json",
        ],
    )
    result = runner.invoke(
        cli,
        ["takeup", "show", "--sprint-id", str(active_sprint["id"]), "--json"],
    )

    assert active.exit_code == 0, active.output
    assert unmatched.exit_code == 0, unmatched.output
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["operation"] == "takeup_show"
    assert data["sprint"]["id"] == active_sprint["id"]
    assert [row["actor"] for row in data["active_takeups"]] == ["agent-a"]
    assert [row["actor"] for row in data["unmatched_releases"]] == ["agent-b"]


def test_takeup_list_cli_text_renders_active_table(runner, conn, active_sprint, db_path):
    take = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--hostname", "devbox",
        ],
    )
    result = runner.invoke(cli, ["takeup", "list", "--sprint-id", str(active_sprint["id"])])

    assert take.exit_code == 0, take.output
    assert result.exit_code == 0, result.output
    assert "Active takeups:" in result.output
    assert "agent-a" in result.output
    assert "devbox" in result.output


def _fake_actionctl_sessions(monkeypatch, rows):
    import sprintctl.cli as cli_module

    def fake_run(args, **kwargs):
        if args[:2] == ["actionctl", "sessions"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(rows), stderr="")
        if args and args[0] == "auditctl":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)


def test_takeup_sweep_releases_takeup_when_runtime_session_absent(
    runner,
    conn,
    active_sprint,
    db_path,
    monkeypatch,
):
    _takeup(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        runtime_session_id="aqs:missing",
    )
    _fake_actionctl_sessions(monkeypatch, [])

    result = runner.invoke(cli, ["takeup", "sweep", "--sprint-id", str(active_sprint["id"]), "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["operation"] == "takeup_sweep"
    assert data["released_takeups"][0]["reason"] == "session-not-active"
    history = db.list_takeup_history(conn, active_sprint["id"])
    assert history["active_takeups"] == []
    assert history["released_takeups"][0]["actor"] == "agent-a"
    assert history["released_takeups"][0]["matched_takeup_event_id"] is not None


def test_takeup_sweep_keeps_takeup_when_runtime_session_active(
    runner,
    conn,
    active_sprint,
    db_path,
    monkeypatch,
):
    _takeup(
        conn,
        active_sprint["id"],
        "agent-a",
        "inst-a",
        runtime_session_id="aqs:active",
    )
    _fake_actionctl_sessions(monkeypatch, [{"session_id": "aqs:active", "status": "running"}])

    result = runner.invoke(cli, ["takeup", "sweep", "--sprint-id", str(active_sprint["id"]), "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["released_takeups"] == []
    assert data["skipped_takeups"][0]["reason"] == "session-active"
    assert len(db.list_active_takeups(conn, active_sprint["id"])) == 1


def test_takeup_sweep_stale_after_releases_old_takeup_without_runtime_session(
    runner,
    conn,
    active_sprint,
    db_path,
    monkeypatch,
):
    event_id = _takeup(conn, active_sprint["id"], "agent-a", "inst-a")
    conn.execute(
        "UPDATE event SET created_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00Z", event_id),
    )
    conn.commit()
    _fake_actionctl_sessions(monkeypatch, [])

    result = runner.invoke(
        cli,
        ["takeup", "sweep", "--sprint-id", str(active_sprint["id"]), "--stale-after", "1", "--json"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["released_takeups"][0]["reason"] == "no-session-stale"
    assert db.list_active_takeups(conn, active_sprint["id"]) == []


def test_render_includes_active_takeup_section(runner, conn, active_sprint, db_path):
    take = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--hostname", "devbox",
            "--context", "cockpit-realign",
        ],
    )
    result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"])])

    assert take.exit_code == 0, take.output
    assert result.exit_code == 0, result.output
    assert "Takeup:" in result.output
    assert "agent-a@devbox" in result.output
    assert "ctx: cockpit-realign" in result.output


def test_render_omits_takeup_section_when_empty(runner, active_sprint):
    result = runner.invoke(cli, ["render", "--sprint-id", str(active_sprint["id"])])

    assert result.exit_code == 0, result.output
    assert "Takeup:" not in result.output


def test_sprint_show_detail_json_includes_takeup_block(runner, conn, active_sprint, db_path):
    take = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
        ],
    )
    result = runner.invoke(
        cli,
        ["sprint", "show", "--id", str(active_sprint["id"]), "--detail", "--json"],
    )

    assert take.exit_code == 0, take.output
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["detail"]["takeup"]["active_count"] == 1
    assert data["detail"]["takeup"]["active"][0]["actor"] == "agent-a"


def test_sprint_show_detail_text_includes_active_takeup(runner, conn, active_sprint, db_path):
    take = runner.invoke(
        cli,
        [
            "takeup", "take",
            "--sprint-id", str(active_sprint["id"]),
            "--actor", "agent-a",
            "--instance-id", "inst-a",
            "--hostname", "devbox",
        ],
    )
    result = runner.invoke(cli, ["sprint", "show", "--id", str(active_sprint["id"]), "--detail"])

    assert take.exit_code == 0, take.output
    assert result.exit_code == 0, result.output
    assert "Takeup:" in result.output
    assert "agent-a@devbox" in result.output
