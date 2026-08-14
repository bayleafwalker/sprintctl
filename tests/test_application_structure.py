"""Structural checks for the application service split."""

from __future__ import annotations

import ast
from pathlib import Path

import sprintctl.application as application


def test_application_compatibility_module_reexports_service_classes():
    assert application.WorkApplication.__module__ == "sprintctl.work_application"
    assert application.ProjectWorkApplication.__module__ == "sprintctl.project_application"
    assert application.ProjectMemberApplication.__module__ == "sprintctl.project_application"
    assert application.batch_idempotency_key is not None
    assert application.cutover.__name__ == "sprintctl.cutover"


def test_application_service_modules_do_not_import_cli():
    root = Path(application.__file__).parent
    for module_name in ("application_common", "work_application", "project_application"):
        path = root / f"{module_name}.py"
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


def test_application_compatibility_module_contains_no_service_implementations():
    path = Path(application.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert not any(isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body)
