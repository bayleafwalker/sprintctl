"""Owner-bound targets for authenticated cooperative provider attempts.

An authorization names a declared target; it is not a forge fence or proof
that the provider was invoked. Repository policy and protected-ref checks
remain with the reconciler and provider. A redeemed authorization alone
cannot prove execution, success, or subject-wide non-invocation.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .effect_intent import AUTHORITY_MARK_APPLIED
from . import releases

AUTHORITY = AUTHORITY_MARK_APPLIED
OPERATION_OPEN = "work.effect.attempt-open-v1"
OPERATION_REDEEM = "work.effect.attempt-redeem-v1"
OPERATION_SEAL_UNUSED = "work.effect.attempt-seal-unused-v1"
OPERATION_REPORT = "work.effect.attempt-report-applied-v1"
OPERATION_GET = "work.effect.attempt-get-v1"
OPERATION_AUTHORITIES = {operation: AUTHORITY for operation in (
    OPERATION_OPEN, OPERATION_REDEEM, OPERATION_SEAL_UNUSED, OPERATION_REPORT, OPERATION_GET,
)}
TARGET_SCHEMA = "sprintctl-effect-attempt-target/v1"
EVENT_KINDS = (
    "attempt_authorization_accepted",
    "invocation_authorization_redeemed",
    "attempt_closed_without_redemption",
    "application_report_received",
)
_INTENT = re.compile(r"intent_[0-9A-HJKMNP-TV-Z]{26}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")
_PUSH_FIELDS = frozenset({"operation", "branch", "commit_sha"})
_PR_FIELDS = _PUSH_FIELDS | {"base_branch"}
_ATTEMPT = re.compile(r"attempt_[0-9A-HJKMNP-TV-Z]{26}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
_OPEN_FIELDS = frozenset({"intent_id", "revision", "canonical_intent_digest",
                         "expected_revision", "release_digest", "target", "idempotency_key"})
_CONSUME_FIELDS = frozenset({"attempt_id", "authorization_digest", "idempotency_key"})
_REPORT_FIELDS = _CONSUME_FIELDS | {"commit_sha", "pr_url"}


def validate_arguments(operation: str, value: Any) -> dict[str, Any]:
    """Validate closed wire shapes before owner lookups or idempotency claims.

    Full targets require stored intent content and are bound separately.
    Neither identity nor delivery permission is an argument of any operation.
    """
    fields = {
        OPERATION_OPEN: _OPEN_FIELDS, OPERATION_REDEEM: _CONSUME_FIELDS,
        OPERATION_SEAL_UNUSED: _CONSUME_FIELDS, OPERATION_REPORT: _REPORT_FIELDS,
        OPERATION_GET: frozenset({"attempt_id"}),
    }.get(operation)
    if fields is None or not isinstance(value, dict) or set(value) != fields:
        raise ValueError("attempt arguments must match the exact operation shape")
    if operation == OPERATION_OPEN:
        if not isinstance(value["intent_id"], str) or not _INTENT.fullmatch(value["intent_id"]):
            raise ValueError("invalid intent identifier")
        if type(value["revision"]) is not int or value["revision"] < 1:
            raise ValueError("intent revision must be a positive integer")
        for field in ("canonical_intent_digest", "release_digest"):
            validate_digest(value[field], field)
        revision = releases.validate_basis(value["expected_revision"])
        if not releases._RELEASE_REVISION_RE.fullmatch(revision):
            raise ValueError("attempt requires the full observed owner Release revision")
        if not isinstance(value["target"], dict):
            raise ValueError("target must be an object")
    else:
        if not isinstance(value["attempt_id"], str) or not _ATTEMPT.fullmatch(value["attempt_id"]):
            raise ValueError("invalid attempt identifier")
        if operation != OPERATION_GET:
            validate_digest(value["authorization_digest"], "authorization_digest")
    if operation != OPERATION_GET:
        if not isinstance(value["idempotency_key"], str) or not _KEY.fullmatch(value["idempotency_key"]):
            raise ValueError("invalid native attempt idempotency key")
    if operation == OPERATION_REPORT:
        if not isinstance(value["commit_sha"], str) or not _COMMIT.fullmatch(value["commit_sha"]):
            raise ValueError("invalid reported Git commit")
        url = value["pr_url"]
        # This is an attributed report. Provider validation and independent
        # forge evidence establish which PR actually exists.
        if (not isinstance(url, str) or not re.fullmatch(r"https?://\S+", url) or len(url) > 2048
                or any(ord(c) <= 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in url)):
            raise ValueError("invalid reported pull request URL")
    return {**value, **({"target": dict(value["target"])} if operation == OPERATION_OPEN else {})}


def validate_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(field + " must be 64 lowercase hexadecimal characters")
    return value


def validate_branch(value: Any) -> str:
    """Accept an exact branch name, never normalize it into a different ref."""
    if (
        not isinstance(value, str) or not value or len(value) > 200
        or value == "@" or value.startswith(("-", "/"))
        or value.endswith(("/", ".")) or ".." in value or "@{" in value
        or "//" in value
        or any(ord(c) <= 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
               or c in "~^:?*[\\" for c in value)
        or any(part.startswith(".") or part.endswith(".lock")
               for part in value.split("/"))
    ):
        raise ValueError("target must name a valid exact branch")
    return value


def canonical_target(intent: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """Bind a closed provider target to stored intent content.

    The prepared commit is declared by the authenticated applier. This
    function neither computes it from a diff nor observes a remote head.
    Owner-derived fields cannot be supplied or overridden on the wire.
    """
    if not isinstance(value, dict):
        raise ValueError("target must be an object")
    operation = value.get("operation")
    if not isinstance(operation, str):
        raise ValueError("target operation must be a supported string")
    fields = {"push_branch": _PUSH_FIELDS, "open_pull_request": _PR_FIELDS}.get(operation)
    if fields is None or set(value) != fields:
        raise ValueError("target must match exactly one supported provider operation")
    intent_id = intent["intent_id"]
    if not isinstance(intent_id, str) or not _INTENT.fullmatch(intent_id):
        raise ValueError("invalid stored intent identifier")
    branch = validate_branch(value["branch"])
    prefix, separator, suffix = branch.rpartition("/")
    if not separator or not prefix or suffix != intent_id:
        raise ValueError("target branch must end in the exact intent identifier")
    commit = value["commit_sha"]
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("target commit must be a lowercase Git object identifier")
    target = {
        "operation": operation,
        "repository": intent["repository"],
        "branch": branch,
        "commit_sha": commit,
        "base_commit": intent["base_commit"],
        "title_sha256": hashlib.sha256(intent["title"].encode("utf-8")).hexdigest(),
        "body_sha256": hashlib.sha256(intent["rationale"].encode("utf-8")).hexdigest(),
    }
    if operation == "open_pull_request":
        base = validate_branch(value["base_branch"])
        if base == branch:
            raise ValueError("pull request base must differ from its head branch")
        target["base_branch"] = base
    return target


def canonical_target_digest(target: Mapping[str, Any]) -> str:
    """Versioned digest of the complete owner-derived target."""
    body = {"schema_version": TARGET_SCHEMA, "target": dict(target)}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
