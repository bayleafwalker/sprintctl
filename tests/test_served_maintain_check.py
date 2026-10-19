"""Exercise the existing maintenance owner through the served CLI facade."""

import json
import sys

import pytest

from sprintctl import db, served
import sprintctl.cli as cli_module
from sprintctl.cli import cli
from sprintctl.application import ApplicationRejection
from tests.test_served_routes import _configure_served_repo
from tests.test_work_application import _application, _context


pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="served requires Python 3.12+")


@pytest.fixture
def diagnostic_client(conn, runner, tmp_path, monkeypatch):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "_get_store", lambda _: pytest.fail("opened CLI store"))
    application = _application(store=conn, backend=db)
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def handshake(self):
            return {}

        async def invoke(self, operation, arguments, **kwargs):
            calls.append((operation, arguments, kwargs))
            try:
                return application.invoke(operation, arguments, _context())
            except ApplicationRejection as error:
                from vuoro_client import InvocationRejectedError
                raise InvocationRejectedError(
                    error.code, error.message, status_code=error.http_status,
                ) from error

    monkeypatch.setattr(served, "_client", lambda _: Client())
    return calls


def test_empty_diagnostic_is_available_as_json(runner, diagnostic_client):
    result = runner.invoke(cli, ["maintain", "check", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["sprint"] is None
    assert report["findings"] == []
    assert diagnostic_client == [("work.maintain.check", {}, {"repo_id": "served-guard-test"})]


def test_empty_diagnostic_is_readable(runner, diagnostic_client):
    result = runner.invoke(cli, ["maintain", "check"])
    assert result.exit_code == 0, result.output
    assert "No active sprint" in result.output
    assert len(diagnostic_client) == 1


def test_sprint_selection_and_findings_reach_text_output(runner, conn, active_sprint, diagnostic_client):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item = db.create_work_item(conn, active_sprint["id"], track, "Needs a reservation")
    db.set_work_item_status(conn, item, "active")
    before = conn.total_changes
    result = runner.invoke(cli, ["maintain", "check", "--sprint-id", str(active_sprint["id"])])
    assert result.exit_code == 0, result.output
    assert '"S1"' in result.output
    assert "Stale items" in result.output
    assert "active-item-without-reservation" in result.output
    assert diagnostic_client[0][1] == {"sprint_id": active_sprint["id"]}
    assert conn.total_changes == before


def test_unknown_explicit_sprint_keeps_typed_refusal(runner, diagnostic_client):
    result = runner.invoke(cli, ["maintain", "check", "--sprint-id", "9999", "--json"])
    assert result.exit_code == 1
    assert "sprint-not-found" in result.output
    assert "operation-handler-failed" not in result.output


def test_unsupported_threshold_refuses_before_invocation(runner, diagnostic_client):
    result = runner.invoke(cli, ["maintain", "check", "--threshold", "8h"])
    assert result.exit_code != 0
    assert "--threshold" in result.output
    assert "server" in result.output
    assert diagnostic_client == []


def test_unavailable_owner_is_an_error_not_empty_success(runner, diagnostic_client, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise TimeoutError("maintenance owner timed out")

    monkeypatch.setattr(served, "_invoke_operation", unavailable)
    result = runner.invoke(cli, ["maintain", "check", "--json"])
    assert result.exit_code == 1
    assert "maintenance owner timed out" in result.output
    assert "No active sprint" not in result.output
    assert diagnostic_client == []
