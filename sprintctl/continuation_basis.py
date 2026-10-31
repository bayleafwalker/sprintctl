"""One bounded continuation checkpoint shared by observation and settlement.

Null Decision/reference digests mean the population could not be read in full.
They never establish exact currentness for a mutation.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA = {
    "type": "object", "properties": {
        "repo_id": {"type": "string", "minLength": 1},
        "item_id": {"type": "integer", "minimum": 1},
        "work_edit_revision": {"type": "string", "minLength": 1},
        "status_revision": {"type": "string", "minLength": 1},
        "current_release_digest": {"anyOf": [{"type": "string", "pattern": "^[0-9a-f]{64}$"}, {"type": "null"}]},
        "current_decision_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
        "current_reference_set_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
    },
    "required": ["repo_id", "item_id", "work_edit_revision", "status_revision", "current_release_digest", "current_decision_digest", "current_reference_set_digest"],
    "additionalProperties": False,
}


def digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate(value: Any, repo_id: str, item_id: int) -> dict:
    if (type(value) is not dict or set(value) != set(SCHEMA["required"])
            or value.get("repo_id") != repo_id or type(value.get("item_id")) is not int
            or value["item_id"] < 1 or value["item_id"] != item_id
            or any(not isinstance(value.get(k), str) or not value[k]
                   for k in ("work_edit_revision", "status_revision"))
            or any(value.get(k) is not None and (not isinstance(value[k], str)
                   or re.fullmatch(pattern, value[k]) is None)
                   for k, pattern in (("current_release_digest", r"[0-9a-f]{64}"),
                                      ("current_decision_digest", r"sha256:[0-9a-f]{64}"),
                                      ("current_reference_set_digest", r"sha256:[0-9a-f]{64}")))):
        raise ValueError("checkpoint must match the complete owner checkpoint schema and repository/item")
    return value


def project(repo_id: str, item_id: int, edit_revision: str, status_revision: str,
            release: dict | None, facts: dict) -> dict:
    decisions = decision_facts(facts)
    references = facts["reference_digest_facts"]
    return {
        "repo_id": repo_id, "item_id": item_id, "work_edit_revision": edit_revision,
        "status_revision": status_revision,
        "current_release_digest": release["release_digest"] if release is not None else None,
        "current_decision_digest": digest(decisions) if facts["decisions_complete"] else None,
        "current_reference_set_digest": digest([{
            "id": row["id"], "ref_type": row["ref_type"], "url": row["url"],
            "label": row.get("label", ""),
        } for row in references]) if facts["references_complete"] else None,
    }


def decision_facts(facts: dict) -> list[dict]:
    return [{"id": row["id"], "kind": row["kind"],
                  "release_digest": row.get("release_digest"),
                  "evidence_digests": row.get("evidence_digests", [])}
                 for row in facts["decisions"]]


def complete(value: dict) -> bool:
    return all(value[k] is not None for k in ("current_decision_digest", "current_reference_set_digest"))
