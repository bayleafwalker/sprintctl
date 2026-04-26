import json

from sprintctl import db
from sprintctl.cli import cli


def test_sprint_list_active_shows_all_active_sprints(runner, conn, active_sprint, db_path):
    second = db.create_sprint(conn, "S2", status="active")
    db.create_sprint(conn, "Backlog", status="active", kind="backlog")

    result = runner.invoke(cli, ["sprint", "list", "--active", "--json"])

    assert result.exit_code == 0, result.output
    ids = {sprint["id"] for sprint in json.loads(result.output)}
    assert ids == {active_sprint["id"], second}


def test_render_without_sprint_id_errors_when_multiple_active_sprints(runner, conn, active_sprint, db_path):
    second = db.create_sprint(conn, "S2", status="active")

    result = runner.invoke(cli, ["render"])

    assert result.exit_code == 1
    assert f"Multiple active sprints (#{second}, #{active_sprint['id']})" in result.output
    assert "Pass --sprint-id explicitly" in result.output


def test_sprint_show_without_id_errors_when_multiple_active_sprints(runner, conn, active_sprint, db_path):
    db.create_sprint(conn, "S2", status="active")

    result = runner.invoke(cli, ["sprint", "show"])

    assert result.exit_code == 1
    assert "Multiple active sprints" in result.output
    assert "Pass --id explicitly" in result.output


def test_next_work_without_sprint_id_errors_when_multiple_active_sprints(runner, conn, active_sprint, db_path):
    db.create_sprint(conn, "S2", status="active")

    result = runner.invoke(cli, ["next-work"])

    assert result.exit_code == 1
    assert "Multiple active sprints" in result.output
    assert "Pass --sprint-id explicitly" in result.output


def test_single_active_sprint_still_resolves_implicitly(runner, conn, active_sprint, db_path):
    result = runner.invoke(cli, ["sprint", "show", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == active_sprint["id"]
