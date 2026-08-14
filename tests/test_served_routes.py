import sys

import pytest

from sprintctl.served_routes import (
    OPERATION_SPECS,
    SERVED_COMMAND_ROUTES,
    doctor_probe_operations,
    routes_for,
)
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from sprintctl.vuoro_adapter import validate_served_operation_registry
import sprintctl.cli as cli_module
from sprintctl.cli import cli

_KNOWN_OPERATIONS = {contract.name for contract in WORK_OPERATION_CONTRACTS}


def test_project_context_result_schema_covers_every_aggregate_field():
    contract = next(
        value for value in WORK_OPERATION_CONTRACTS
        if value.name == "work.project.context"
    )
    assert set(contract.result_schema["properties"]) == {
        "contract_version", "project", "summary", "sprints",
        "active_claims", "active_unclaimed_items", "conflicts", "ready_items",
        "blocked_items", "stale_items", "recent_decisions", "next_actions",
        "repositories",
    }


def test_every_route_targets_a_published_operation():
    for route in SERVED_COMMAND_ROUTES:
        assert route.operation in _KNOWN_OPERATIONS, route


def test_operation_specs_are_an_immutable_complete_route_and_probe_view():
    assert tuple((spec.command_path, spec.operation, spec.precondition) for spec in OPERATION_SPECS) == tuple(
        (route.command_path, route.operation, route.precondition)
        for route in SERVED_COMMAND_ROUTES
    )
    assert doctor_probe_operations() == {
        spec.operation for spec in OPERATION_SPECS if spec.probe
    }
    assert all(spec.disposition in {"catalog", "unavailable"} for spec in OPERATION_SPECS)


def test_served_operation_specs_are_all_backed_by_catalog_contracts():
    validate_served_operation_registry()


def test_next_work_has_preconditioned_routes_and_a_distinct_explain_aggregate():
    routes = routes_for("next-work")
    assert {route.operation for route in routes} == {
        "work.read.next-work",
        "work.project.next-work",
    }
    for route in routes:
        assert route.precondition, "next-work routes must be preconditioned"
    explain = routes_for("next-work.explain")
    assert len(explain) == 1
    assert explain[0].operation == "work.read.next-work-explain"
    assert explain[0].precondition


def test_single_route_commands_have_no_ambiguity():
    for route in SERVED_COMMAND_ROUTES:
        if route.command_path in {"next-work", "usage.context", "sprint.list", "item.list"}:
            continue
        assert len(routes_for(route.command_path)) == 1


def test_unknown_command_has_no_routes():
    assert routes_for("db.vacuum") == ()


def test_event_list_routes_to_work_read_events():
    """sprintctl#1247: `event list` gets a served route (`work.read.events`)
    even though no served CLI path invokes it yet -- landing the route ahead
    of the CLI wiring (#1249's event-list sub-part) is the whole point of
    this change."""

    routes = routes_for("event.list")
    assert len(routes) == 1
    assert routes[0].operation == "work.read.events"


def _click_leaf_commands(command, prefix=()):
    if isinstance(command, cli_module.click.Group):
        return {
            path: leaf
            for name, child in command.commands.items()
            for path, leaf in _click_leaf_commands(child, (*prefix, name)).items()
        }
    return {" ".join(prefix): command}


def test_served_disposition_inventory_covers_and_guards_every_click_leaf():
    """A new Click command cannot accidentally regain direct-store fallback."""
    leaves = _click_leaf_commands(cli)

    assert set(leaves) == set(cli_module._served_routes.SERVED_COMMAND_DISPOSITIONS)
    assert all(
        getattr(command.callback, "__served_guard_path__", None) == path
        for path, command in leaves.items()
    )


def _configure_served_repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    marker = tmp_path / ".sprintctl"
    marker.mkdir()
    marker.joinpath("backend.json").write_text(
        '{"backend":"served","repo_id":"served-guard-test"}', encoding="utf-8"
    )
    profile = tmp_path / "profile.json"
    profile.write_text(
        """{
          "schema_version":"vuoro-client-profile/v1",
          "id":"served-guard-test",
          "target":{"environment_id":"test","environment_class":"development","endpoint":"https://vuoro.example.test"},
          "credential_ref":"file:~/.config/vuoro/test"
        }""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPRINTCTL_BACKEND", "served")
    monkeypatch.setenv("SPRINTCTL_VUORO_PROFILE", str(profile))
    monkeypatch.delenv("SPRINTCTL_URL", raising=False)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="served mode requires Python 3.12+",
)
def test_unclassified_legacy_store_paths_fail_before_get_store_in_served_mode(
    runner, tmp_path, monkeypatch
):
    """Representative old direct paths fail at dispatch, never at psycopg."""
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened a store")
    )

    for argv in (
        ["sprint", "kind", "--id", "1", "--kind", "backlog"],
        ["render", "--sprint-id", "1"],
        ["repo", "list"],
    ):
        result = runner.invoke(cli, argv)
        assert result.exit_code == 1, result.output
        assert "served-operation-unavailable" in result.output
        assert "could not connect to postgres" not in result.output.lower()


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="served mode requires Python 3.12+",
)
def test_catalog_classified_command_still_never_opens_store_in_served_mode(
    runner, tmp_path, monkeypatch
):
    _configure_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("served command opened a store")
    )
    monkeypatch.setattr(
        cli_module._served, "read_sprints", lambda *args, **kwargs: {"sprints": []}
    )

    result = runner.invoke(cli, ["sprint", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "[]"


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="served mode requires Python 3.12+",
)
def test_authority_mode_is_a_local_served_consumer_command(runner, tmp_path, monkeypatch):
    """A non-sprintctl manifest can enable its named local rollout pilot."""
    _configure_served_repo(tmp_path, monkeypatch)
    (tmp_path / "vuoro.dispatch.json").write_text(
        '{"schema_version": 1, "repo_id": "vuoro"}', encoding="utf-8"
    )
    monkeypatch.setattr(
        cli_module, "_get_store", lambda _obj: pytest.fail("authority mode opened a store")
    )

    result = runner.invoke(cli, ["authority", "mode", "--set", "enforce", "--json"])

    assert result.exit_code == 0, result.output
    assert '"mode": "enforce"' in result.output
