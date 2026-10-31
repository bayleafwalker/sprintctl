"""Closed readonly proposal export; source assertions never acceptance authority.

The first version exports three-envelope inputs only. No native proposal-bound
verification selector exists yet, so required verification always refuses.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import effect_intent, releases

SCHEMA = "sprintctl-effect-proposal-export/v2"
MAX_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
CONTRACT_KEYS = {"review_required", "verification_profile", "evidence_obligations",
                 "effect_verification_required", "effect_check_revisions"}


class ExportRefused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require(ok: bool, code: str = "proposal-export-invalid") -> None:
    if not ok:
        raise ExportRefused(code)


def _strict(value: Any, depth: int = 0) -> None:
    _require(depth <= 32)
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        _require(abs(value) <= MAX_SAFE_INTEGER)
    elif type(value) is str:
        _require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value))
    elif type(value) is list:
        for item in value:
            _strict(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str and all(ord(c) <= 0xFFFF for c in key))
            _strict(key, depth + 1)
            _strict(item, depth + 1)
    else:
        raise ExportRefused("proposal-export-invalid")


def _canonical(value: Any) -> bytes:
    _strict(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    _require(len(encoded) <= MAX_BYTES, "proposal-export-size-bound")
    return encoded


def _text(value: Any, maximum: int = 200, *, empty: bool = False) -> None:
    _require(type(value) is str and (empty or bool(value)) and len(value) <= maximum)


def export_proposal(backend, store, intent_id: str, revision: int,
                    workspace_id: str, *, version: int = 2) -> dict[str, Any]:
    """One PostgreSQL readonly repeatable snapshot; no writes or arbitrary runs."""
    try:
        _require(version in (2, 3))
        with backend.repeatable_read_snapshot(store) as snapshot:
            if version == 3:
                from .effect_verification_export_index import inspect_catalog
                with snapshot.conn.cursor() as cur:
                    cur.execute("SELECT version FROM schema_version")
                    versions = cur.fetchall()
                    _require(len(versions) == 1 and versions[0]["version"] in (24, 25, 26, 27, 28, 29, 30),
                             "proposal-verification-index-unavailable")
                    index = inspect_catalog(cur)
                _require(index["available"] and index["complete"], "proposal-verification-index-unavailable")
            intent = backend.get_effect_intent(snapshot, intent_id)
            _require(intent is not None, "proposal-export-unavailable")
            _require(intent["state"] == "proposed" and intent["revision"] == revision,
                     "proposal-export-stale")
            run = backend.get_run(snapshot, intent["run_id"])
            _require(run is not None and run["workspace_id"] == workspace_id
                     and run["principal_id"] == intent["proposer_principal"],
                     "proposal-export-unavailable")
            item, current_revision, release = backend.effect_proposal_release_basis(
                snapshot, intent["item_id"]
            )
            _require(release is not None and intent.get("release_digest") == release["release_digest"]
                     and release["item_revision"] == current_revision,
                     "proposal-export-release-stale")
            _require(release["work_item_id"] == intent["item_id"])
            contract = release["acceptance_contract"]
            _require(type(contract) is dict and set(contract) <= CONTRACT_KEYS,
                     "proposal-export-unsupported-contract")
            _require(releases.normalize_acceptance_contract(contract) == contract)
            if "review_required" in contract:
                _require(type(contract["review_required"]) is bool)
            obligations = contract.get("evidence_obligations", [])
            _require(len(obligations) <= 64 and all(len(label) <= 200 for label in obligations))
            if version == 2:
                _require(not contract.get("effect_verification_required", False)
                         and "effect_check_revisions" not in contract,
                         "proposal-verification-selection-unavailable")
            refs = release["context_refs"]
            _require(type(refs) is list and len(refs) <= 128)
            _require(all(type(ref) is dict and set(ref) == {"ref_type", "url", "label"}
                         and all(type(v) is str for v in ref.values()) for ref in refs))
            _require(refs == releases.canonical_context_refs(refs))
            aggregate_uuid = item["aggregate_uuid"].lower()
            _require(re.fullmatch(r"[0-9a-f-]{36}", aggregate_uuid) is not None)
            edit_revision = current_revision.rsplit("@revise:", 1)[0]
            _require(releases.validate_basis(edit_revision) == edit_revision)
            _require(edit_revision.split("@")[0][5:].lower() == aggregate_uuid)
            _require(releases.release_digest(aggregate_uuid, current_revision, contract, refs)
                     == release["release_digest"])
            _require(effect_intent.canonical_intent_digest(intent) == intent["canonical_intent_digest"])
            for key, maximum in (("repository", 200), ("title", 200), ("rationale", 8000),
                                 ("unified_diff", 1_000_000)):
                _text(intent[key], maximum, empty=key != "repository")
            _require(type(intent["base_commit"]) is str
                     and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", intent["base_commit"]) is not None)
            _require(type(intent["item_id"]) is int and 1 <= intent["item_id"] <= MAX_SAFE_INTEGER)
            for value in (store.repo_id, workspace_id, intent_id, run["run_id"], run["principal_id"]):
                _text(value)
            for key in ("client_id", "grant_id"):
                if run[key] is not None:
                    _text(run[key])
            result = {
                "schema": SCHEMA, "repo_id": store.repo_id, "workspace_id": workspace_id,
                "intent": {key: intent[key] for key in (
                    "intent_id", "revision", "canonical_intent_digest", "release_digest",
                    "item_id", "repository", "base_commit", "title", "rationale", "unified_diff", "run_id")},
                "run": {key: run[key] for key in (
                    "run_id", "principal_id", "workspace_id", "client_id", "grant_id")},
                "work": {"work_item_id": intent["item_id"], "aggregate_uuid": aggregate_uuid,
                         "edit_revision": edit_revision, "status_revision": backend.item_status_revision(item)},
                "release": {key: release[key] for key in (
                    "work_item_id", "item_revision", "release_digest", "acceptance_contract", "context_refs")},
            }
            if version == 3:
                from .effect_verification_export import SCHEMA as schema_v3, project_rows
                rows = backend.effect_proposal_verification_rows(snapshot, intent_id, workspace_id)
                result["schema"] = schema_v3
                result["verification_evidence"] = project_rows(backend, rows, intent, release, workspace_id)
            # Native subset is <=2MiB; protected projection and transport enforce
            # their own exact 2MiB canonical/envelope and 4MiB raw HTTP budgets.
            _canonical(result)
            return result
    except ExportRefused:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ExportRefused("proposal-export-invalid") from None


def _obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1, "maxLength": 200}
_INT = {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER}
_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_CONTRACT_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "review_required": {"type": "boolean"},
    "verification_profile": {"enum": ["checked", "self-reported"]},
    "evidence_obligations": {"type": "array", "maxItems": 64,
                             "items": {"type": "string", "minLength": 1, "maxLength": 200}},
    "effect_verification_required": {"const": False},
}}
RESULT_SCHEMA = _obj({
    "schema": {"const": SCHEMA}, "repo_id": _TEXT, "workspace_id": _TEXT,
    "intent": _obj({"intent_id": _TEXT, "revision": _INT,
        "canonical_intent_digest": _SHA, "release_digest": _SHA, "item_id": _INT,
        "repository": _TEXT, "base_commit": {"type": "string", "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"},
        "title": {"type": "string", "maxLength": 200},
        "rationale": {"type": "string", "maxLength": 8000},
        "unified_diff": {"type": "string", "maxLength": 1_000_000}, "run_id": _TEXT}),
    "run": _obj({"run_id": _TEXT, "principal_id": _TEXT, "workspace_id": _TEXT,
                 "client_id": {"anyOf": [_TEXT, {"type": "null"}]},
                 "grant_id": {"anyOf": [_TEXT, {"type": "null"}]}}),
    "work": _obj({"work_item_id": _INT,
        "aggregate_uuid": {"type": "string", "pattern": "^[0-9a-f-]{36}$"},
        "edit_revision": {"type": "string", "pattern": r"^item:[0-9a-fA-F-]{36}@description:v[0-9]+@sha256:[0-9a-f]{64}$"},
        "status_revision": _TEXT}),
    "release": _obj({"work_item_id": _INT,
        "item_revision": {"type": "string", "pattern": r"^item:[0-9a-fA-F-]{36}@description:v[0-9]+@sha256:[0-9a-f]{64}@revise:[0-9]+$"},
        "release_digest": _SHA, "acceptance_contract": _CONTRACT_SCHEMA,
        "context_refs": {"type": "array", "maxItems": 128, "items": _obj({
            "ref_type": {"type": "string"}, "url": {"type": "string"}, "label": {"type": "string"}})}}),
})

RESULT_SCHEMA["$schema"] = "https://json-schema.org/draft/2020-12/schema"
