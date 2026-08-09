#!/usr/bin/env python3
"""Dependency-free integrity checks for the #2130 owner proof."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "verification/fixtures/maintenance-resource-owner-v1/frozen-owner-contract.json"
EXPECTED_FREEZE = "263cfab83acb1070b1b97809b0df1e099ec8e232aa5499e545b71df4109e627d"
EXPECTED_OPERATIONS = {"work.maintenance.resource.prepare", "work.maintenance.resource.get", "work.maintenance.resource.changes"}
CANDIDATES = (
    "pyproject.toml", "uv.lock", "sprintctl/__init__.py", "sprintctl/db.py", "sprintctl/pg.py",
    "sprintctl/pg_migrations.py", "sprintctl/maintenance_capability.py",
    "sprintctl/maintenance_resource.py", "sprintctl/application.py",
    "sprintctl/vuoro_adapter.py", "sprintctl/pg_testing.py",
    "tests/test_maintenance_capability.py", "tests/test_maintenance_resource.py",
    "tests/test_pg_bootstrap.py", "tests/test_work_application.py",
    "tests/test_work_application_pg.py", "tests/test_vuoro_work_adapter_integration.py",
    "verification/claims/2130-owner-claim.json",
    "verification/contexts/maintenance-resource-owner.json",
    "verification/fixtures/maintenance-resource-owner-v1/frozen-owner-contract.json",
    "verification/validate_maintenance_resource_owner.py",
)


def candidate_digest() -> str:
    value = hashlib.sha256()
    for relative in CANDIDATES:
        value.update(relative.encode() + b"\0" + (ROOT / relative).read_bytes() + b"\0")
    return value.hexdigest()


def main() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == EXPECTED_FREEZE
    freeze = json.loads(FIXTURE.read_text())
    assert set(freeze["operations"]) == EXPECTED_OPERATIONS
    assert len(freeze["required_histories"]) == 21
    assert freeze["retention"]["floor_changes_only_on_committed_pruning"] is True
    assert freeze["expiry_materialization"]["advances_recovery_floor"] is False
    assert freeze["compatibility"]["preexisting_operation_descriptor_bytes_unchanged"] is True
    test_tree = ast.parse((ROOT / "tests/test_maintenance_resource.py").read_text())
    histories = next(node.value for node in test_tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "HISTORIES" for target in node.targets))
    assert set(ast.literal_eval(histories)) == set(freeze["required_histories"])
    exercise = next(node for node in test_tree.body if isinstance(node, ast.FunctionDef) and node.name == "exercise_frozen_history")
    semantic_branches = {}
    for node in ast.walk(exercise):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.comparators) == 1 and isinstance(node.test.comparators[0], ast.Constant) and isinstance(node.test.comparators[0].value, str):
            name = node.test.comparators[0].value
            semantic_branches[name] = any(isinstance(child, (ast.Assert, ast.With)) for statement in node.body for child in ast.walk(statement))
    transactional = next(node.value for node in test_tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "TRANSACTIONAL_HISTORIES" for target in node.targets))
    transactional_names = set(ast.literal_eval(transactional.args[0]))
    assert set(semantic_branches) == set(freeze["required_histories"]) - transactional_names
    assert all(semantic_branches.values())
    required_falsifiers = {
        "cursor-below-floor": ("threading.Thread", "CursorExpired", "sprintctl-maintenance-event-1"),
        "disconnect": (".close()", 'owner.prepare(**arguments)', 'duplicate'),
        "parallel-owner-decoders": ("threading.Thread", "decoded == expected", "after == before"),
        "prepare-response-loss": ('owner.prepare(**arguments)', 'reference_envelope', 'duplicate'),
        "expiry-materialization-authorized-write": ("sweep_expired", 'state"] == "expired"', 'cursor"].endswith("-2")'),
        "restart-during-wait": ("threading.Event", "changes(", "interrupted == [True]"),
    }
    for relative, function_name in (
        ("tests/test_maintenance_resource.py", "exercise_sqlite_transactional_history"),
        ("tests/test_work_application_pg.py", "exercise_postgres_transactional_history"),
    ):
        source_text = (ROOT / relative).read_text()
        tree = ast.parse(source_text)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
        function_text = ast.get_source_segment(source_text, function)
        assert function_text is not None
        assert sum(isinstance(node, ast.Assert) for node in ast.walk(function)) >= 10
        for history, tokens in required_falsifiers.items():
            branch = next(
                node for node in ast.walk(function)
                if isinstance(node, ast.If) and history in ast.unparse(node.test)
            )
            branch_text = "\n".join(ast.get_source_segment(source_text, statement) or "" for statement in branch.body)
            assert sum(isinstance(node, ast.Assert) for statement in branch.body for node in ast.walk(statement)) >= 2
            assert all(token in branch_text for token in tokens), (relative, history)
    postgres_source = (ROOT / "tests/test_work_application_pg.py").read_text()
    for token in ("non-disclosure-four-way", '"malformed"', '"absent"', '"foreign"', '"unauthorized"', "NOT_FOUND_PAYLOAD", "local_resource.visible"):
        assert token in postgres_source
    source = (ROOT / "sprintctl/vuoro_adapter.py").read_text()
    for operation in EXPECTED_OPERATIONS:
        assert source.count(f'"{operation}"') >= 1
    claim = json.loads((ROOT / "verification/claims/2130-owner-claim.json").read_text())
    assert claim["claim_id"] == 463 and claim["claim_type"] == "execute"
    assert claim["claim_token_recorded"] is False
    result = json.loads((ROOT / "verification/results/maintenance-resource-owner-item-2130.json").read_text())
    assert result["evidence"]["candidate_paths"] == list(CANDIDATES)
    assert result["evidence"]["candidate_digest"] == candidate_digest()
    print("maintenance-resource owner evidence: valid")


if __name__ == "__main__":
    main()
