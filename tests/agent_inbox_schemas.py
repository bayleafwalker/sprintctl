"""Validators for the vendored agent declaration v1/v2 and inbox v1 schemas.

tests/fixtures/agent-inbox-v1/ holds verbatim copies of vuoro's declaration
schemas and agent-*-v1 inbox schemas (with the vuoro#219 launch-ambiguous
amendment), so declarations and results are checked against the contract's own
schemas rather than against sprintctl's reading of them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).parent / "fixtures" / "agent-inbox-v1"
NAMES = ("declaration", "declaration-v2", "message", "message-receipt", "directory-entry")
_RFC3339 = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$")


def _validators() -> dict[str, Draft202012Validator]:
    paths = {name: f"agent-{name}-v1.schema.json" for name in NAMES}
    paths["declaration-v2"] = "agent-declaration-v2.schema.json"
    schemas = {name: json.loads((SCHEMA_DIR / filename).read_text()) for name, filename in paths.items()}
    registry = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    formats = FormatChecker(formats=())
    formats.checks("date-time")(lambda value: not isinstance(value, str) or bool(_RFC3339.match(value)))
    return {name: Draft202012Validator(schema, registry=registry, format_checker=formats)
            for name, schema in schemas.items()}


VALIDATORS = _validators()
STAMPS = ("sender_principal_id", "workspace_id", "repo_id", "persisted_at", "envelope_digest", "inbox_seq")


def valid(name: str, value):
    if name == "declaration" and isinstance(value, dict) and value.get("schema") == "agent-declaration/v2":
        name = "declaration-v2"
    VALIDATORS[name].validate(value)
    return value


def is_valid(name: str, value) -> bool:
    if name == "declaration" and isinstance(value, dict) and value.get("schema") == "agent-declaration/v2":
        name = "declaration-v2"
    return VALIDATORS[name].is_valid(value)


def valid_stamped(stamped: dict) -> dict:
    envelope = {k: v for k, v in stamped.items() if k not in (*STAMPS, "receipts")}
    valid("message", envelope)
    for receipt in stamped["receipts"]:
        valid("message-receipt", receipt)
    return stamped
