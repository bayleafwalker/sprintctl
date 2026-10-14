"""Validators for the vendored agent directory and inbox v1 schemas.

tests/fixtures/agent-inbox-v1/ holds verbatim copies of vuoro's
docs/architecture/agent-*-v1.schema.json (contract v1 with the vuoro#219
launch-ambiguous amendment), so the owner's results are checked against the
contract's own schemas rather than against sprintctl's reading of them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).parent / "fixtures" / "agent-inbox-v1"
NAMES = ("declaration", "message", "message-receipt", "directory-entry")
_RFC3339 = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$")


def _validators() -> dict[str, Draft202012Validator]:
    schemas = {name: json.loads((SCHEMA_DIR / f"agent-{name}-v1.schema.json").read_text()) for name in NAMES}
    registry = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    formats = FormatChecker(formats=())
    formats.checks("date-time")(lambda value: not isinstance(value, str) or bool(_RFC3339.match(value)))
    return {name: Draft202012Validator(schema, registry=registry, format_checker=formats)
            for name, schema in schemas.items()}


VALIDATORS = _validators()
STAMPS = ("sender_principal_id", "workspace_id", "repo_id", "persisted_at", "envelope_digest", "inbox_seq")


def valid(name: str, value):
    VALIDATORS[name].validate(value)
    return value


def is_valid(name: str, value) -> bool:
    return VALIDATORS[name].is_valid(value)


def valid_stamped(stamped: dict) -> dict:
    envelope = {k: v for k, v in stamped.items() if k not in (*STAMPS, "receipts")}
    valid("message", envelope)
    for receipt in stamped["receipts"]:
        valid("message-receipt", receipt)
    return stamped
