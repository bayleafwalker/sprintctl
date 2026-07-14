from __future__ import annotations

import json

from sprintctl import db
from sprintctl.cli import cli


def _configure_repo_marker(tmp_path) -> None:
    state = tmp_path / ".sprintctl"
    state.mkdir()
    (state / "backend.json").write_text(
        json.dumps({"backend": "local", "repo_id": tmp_path.name}), encoding="utf-8"
    )


def test_pilot_is_disabled_by_default_and_enable_is_explicit(runner, tmp_path):
    _configure_repo_marker(tmp_path)

    initial = runner.invoke(cli, ["pilot", "status", "--json"])
    assert initial.exit_code == 0, initial.output
    assert json.loads(initial.output)["state"] == "disabled"

    enabled = runner.invoke(cli, ["pilot", "enable", "--json"])
    assert enabled.exit_code == 0, enabled.output
    assert json.loads(enabled.output)["state"] == "enabled"

    disabled = runner.invoke(cli, ["pilot", "disable", "--json"])
    assert disabled.exit_code == 0, disabled.output
    assert json.loads(disabled.output)["state"] == "disabled"


def test_pilot_mirrors_supported_event_and_reports_parity(runner, conn, active_sprint, tmp_path):
    _configure_repo_marker(tmp_path)
    enabled = runner.invoke(cli, ["pilot", "enable"])
    assert enabled.exit_code == 0, enabled.output

    added = runner.invoke(
        cli,
        [
            "event", "add", "--sprint-id", str(active_sprint["id"]),
            "--type", "work.completed", "--actor", "agent-a",
            "--payload", '{"summary":"pilot evidence"}', "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["shadow_pilot"]["status"] == "mirrored"

    status = runner.invoke(cli, ["pilot", "status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["outbox_records"] == 1

    verified = runner.invoke(
        cli, ["pilot", "verify", "--sprint-id", str(active_sprint["id"]), "--json"]
    )
    assert verified.exit_code == 0, verified.output
    report = json.loads(verified.output)
    assert report["is_equal"] is True
    assert report["counts"] == {"equal": 1, "mismatched": 0, "missing": 0, "unexpected": 0}


def test_pilot_never_mirrors_unclassified_generic_events(runner, conn, active_sprint, tmp_path):
    _configure_repo_marker(tmp_path)
    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0

    added = runner.invoke(
        cli,
        [
            "event", "add", "--sprint-id", str(active_sprint["id"]),
            "--type", "unclassified.event", "--actor", "agent-a", "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["shadow_pilot"]["status"] == "unsupported"

    status = runner.invoke(cli, ["pilot", "status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["outbox_records"] is None


def test_pilot_sync_is_guarded_in_local_backend(runner, tmp_path):
    _configure_repo_marker(tmp_path)
    assert runner.invoke(cli, ["pilot", "enable"]).exit_code == 0

    result = runner.invoke(cli, ["pilot", "sync"])
    assert result.exit_code != 0
    assert "requires a remote sprintctl backend" in result.output
