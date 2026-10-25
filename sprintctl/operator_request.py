"""Wire validation for the Vuoro operator-request/v1 contract.

This module deliberately contains no request state or work-item status
transitions. The durable owner stores requests in a separate lifecycle family;
the packet itself is untrusted instruction data and never grants authority.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from collections.abc import Mapping
from typing import Any


OPERATION_PUBLISH = "work.request.publish-v1"
OPERATION_GET = "work.request.get-v1"
OPERATION_LIST = "work.request.list-v1"
OPERATION_EVENTS = "work.request.events-v1"
OPERATION_REVISE = "work.request.revise-v1"
OPERATION_DEPENDENT_ADD = "work.request.dependent-add-v1"
OPERATION_DEPENDENT_WITHDRAW = "work.request.dependent-withdraw-v1"
OPERATION_PROGRESS = "work.request.progress-v1"
OPERATION_REPORT_PERFORMED = "work.request.report-performed-v1"
OPERATION_ATTEST = "work.request.attest-v1"
OPERATION_DECLINE = "work.request.decline-v1"
OPERATION_SUPERSEDE = "work.request.supersede-v1"

WRITE_OPERATIONS = frozenset({
    OPERATION_PUBLISH,
    OPERATION_REVISE,
    OPERATION_DEPENDENT_ADD,
    OPERATION_DEPENDENT_WITHDRAW,
    OPERATION_PROGRESS,
    OPERATION_REPORT_PERFORMED,
    OPERATION_ATTEST,
    OPERATION_DECLINE,
    OPERATION_SUPERSEDE,
})
READ_OPERATIONS = frozenset({OPERATION_GET, OPERATION_LIST, OPERATION_EVENTS})
OPERATIONS = WRITE_OPERATIONS | READ_OPERATIONS

REQUEST_ID_RE = re.compile(r"^req_[0-9A-HJKMNP-TV-Z]{26}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_RFC3339_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")
_KINDS = frozenset({"perform", "authorize", "decide", "provide-information"})
_STATES = frozenset({"open", "reported-performed", "verifying", "resolved", "declined", "withdrawn", "superseded"})
_SENSITIVE_KEYS = frozenset({
    "actor", "principal", "principal_id", "workspace", "workspace_id", "repo", "repo_id",
    "authority", "authorities", "capability", "token", "access_token", "secret", "password",
    "credential", "transcript", "raw_transcript", "raw_log", "session_log",
})
MAX_PACKET_BYTES = 64 * 1024
MAX_DEPENDANTS = 128
MAX_STEPS = 64
MAX_SOURCE_REVISIONS = 32
MAX_INPUTS = 16
MAX_SAFE_INTEGER = 2**53 - 1
MAX_REVISION = 2**31 - 1


class RequestRefused(ValueError):
    """A stable operator-request contract refusal."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details) if details is not None else None

    @property
    def http_status(self) -> int:
        return {
            "invalid-arguments": 422,
            "payload-too-large": 413,
            "not-found": 404,
            "work-not-found": 404,
            "requester-not-owner": 403,
            "request-id-conflict": 409,
            "revision-conflict": 409,
            "request-not-open": 409,
            "dependant-not-active": 409,
            "dependant-already-withdrawn": 409,
            "idempotency-conflict": 409,
            "revision-exhausted": 409,
            "request-not-verifiable": 409,
            "no-active-dependants": 409,
            "unknown-procedure-step": 422,
        }.get(self.code, 409)


def _invalid(message: str) -> None:
    raise RequestRefused("invalid-arguments", message)


def _object(value: Any, name: str, *, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, Mapping):
        _invalid(f"{name} must be an object")
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required - optional)
    if missing:
        _invalid(f"{name} lacks {', '.join(missing)}")
    if extra:
        _invalid(f"{name} does not accept {', '.join(extra)}")
    return dict(value)


