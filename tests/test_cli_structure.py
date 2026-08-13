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
    for module_name in ("remote_schema", "repo"):
        imported_modules, imported_names = _module_imports(module_name)

        assert "sprintctl.cli" not in imported_modules
        assert "cli" not in imported_names


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
