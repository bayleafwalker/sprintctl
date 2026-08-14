"""Structural checks for the incremental CLI command extraction."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from sprintctl import pg
import sprintctl.cli as cli_module
from sprintctl.cli import cli


def _module_imports(module_name: str) -> tuple[set[str | None], set[str]]:
    path = Path(cli_module.__file__).with_name("commands") / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return imported_modules, imported_names


def test_extracted_command_modules_have_no_back_edge_to_cli():
    for module_name in (
        "db",
        "doctor",
        "lifecycle",
        "operations",
        "remote_schema",
        "repo",
        "session",
        "transfer",
        "work",
    ):
        imported_modules, imported_names = _module_imports(module_name)

        assert "sprintctl.cli" not in imported_modules
        assert "cli" not in imported_names


def test_root_cli_has_no_inline_command_decorators():
    source = Path(cli_module.__file__).read_text(encoding="utf-8")

    assert "@cli.command" not in source
    assert "@cli.group" not in source


def test_cli_is_a_small_composition_root_with_runtime_support_outside_it():
    root_path = Path(cli_module.__file__)
    runtime_path = root_path.with_name("cli_runtime.py")

    assert len(root_path.read_text(encoding="utf-8").splitlines()) <= 300
    assert runtime_path.is_file()
    assert len(runtime_path.read_text(encoding="utf-8").splitlines()) <= 1000


def test_extracted_doctor_and_session_commands_preserve_order_and_guards():
    assert list(cli.commands)[:19] == [
        "doctor",
        "sprint",
        "item",
        "event",
        "authority",
        "projection-reads",
        "sync",
        "takeup",
        "maintain",
        "db",
        "export",
        "import",
        "claim",
        "handoff",
        "agent-protocol",
        "next-work",
        "context-candidates",
        "session",
        "usage",
    ]
    assert list(cli.commands)[19:23] == [
        "git-context",
        "render",
        "migrate-to-remote",
        "remote-backfill",
    ]
    assert cli_module.doctor_cmd is cli.commands["doctor"]
    assert cli_module.handoff_cmd is cli.commands["handoff"]
    assert cli_module.session is cli.commands["session"]
    assert cli_module.usage_cmd is cli.commands["usage"]

    leaves = {
        name: cli.commands[name]
        for name in (
            "doctor",
            "handoff",
            "agent-protocol",
            "next-work",
            "context-candidates",
            "usage",
            "git-context",
            "render",
            "migrate-to-remote",
            "remote-backfill",
        )
    }
    assert {
        getattr(command.callback, "__served_guard_path__", None)
        for command in leaves.values()
    } == set(leaves)


def test_extracted_remote_schema_leaves_receive_served_guard_markers():
    leaves = {
        "remote-schema check": cli.commands["remote-schema"].commands["check"],
        "remote-schema migrate": cli.commands["remote-schema"].commands["migrate"],
        "remote-schema stage-maintenance-bridge": cli.commands["remote-schema"].commands[
            "stage-maintenance-bridge"
        ],
    }

    assert {
        getattr(command.callback, "__served_guard_path__", None)
        for command in leaves.values()
    } == set(leaves)


def test_extracted_repo_preserves_order_aliases_and_served_guard_markers():
    assert list(cli.commands)[-2:] == ["repo", "remote-schema"]
    assert list(cli.commands["repo"].commands) == ["list", "delete"]
    assert cli_module.repo is cli.commands["repo"]
    assert cli_module.repo_list is cli.commands["repo"].commands["list"]
    assert cli_module.repo_delete is cli.commands["repo"].commands["delete"]

    leaves = {
        "repo list": cli.commands["repo"].commands["list"],
        "repo delete": cli.commands["repo"].commands["delete"],
    }
    assert {
        getattr(command.callback, "__served_guard_path__", None)
        for command in leaves.values()
    } == set(leaves)


def test_extracted_repo_keeps_cli_get_store_monkeypatch_seam(runner, monkeypatch):
    store = SimpleNamespace(conn=object())
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: (store, pg))
    monkeypatch.setattr(pg, "list_repos", lambda conn: ["alpha", "beta"])

    result = runner.invoke(cli, ["repo", "list"])

    assert result.exit_code == 0, result.output
    assert result.output == "alpha\nbeta\n"


def test_extracted_db_preserves_order_aliases_and_served_guard_markers():
    assert list(cli.commands)[7:11] == ["takeup", "maintain", "db", "export"]
    assert list(cli.commands["db"].commands) == [
        "vacuum",
        "integrity",
        "recover-from-remote",
    ]
    assert cli_module.db_group is cli.commands["db"]
    assert cli_module.db_vacuum is cli.commands["db"].commands["vacuum"]
    assert cli_module.db_integrity is cli.commands["db"].commands["integrity"]
    assert cli_module.db_recover_from_remote is cli.commands["db"].commands[
        "recover-from-remote"
    ]

    leaves = {
        "db vacuum": cli.commands["db"].commands["vacuum"],
        "db integrity": cli.commands["db"].commands["integrity"],
        "db recover-from-remote": cli.commands["db"].commands["recover-from-remote"],
    }
    assert {
        getattr(command.callback, "__served_guard_path__", None)
        for command in leaves.values()
    } == set(leaves)


def test_extracted_db_maintenance_keeps_cli_get_store_monkeypatch_seam(runner, monkeypatch):
    store = SimpleNamespace(conn=object())
    monkeypatch.setattr(cli_module, "_get_store", lambda _obj: (store, pg))
    monkeypatch.setattr(pg, "check_integrity", lambda conn: {
        "backend": "local",
        "ok": True,
        "integrity_check": ["ok"],
        "foreign_key_violations": [],
        "table_counts": {"sprint": 0},
    })

    result = runner.invoke(cli, ["db", "integrity", "--json"])

    assert result.exit_code == 0, result.output
    assert '"ok": true' in result.output


def test_extracted_transfer_preserves_order_aliases_and_served_guard_markers():
    assert list(cli.commands)[10:12] == ["export", "import"]
    assert cli_module.export_cmd is cli.commands["export"]
    assert cli_module.import_cmd is cli.commands["import"]

    leaves = {"export": cli.commands["export"], "import": cli.commands["import"]}
    assert {
        getattr(command.callback, "__served_guard_path__", None)
        for command in leaves.values()
    } == set(leaves)
