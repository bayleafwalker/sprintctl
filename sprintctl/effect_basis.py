"""Readonly public-source corroboration; protected copies alone carry approval."""
from __future__ import annotations

from copy import deepcopy
import re

from . import effect_intent, releases
from .effect_intent_export import (_canonical, _require, _text, ExportRefused,
                                   CONTRACT_KEYS, MAX_SAFE_INTEGER)
from .effect_verification_export import RESULT_SCHEMA_V3

SCHEMA = "sprintctl-effect-basis/v1"
_STATES = ("pending", "active", "blocked", "done")


def _full_revision(value, aggregate_uuid):
    _require(type(value) is str and re.fullmatch(
        r"item:[0-9a-fA-F-]{36}@description:v[0-9]+@sha256:[0-9a-f]{64}@revise:[0-9]+", value))
    edit, revise = value.rsplit("@revise:", 1)
    _require(int(revise) <= MAX_SAFE_INTEGER)
    _require(releases.validate_basis(edit) == edit)
    _require(edit.split("@")[0][5:].lower() == aggregate_uuid)
    return edit


def read_basis(backend, store, intent_id, revision, workspace_id):
    try:
        with backend.repeatable_read_snapshot(store) as snapshot:
            intent = backend.get_effect_intent(snapshot, intent_id)
            _require(intent is not None, "effect-basis-unavailable")
            _require(intent["revision"] == revision, "effect-basis-stale")
            _require(intent["state"] in effect_intent.EFFECT_STATES)
            run = backend.get_run(snapshot, intent["run_id"])
            _require(run is not None and run["workspace_id"] == workspace_id
                     and run["principal_id"] == intent["proposer_principal"], "effect-basis-unavailable")
            _require(effect_intent.canonical_intent_digest(intent) == intent["canonical_intent_digest"])
            _require(type(intent["release_digest"]) is str
                     and re.fullmatch(r"[0-9a-f]{64}", intent["release_digest"]))
            for key, maximum in (("repository", 200), ("title", 200),
                                 ("rationale", 8000), ("unified_diff", 1_000_000)):
                _text(intent[key], maximum, empty=key != "repository")
            _require(type(intent["base_commit"]) is str
                     and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", intent["base_commit"]))
            _require(type(intent["item_id"]) is int and 1 <= intent["item_id"] <= MAX_SAFE_INTEGER)
            for value in (store.repo_id, workspace_id, intent_id, run["run_id"], run["principal_id"]):
                _text(value)
            for key in ("client_id", "grant_id"):
                if run[key] is not None:
                    _text(run[key])
            item, current_revision, release = backend.effect_proposal_release_basis(snapshot, intent["item_id"])
            _require(item["id"] == intent["item_id"] and item["status"] in _STATES)
            aggregate_uuid = item["aggregate_uuid"].lower()
            _require(re.fullmatch(r"[0-9a-f-]{36}", aggregate_uuid))
            edit_revision = _full_revision(current_revision, aggregate_uuid)
            status_revision = backend.item_status_revision(item)
            _text(status_revision)
            selected_release = None
            if release is not None:
                _require(release["work_item_id"] == intent["item_id"])
                _full_revision(release["item_revision"], aggregate_uuid)
                contract = release["acceptance_contract"]
                _require(type(contract) is dict and set(contract) <= CONTRACT_KEYS)
                _require(releases.normalize_acceptance_contract(contract) == contract)
                if "review_required" in contract:
                    _require(type(contract["review_required"]) is bool)
                obligations = contract.get("evidence_obligations", [])
                _require(len(obligations) <= 64 and all(len(label) <= 200 for label in obligations))
                refs = release["context_refs"]
                _require(type(refs) is list and len(refs) <= 128)
                _require(all(type(ref) is dict and set(ref) == {"ref_type", "url", "label"}
                             and all(type(v) is str for v in ref.values()) for ref in refs))
                _require(refs == releases.canonical_context_refs(refs))
                _require(releases.release_digest(aggregate_uuid, release["item_revision"], contract, refs)
                         == release["release_digest"])
                selected_release = {key: release[key] for key in (
                    "work_item_id", "item_revision", "release_digest", "acceptance_contract", "context_refs")}
            result = {
                "schema": SCHEMA, "repo_id": store.repo_id, "workspace_id": workspace_id,
                "intent": {key: intent[key] for key in (
                    "intent_id", "revision", "state", "canonical_intent_digest", "release_digest", "item_id",
                    "repository", "base_commit", "title", "rationale", "unified_diff", "run_id")},
                "run": {key: run[key] for key in ("run_id", "principal_id", "workspace_id", "client_id", "grant_id")},
                "work": {"work_item_id": intent["item_id"], "aggregate_uuid": aggregate_uuid,
                         "status": item["status"], "edit_revision": edit_revision,
                         "status_revision": status_revision, "item_revision": current_revision},
                "release": selected_release,
            }
            _canonical(result)
            return result
    except ExportRefused as exc:
        if exc.code in ("effect-basis-unavailable", "effect-basis-stale"):
            raise
        code = "effect-basis-size-bound" if exc.code == "proposal-export-size-bound" else "effect-basis-invalid"
        raise ExportRefused(code) from None
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ExportRefused("effect-basis-invalid") from None


RESULT_SCHEMA = deepcopy(RESULT_SCHEMA_V3)
RESULT_SCHEMA["properties"].pop("verification_evidence")
RESULT_SCHEMA["required"].remove("verification_evidence")
RESULT_SCHEMA["properties"]["schema"] = {"const": SCHEMA}
intent_schema = RESULT_SCHEMA["properties"]["intent"]
intent_schema["properties"]["state"] = {"enum": list(effect_intent.EFFECT_STATES)}
intent_schema["required"].append("state")
work_schema = RESULT_SCHEMA["properties"]["work"]
work_schema["properties"]["status"] = {"enum": list(_STATES)}
work_schema["properties"]["item_revision"] = deepcopy(RESULT_SCHEMA["properties"]["release"]["properties"]["item_revision"])
work_schema["required"].extend(("status", "item_revision"))
RESULT_SCHEMA["properties"]["release"] = {"anyOf": [RESULT_SCHEMA["properties"]["release"], {"type": "null"}]}
