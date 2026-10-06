"""Protected receipt contract for raw UTF-8 diff artifacts, not provider verdicts.

Owner storage binds the authenticated receipt writer and current Release. This
module only validates the receipt and hashes its content / actual patch bytes.
Neither importing a provider observation nor passing a check grants authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

SCHEMA = "sprintctl-protected-artifact-verification/v1"
KIND = "protected-artifact-verification"
ARTIFACT_DOMAIN = "utf8-unified-diff/v1"
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")


def artifact_digest(unified_diff: str) -> str:
    return "sha256:" + hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()


def receipt_digest(detail: Mapping[str, Any]) -> str:
    encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_receipt(item: Mapping[str, Any], intent: Mapping[str, Any], release_digest: str) -> dict:
    """Return the exact checked detail, or refuse without inferring any link.

    The caller must first establish receipt ownership from native run storage,
    not from a principal field in this untrusted detail. Checks are assertions
    of that protected actor, not cryptographic execution attestation.
    """
    claims = item.get("claims")
    if item.get("kind") != KIND or not isinstance(claims, list) or len(claims) != 1:
        raise ValueError("a protected verification receipt is required")
    claim = claims[0]
    detail = claim.get("detail") if isinstance(claim, dict) else None
    fields = {"schema", "intent_id", "intent_revision", "canonical_intent_digest", "release_digest", "artifact", "checks"}
    if not isinstance(detail, dict) or set(detail) != fields or detail["schema"] != SCHEMA:
        raise ValueError("unsupported protected verification receipt")
    if (claim.get("claim_type") != "observation" or claim.get("subject") != intent["intent_id"]
            or claim.get("grant_id") is not None or claim.get("confirms") is not None):
        raise ValueError("verification evidence is an observation, never an authority grant")
    if (detail["intent_id"] != intent["intent_id"]
            or type(detail["intent_revision"]) is not int or detail["intent_revision"] != intent["revision"]
            or detail["canonical_intent_digest"] != intent["canonical_intent_digest"]
            or detail["release_digest"] != release_digest):
        raise ValueError("verification does not bind this exact intent and Release")
    artifact = detail["artifact"]
    if (not isinstance(artifact, dict) or set(artifact) != {"domain", "digest"}
            or artifact["domain"] != ARTIFACT_DOMAIN
            or artifact["digest"] != artifact_digest(intent["unified_diff"])):
        raise ValueError("verification artifact differs from the actual UTF-8 patch bytes")
    checks = detail["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
        raise ValueError("protected verification requires bounded check results")
    names = set()
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"name", "revision", "status"}
                or not isinstance(check["name"], str) or not check["name"].strip()
                or len(check["name"]) > 200 or check["name"] in names
                or not isinstance(check["revision"], str) or not _SHA.fullmatch(check["revision"])
                or check["status"] != "passed"):
            raise ValueError("every named, revision-bound protected check must pass")
        names.add(check["name"])
    if item.get("digest") != receipt_digest(detail):
        raise ValueError("verification receipt content differs from its recorded digest")
    return detail
