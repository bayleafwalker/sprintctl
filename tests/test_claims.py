"""Regression tests for the credential-free reservation replacement.

The former claim suite protected token leases, proof checks, heartbeats and
handoff rotation.  Those mechanisms are deliberately retired: coordination is
now an advisory, session-bound reservation and item mutations use normal CAS.
"""

from __future__ import annotations

import json

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, active_sprint, title: str = "Task") -> int:
    track_id = db.get_or_create_track(conn, active_sprint["id"], "eng")
    return db.create_work_item(conn, active_sprint["id"], track_id, title)


def test_reservation_reserve_json_is_credential_free(runner, conn, active_sprint):
    item_id = _item(conn, active_sprint)

    result = runner.invoke(
        cli,
        ["reservation", "reserve", "--item-id", str(item_id), "--actor", "bot-1",
         "--session-id", "session-1", "--correlation-ref", "actionq:receipt:17", "--json"],
    )

    assert result.exit_code == 0, result.output
    reservation = json.loads(result.output)
    assert reservation["work_item_id"] == item_id
    assert reservation["actor"] == "bot-1"
    assert reservation["session_id"] == "session-1"
    assert reservation["correlation_ref"] == "actionq:receipt:17"
    assert reservation["state"] == "active"
    assert not {"claim_token", "ownership_proof", "lease_epoch"} & reservation.keys()


def test_reservation_cli_conflict_requires_explicit_override(runner, conn, active_sprint):
    item_id = _item(conn, active_sprint)
    first = runner.invoke(cli, ["reservation", "reserve", "--item-id", str(item_id), "--actor", "one", "--session-id", "s1", "--json"])
    assert first.exit_code == 0, first.output

    blocked = runner.invoke(cli, ["reservation", "reserve", "--item-id", str(item_id), "--actor", "two", "--session-id", "s2", "--json"])
    assert blocked.exit_code != 0
    assert "--override" in blocked.output

    replacement = runner.invoke(cli, ["reservation", "reserve", "--item-id", str(item_id), "--actor", "two", "--session-id", "s2", "--override", "--json"])
    assert replacement.exit_code == 0, replacement.output
    assert db.get_reservation(conn, json.loads(first.output)["id"])["state"] == "interrupted"


def test_touch_rejects_a_different_session_without_secret(runner, conn, active_sprint):
    reservation = db.reserve(conn, _item(conn, active_sprint), actor="one", session_id="s1")

    result = runner.invoke(cli, ["reservation", "touch", "--id", str(reservation["id"]), "--session-id", "s2"])

    assert result.exit_code != 0
    assert "another session" in result.output


def test_reassign_and_release_need_no_credentials(runner, conn, active_sprint):
    reservation = db.reserve(conn, _item(conn, active_sprint), actor="one", session_id="s1")

    reassigned = runner.invoke(cli, ["reservation", "reassign", "--id", str(reservation["id"]), "--actor", "two", "--session-id", "s2", "--json"])
    assert reassigned.exit_code == 0, reassigned.output
    assert json.loads(reassigned.output)["actor"] == "two"

    released = runner.invoke(cli, ["reservation", "release", "--id", str(reservation["id"]), "--actor", "operator", "--json"])
    assert released.exit_code == 0, released.output
    assert json.loads(released.output)["state"] == "released"


def test_reservation_list_and_show_support_item_filter(runner, conn, active_sprint):
    first_item = _item(conn, active_sprint, "First")
    second_item = _item(conn, active_sprint, "Second")
    first = db.reserve(conn, first_item, actor="one", session_id="s1")
    db.reserve(conn, second_item, actor="two", session_id="s2")

    listed = runner.invoke(cli, ["reservation", "list", "--item-id", str(first_item), "--json"])
    assert listed.exit_code == 0, listed.output
    assert [row["id"] for row in json.loads(listed.output)] == [first["id"]]

    shown = runner.invoke(cli, ["reservation", "show", "--id", str(first["id"]), "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["work_item_id"] == first_item


def test_reservation_list_all_includes_released_history(runner, conn, active_sprint):
    reservation = db.reserve(conn, _item(conn, active_sprint), actor="one", session_id="s1")
    db.release_reservation(conn, reservation["id"])

    active = runner.invoke(cli, ["reservation", "list", "--json"])
    assert active.exit_code == 0, active.output
    assert json.loads(active.output) == []

    history = runner.invoke(cli, ["reservation", "list", "--all", "--json"])
    assert history.exit_code == 0, history.output
    assert json.loads(history.output)[0]["state"] == "released"


def test_claim_command_is_not_a_compatibility_alias(runner):
    result = runner.invoke(cli, ["claim", "create"])

    assert result.exit_code != 0
    assert "No such command 'claim'" in result.output
