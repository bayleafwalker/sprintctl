"""Structural checks for the incremental CLI command extraction."""

from __future__ import annotations

import ast
from pathlib import Path

import sprintctl.cli as cli_module
from sprintctl.cli import cli


def test_extracted_remote_schema_module_has_no_back_edge_to_cli():
    path = Path(cli_module.__file__).with_name("commands") / "remote_schema.py"
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