def _text(value: Any, name: str, *, minimum: int = 1, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or "\x00" in value:
        _invalid(f"{name} must be text of {minimum} to {maximum} characters without NUL")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _invalid(f"{name} must be valid UTF-8 text")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        _invalid(f"{name} must be an RFC 3339 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _invalid(f"{name} is not a valid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid(f"{name} must include a timezone")
    return parsed


def _sensitive_key_check(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _invalid("packet object keys must be strings")
            normalized = key.casefold().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                _invalid(f"packet field {key!r} is not allowed in durable request content")
            _sensitive_key_check(child)
    elif isinstance(value, list):
        for child in value:
            _sensitive_key_check(child)


def validate_request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
        _invalid("request_id must be a req_ ULID")
    return value


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_RE.fullmatch(value):
        _invalid("idempotency_key must be 8-128 characters of A-Z a-z 0-9 . _ : -")
    return value


def validate_revision(value: Any, name: str = "expected_revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_REVISION:
        _invalid(f"{name} must be a positive int32")
    return value


def validate_packet(value: Any) -> dict:
    """Validate and canonicalize a complete v1 instruction packet."""
    packet = _object(value, "packet", required={
        "schema", "kind", "required_outcome", "reason", "execution", "procedure", "blocker",
    })
    if packet["schema"] != "operator-request/v1":
        _invalid("packet.schema must be operator-request/v1")
    if not isinstance(packet["kind"], str) or packet["kind"] not in _KINDS:
        _invalid("packet.kind is not a supported request kind")
    _text(packet["required_outcome"], "packet.required_outcome")
    _text(packet["reason"], "packet.reason")

    execution = _object(packet["execution"], "packet.execution", required={
        "host", "directory", "intended_identity", "target", "inputs",
    })
    for field in ("host", "directory", "intended_identity", "target"):
        _text(execution[field], f"packet.execution.{field}", maximum=1024)
    inputs = execution["inputs"]
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        _invalid(f"packet.execution.inputs must be an array of at most {MAX_INPUTS}")
    for index, raw in enumerate(inputs):
        entry = _object(raw, f"packet.execution.inputs[{index}]", required={"name", "acquire_from"})
        _text(entry["name"], f"packet.execution.inputs[{index}].name", maximum=256)
        _text(entry["acquire_from"], f"packet.execution.inputs[{index}].acquire_from", maximum=1024)

    procedure = _object(packet["procedure"], "packet.procedure", required={"version", "steps"})
    _text(procedure["version"], "packet.procedure.version", maximum=128)
    steps = procedure["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        _invalid(f"packet.procedure.steps must contain 1-{MAX_STEPS} steps")
    seen_step_ids: set[str] = set()
    for index, raw in enumerate(steps):
        prefix = f"packet.procedure.steps[{index}]"
        step = _object(raw, prefix, required={
            "step_id", "instruction", "expected_result", "preconditions", "verification",
            "partial_progress", "recovery",
        })
        step_id = _text(step["step_id"], f"{prefix}.step_id", maximum=128)
        if step_id in seen_step_ids:
            _invalid("procedure step_id values must be unique")
        seen_step_ids.add(step_id)
        for field in ("instruction", "expected_result", "verification", "partial_progress", "recovery"):
            _text(step[field], f"{prefix}.{field}", maximum=4096)
        preconditions = step["preconditions"]
        if not isinstance(preconditions, list) or len(preconditions) > 32:
            _invalid(f"{prefix}.preconditions must be an array of at most 32 entries")
        for check_index, condition in enumerate(preconditions):
            _text(condition, f"{prefix}.preconditions[{check_index}]", maximum=1024)

    blocker = _object(packet["blocker"], "packet.blocker", required={
        "condition", "checked_at", "fresh_until", "source_revisions",
    })
    _text(blocker["condition"], "packet.blocker.condition")
    checked_at = _timestamp(blocker["checked_at"], "packet.blocker.checked_at")
    fresh_until = _timestamp(blocker["fresh_until"], "packet.blocker.fresh_until")
    if fresh_until <= checked_at:
        _invalid("packet.blocker.fresh_until must be later than checked_at")
    revisions = blocker["source_revisions"]
    if not isinstance(revisions, list) or len(revisions) > MAX_SOURCE_REVISIONS:
        _invalid(f"packet.blocker.source_revisions must be an array of at most {MAX_SOURCE_REVISIONS}")
    seen_sources: set[str] = set()
    for index, raw in enumerate(revisions):
        revision = _object(raw, f"packet.blocker.source_revisions[{index}]", required={"source", "revision"})
        source = _text(revision["source"], f"packet.blocker.source_revisions[{index}].source", maximum=1024)
        _text(revision["revision"], f"packet.blocker.source_revisions[{index}].revision", maximum=512)
        if source in seen_sources:
            _invalid("source revision source values must be unique")
        seen_sources.add(source)

    _sensitive_key_check(packet)
    try:
        encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             allow_nan=False).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        _invalid("packet must be valid JSON")
    if len(encoded) > MAX_PACKET_BYTES:
        raise RequestRefused("payload-too-large", f"packet exceeds {MAX_PACKET_BYTES} bytes")
    return json.loads(encoded)


def validate_dependant(value: Any) -> dict:
    dependant = _object(value, "dependant", required={"work_item_id", "session_id"}, optional={"runtime_id"})
    item_id = dependant["work_item_id"]
    if isinstance(item_id, bool) or not isinstance(item_id, int) or not 1 <= item_id <= MAX_SAFE_INTEGER:
        _invalid("dependant.work_item_id must be a positive safe integer")
    _text(dependant["session_id"], "dependant.session_id", maximum=256)
    if "runtime_id" in dependant:
        _text(dependant["runtime_id"], "dependant.runtime_id", maximum=256)
    return dependant


def validate_dependants(value: Any) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DEPENDANTS:
        _invalid(f"dependants must contain 1-{MAX_DEPENDANTS} entries")
    parsed = [validate_dependant(entry) for entry in value]
    keys = [(entry["work_item_id"], entry["session_id"]) for entry in parsed]
    if len(keys) != len(set(keys)):
        _invalid("dependants must have unique work_item_id and session_id pairs")
    return parsed


def publish_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={"request_id", "packet", "dependants", "idempotency_key"})
    return {
        "request_id": validate_request_id(args["request_id"]),
        "packet": validate_packet(args["packet"]),
        "dependants": validate_dependants(args["dependants"]),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def revise_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={"request_id", "expected_revision", "packet", "idempotency_key"})
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        "packet": validate_packet(args["packet"]),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def dependant_add_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "dependant", "idempotency_key",
    })
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        "dependant": validate_dependant(args["dependant"]),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def dependant_withdraw_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "work_item_id", "session_id", "idempotency_key",
    })
    dependant = validate_dependant({"work_item_id": args["work_item_id"], "session_id": args["session_id"]})
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        **dependant,
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def progress_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "work_item_id", "session_id", "step_id", "status", "note",
        "idempotency_key",
    })
    dependent = validate_dependant({"work_item_id": args["work_item_id"], "session_id": args["session_id"]})
    if not isinstance(args["status"], str) or args["status"] not in {
        "in-progress", "completed", "blocked", "uncertain",
    }:
        _invalid("status must be in-progress, completed, blocked or uncertain")
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        **dependent,
        "step_id": _text(args["step_id"], "step_id", maximum=128),
        "status": args["status"],
        "note": _text(args["note"], "note", maximum=2048),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def report_performed_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "outcome", "note", "idempotency_key",
    })
    outcome = args["outcome"]
    if not isinstance(outcome, str) or outcome not in {"performed", "partial", "uncertain"}:
        _invalid("outcome must be performed, partial or uncertain")
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        "outcome": outcome,
        "note": _text(args["note"], "note", maximum=2048),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def attest_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "statement", "performed_at", "idempotency_key",
    })
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        "statement": _text(args["statement"], "statement", maximum=2048),
        "performed_at": _timestamp(args["performed_at"], "performed_at").isoformat(),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def decline_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "reason", "idempotency_key",
    })
    return {
        "request_id": validate_request_id(args["request_id"]),
        "expected_revision": validate_revision(args["expected_revision"]),
        "reason": _text(args["reason"], "reason", maximum=2048),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def supersede_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={
        "request_id", "expected_revision", "replacement_request_id", "packet", "idempotency_key",
    })
    request_id = validate_request_id(args["request_id"])
    replacement_id = validate_request_id(args["replacement_request_id"])
    if request_id == replacement_id:
        _invalid("replacement_request_id must differ from request_id")
    return {
        "request_id": request_id,
        "expected_revision": validate_revision(args["expected_revision"]),
        "replacement_request_id": replacement_id,
        "packet": validate_packet(args["packet"]),
        "idempotency_key": validate_idempotency_key(args["idempotency_key"]),
    }


def get_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={"request_id"})
    return {"request_id": validate_request_id(args["request_id"])}


def events_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={"request_id", "after_revision", "limit"})
    after = args["after_revision"]
    limit = args["limit"]
    if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after <= MAX_REVISION:
        _invalid("after_revision must be a non-negative int32")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        _invalid("limit must be an integer from 1 to 100")
    return {"request_id": validate_request_id(args["request_id"]), "after_revision": after, "limit": limit}


def list_arguments(value: Any) -> dict:
    args = _object(value, "arguments", required={"limit"}, optional={"after_request_id", "state"})
    limit = args["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        _invalid("limit must be an integer from 1 to 100")
    result = {"limit": limit}
    if "after_request_id" in args:
        result["after_request_id"] = validate_request_id(args["after_request_id"])
    if "state" in args:
        if not isinstance(args["state"], str) or args["state"] not in _STATES:
            _invalid("state is not a known operator-request state")
        result["state"] = args["state"]
    return result
