"""Graph suggestions cannot establish contextual dispatch admissibility."""
import json
import sys

import jsonschema
import pytest

from sprintctl import db, served
from sprintctl.application import ProjectMemberApplication, ProjectWorkApplication
import sprintctl.cli as cli_module
from sprintctl.cli import cli
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.test_served_routes import _configure_served_repo
from tests.test_work_application import _application, _context


OPERATIONS = ("work.read.next-work", "work.project.next-work", "work.project.next-work-explain")


def _project(application, *additional):
    members = (ProjectMemberApplication("test-repo", application), *additional)
    return ProjectWorkApplication("test-project", members, canonical_binding={
        "project_id": "test-project", "backlog_repos": [member.origin_repo for member in members]})


def _seed(conn, sprint):
    track = db.get_or_create_track(conn, sprint["id"], "dispatch")
    return db.create_work_item(conn, sprint["id"], track, "Graph-ready suggestion")


def _validate(operation, result):
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == operation)
    # Existing requests cannot supply an intent, operation or authority context.
    assert set(contract.input_schema["properties"]) == {"sprint_id"}
    jsonschema.validate({}, contract.input_schema)
    jsonschema.validate(result, contract.result_schema)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("ready", [False, True])
def test_next_work_reports_only_graph_readiness(conn, active_sprint, operation, ready):
    item_id = _seed(conn, active_sprint) if ready else None
    application = _application(store=conn, backend=db)
    owner = application if operation == "work.read.next-work" else _project(application)
    before = conn.total_changes
    result = owner.invoke(operation, {}, _context(repo_ids=frozenset({"test-repo"})))
    _validate(operation, result)
    assert result["graph_ready"] is ready
    assert result["dispatch_admissible"] == ("unknown" if ready else "inadmissible")
    assert result["dispatch_reason"] == ("dispatch-context-required" if ready else "no-ready-items")
    assert [row["id"] for row in result["ready_items"]] == ([item_id] if ready else [])
    for member in result.get("repositories", []):
        assert member["dispatch_admissible"] == result["dispatch_admissible"]
        assert member["dispatch_reason"] == result["dispatch_reason"]
    assert conn.total_changes == before


@pytest.mark.parametrize("operation", OPERATIONS[1:])
def test_unavailable_member_preserves_unknown_with_available_suggestions(
        conn, active_sprint, tmp_path, operation):
    item_id = _seed(conn, active_sprint)
    missing = db.get_connection(tmp_path / "missing-member.db")
    db.init_db(missing)  # No selectable sprint: real member resolution fails.
    try:
        project = _project(_application(store=conn, backend=db), ProjectMemberApplication(
            "unavailable-repo", _application(store=missing, backend=db)))
        result = project.invoke(operation, {}, _context(
            repo_ids=frozenset({"test-repo", "unavailable-repo"})))
        _validate(operation, result)
        assert result["graph_ready"] is False
        assert result["dispatch_admissible"] == "unknown"
        assert result["dispatch_reason"] == "member-unavailable"
        assert [row["id"] for row in result["ready_items"]] == [item_id]
        assert result["repositories"][1]["status"] == "unavailable"
    finally:
        missing.close()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served requires Python3.12+")
@pytest.mark.parametrize("project_scope", [False, True])
def test_served_cli_keeps_graph_suggestions_without_positive_dispatch(
        conn, runner, active_sprint, tmp_path, monkeypatch, project_scope):
    item_id = _seed(conn, active_sprint)
    application = _application(store=conn, backend=db)
    project = _project(application)
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _: pytest.fail("opened CLI store"))
    results = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def handshake(self):
            return {}

        async def invoke(self, operation, arguments, **kwargs):
            owner = project if operation.startswith("work.project.") else application
            result = owner.invoke(operation, arguments, _context(repo_ids=frozenset({"test-repo"})))
            _validate(operation, result)
            results.append(result)
            return result

    monkeypatch.setattr(served, "_client", lambda _: Client())
    args = ["next-work", "--json"] + (["--project"] if project_scope else [])
    output = runner.invoke(cli, args)
    assert output.exit_code == 0, output.output
    assert [row["id"] for row in json.loads(output.output)] == [item_id]
    assert len(results) == 1
    assert results[0]["dispatch_admissible"] == "unknown"
    assert results[0]["dispatch_reason"] == "dispatch-context-required"
