"""Execute generated status guidance against existing CLI backend boundaries."""

import json
import shlex
import sys

import pytest

from sprintctl import db
import sprintctl.cli as cli_module
from sprintctl.cli import cli
from tests.test_served_routes import _configure_served_repo


def _protocol(runner):
    result = runner.invoke(cli, ["agent-protocol", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _status_argv(command, *, item_id=7, revision="unused"):
    return shlex.split(command.replace("<item_id>", str(item_id))
        .replace("active|done|blocked", "active")
        .replace("<name>", "fixture-reader")
        .replace("<revision>", revision))[1:]


def test_protocol_retains_local_compatibility_and_explains_both_backends(runner):
    protocol = _protocol(runner)
    step = protocol["lifecycle"]["3_status_transition"]
    assert protocol["sprintctl_agent_protocol_version"] == "3"
    assert step["command_backend"] == "local"
    assert step["command"] == step["backend_commands"]["local"]
    assert "--expected-revision" in step["backend_commands"]["local"]
    assert "--expected-revision" not in step["backend_commands"]["served"]
    assert "--actor" not in step["backend_commands"]["served"]
    assert "authenticated" in step["note"]
    assert "immutable basis" in step["note"]
    assert protocol["reservation_model"]["ownership_proof"] is None
    assert protocol["reservation_model"]["exclusive"] is False


def test_text_protocol_labels_each_generated_status_command(runner):
    step = _protocol(runner)["lifecycle"]["3_status_transition"]
    result = runner.invoke(cli, ["agent-protocol"])
    assert result.exit_code == 0, result.output
    for backend, command in step["backend_commands"].items():
        assert f"[{backend}] $ {command}" in result.output
    assert step["note"] in result.output


def test_generated_local_command_obeys_real_revision_cas(runner, conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "protocol")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Protocol fixture")
    revision = db.item_status_revision(db.get_work_item(conn, item_id))
    command = _protocol(runner)["lifecycle"]["3_status_transition"]["backend_commands"]["local"]
    argv = _status_argv(command, item_id=item_id, revision=revision)
    result = runner.invoke(cli, argv)
    assert result.exit_code == 0, result.output
    assert db.get_work_item(conn, item_id)["status"] == "active"
    # The emitted revision is a comparison basis, not a bypass on reuse.
    argv[argv.index("--status") + 1] = "blocked"
    stale = runner.invoke(cli, argv)
    assert stale.exit_code != 0
    assert "status revision mismatch" in stale.output
    assert db.get_work_item(conn, item_id)["status"] == "active"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served mode requires Python 3.12+")
def test_generated_served_command_reaches_owner_without_local_cas_override(runner, tmp_path, monkeypatch):
    step = _protocol(runner)["lifecycle"]["3_status_transition"]
    _configure_served_repo(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(cli_module, "_get_store", lambda _: pytest.fail("opened local state"))
    monkeypatch.setattr(cli_module, "_served_item_status", lambda *args: calls.append(args))
    result = runner.invoke(cli, _status_argv(step["backend_commands"]["served"]))
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][1:5] == (7, "active", None, None)
    # The old local-only instruction still fails in served mode; validation
    # stays unchanged instead of silently ignoring an invalid override.
    refused = runner.invoke(cli, _status_argv(step["command"]))
    assert refused.exit_code == 1
    assert "direct-backend CAS option" in refused.output
    assert len(calls) == 1
