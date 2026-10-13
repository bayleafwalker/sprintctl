"""All visible proposal-associated receipts, not a public acceptance authorizer."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy

from . import effect_verification
from .effect_intent_export import (_canonical, _require, _text, _strict, _obj, _TEXT,
                                   _INT, _SHA, ExportRefused, RESULT_SCHEMA)

SCHEMA = "sprintctl-effect-proposal-export/v3"
MAX_EVIDENCE = 29
_SHA_PREFIXED = re.compile(r"sha256:[0-9a-f]{64}")
QUERY = '''SELECT e.*, r.run_id AS verifier_run_id, r.principal_id AS verifier_principal,
       r.workspace_id AS verifier_workspace_id, r.client_id AS verifier_client_id,
       r.grant_id AS verifier_grant_id
FROM evidence_item e
LEFT JOIN run r ON r.repo_id=e.repo_id AND r.run_id=e.run_id
WHERE e.repo_id=%s AND (r.workspace_id=%s OR r.run_id IS NULL)
  AND e.kind='protected-artifact-verification'
  AND e.claims @> %s::jsonb
ORDER BY e.run_id COLLATE "C", e.chain_seq, e.item_id COLLATE "C"
LIMIT 30'''


def read_rows(store, intent_id, workspace_id):
    with store.conn.cursor() as cur:
        cur.execute(QUERY, (store.repo_id, workspace_id,
                           _canonical([{"subject": intent_id}]).decode("utf-8")))
        return cur.fetchall()


def project_rows(backend, rows, intent, release, workspace_id):
    _require(len(rows) <= MAX_EVIDENCE, "proposal-verification-overflow")
    required = (release["acceptance_contract"].get("effect_verification_required", False)
                or "effect_check_revisions" in release["acceptance_contract"])
    _require(not required or bool(rows), "proposal-verification-required")
    projected = []
    for row in rows:
        try:
            _require(row["verifier_run_id"] == row["run_id"]
                     and row["verifier_workspace_id"] == workspace_id)
            for name in ("run_id", "item_id", "verifier_principal", "verifier_workspace_id"):
                _text(row[name])
            for name in ("verifier_client_id", "verifier_grant_id"):
                if row[name] is not None:
                    _text(row[name])
            _require(type(row["chain_seq"]) is int and 0 <= row["chain_seq"] <= 2**53-1)
            prev = row["chain_prev_digest"]
            _require((row["chain_seq"] == 0) == (prev is None))
            _require(prev is None or (type(prev) is str and _SHA_PREFIXED.fullmatch(prev) is not None))
            _strict(row["claims"])
            detail = effect_verification.validate_receipt(
                {"kind": row["kind"], "claims": row["claims"], "digest": row["digest"]},
                intent, release["release_digest"],
                release["acceptance_contract"].get("effect_check_revisions"),
            )
            _text(detail["intent_id"])
            _canonical(detail)
            receipt_native = effect_verification.receipt_digest(detail)
            receipt_v5 = "sha256:" + hashlib.sha256(_canonical(detail)).hexdigest()
            entry = {"item_id": row["item_id"], "digest": row["digest"],
                     "chain_seq": row["chain_seq"], "chain_prev_digest": prev}
            entry_native = backend.evidence_entry_digest(entry)
            entry_v5 = "sha256:" + hashlib.sha256(_canonical(entry)).hexdigest()
            _require(receipt_native == receipt_v5 and entry_native == entry_v5,
                     "proposal-verification-hash-domain-unsupported")
            claim = row["claims"][0]
            projected.append({
                "run": {"run_id": row["run_id"], "principal_id": row["verifier_principal"],
                        "workspace_id": row["verifier_workspace_id"],
                        "client_id": row["verifier_client_id"], "grant_id": row["verifier_grant_id"]},
                "item": {key: row[key] for key in ("item_id", "kind", "digest", "chain_seq", "chain_prev_digest")},
                "native_claim": {key: claim.get(key) for key in ("claim_type", "subject", "grant_id", "confirms")},
                "receipt": detail, "entry_digest": entry_native,
            })
        except ExportRefused as exc:
            if exc.code == "proposal-verification-hash-domain-unsupported":
                raise
            raise ExportRefused("proposal-verification-candidate-invalid") from None
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ExportRefused("proposal-verification-candidate-invalid") from None
    return projected


_PREFIXED = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_NULL_TEXT = {"anyOf": [_TEXT, {"type": "null"}]}
_RECEIPT = _obj({"schema": {"const": effect_verification.SCHEMA}, "intent_id": _TEXT,
    "intent_revision": _INT, "canonical_intent_digest": _SHA, "release_digest": _SHA,
    "artifact": _obj({"domain": {"const": effect_verification.ARTIFACT_DOMAIN}, "digest": _PREFIXED}),
    "checks": {"type": "array", "minItems": 1, "maxItems": 64, "items": _obj({
        "name": {**_TEXT, "pattern": "\\S"}, "revision": _PREFIXED, "status": {"const": "passed"}})}})
ENTRY_SCHEMA = _obj({"run": RESULT_SCHEMA["properties"]["run"],
    "item": _obj({"item_id": _TEXT, "kind": {"const": effect_verification.KIND}, "digest": _PREFIXED,
        "chain_seq": {"type": "integer", "minimum": 0, "maximum": 2**53-1},
        "chain_prev_digest": {"anyOf": [_PREFIXED, {"type": "null"}]}}),
    "native_claim": _obj({"claim_type": {"const": "observation"}, "subject": _TEXT,
                           "grant_id": {"type": "null"}, "confirms": {"type": "null"}}),
    "receipt": _RECEIPT, "entry_digest": _PREFIXED})
RESULT_SCHEMA_V3 = deepcopy(RESULT_SCHEMA)
RESULT_SCHEMA_V3["properties"]["schema"] = {"const": SCHEMA}
RESULT_SCHEMA_V3["properties"]["verification_evidence"] = {"type": "array", "maxItems": MAX_EVIDENCE, "items": ENTRY_SCHEMA}
RESULT_SCHEMA_V3["required"].append("verification_evidence")
contract = RESULT_SCHEMA_V3["properties"]["release"]["properties"]["acceptance_contract"]["properties"]
contract["effect_verification_required"] = {"type": "boolean"}
contract["effect_check_revisions"] = {"type": "object", "minProperties": 1, "maxProperties": 64,
    "propertyNames": {**_TEXT, "pattern": "\\S"}, "additionalProperties": _PREFIXED}
