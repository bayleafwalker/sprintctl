"""Agent directory and durable inbox, contract v1: argument rules and codes.

Vuoro owns the contract (docs/architecture/agent-directory-and-inbox.md and
the agent-*-v1 JSON schemas); this module is sprintctl's store-side reading of
it.  Validation here is hand-written because jsonschema is a dev-only
dependency; tests/test_agent_inbox_contract.py checks it against vendored
copies of the schemas.  Storage lives in :mod:`sprintctl.agent_inbox_pg` and
the schema 25 catalog in :mod:`sprintctl.agent_inbox_schema`.

Nothing here assigns, schedules, retries or expires anything (TS-1): the
store records what principals declare, observe, send and decide.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping

OPERATION_REGISTER = "work.agent.register-v1"
OPERATION_OBSERVE = "work.agent.observe-v1"
OPERATION_RETIRE = "work.agent.retire-v1"
OPERATION_LIST = "work.agent.list-v1"
OPERATION_DESCRIBE = "work.agent.describe-v1"
OPERATION_SEND = "work.message.send-v1"
OPERATION_READ = "work.message.read-v1"
OPERATION_STATUS = "work.message.status-v1"
OPERATION_RECEIPT = "work.message.receipt-v1"
OPERATION_CANCEL = "work.message.cancel-v1"

WRITE_OPERATIONS = frozenset({
    OPERATION_REGISTER, OPERATION_OBSERVE, OPERATION_RETIRE, OPERATION_SEND,
    OPERATION_RECEIPT, OPERATION_CANCEL,
})
READ_OPERATIONS = frozenset({OPERATION_LIST, OPERATION_DESCRIBE, OPERATION_READ, OPERATION_STATUS})
OPERATIONS = WRITE_OPERATIONS | READ_OPERATIONS

#: The PostgreSQL schema that first stores these records.
SCHEMA_VERSION = 25

DECLARATION_SCHEMA = "agent-declaration/v1"
MESSAGE_SCHEMA = "agent-message/v1"
RECEIPT_SCHEMA = "agent-message-receipt/v1"
DIRECTORY_ENTRY_SCHEMA = "agent-directory-entry/v1"

MAX_ENVELOPE_BYTES = 64 * 1024
MAX_BODY_TEXT_BYTES = 16 * 1024
MAX_TEXT_BYTES = 2048
MIN_VALID_FOR_SECONDS = 1
MAX_VALID_FOR_SECONDS = 3600
MAX_READ_LIMIT = 100
SOURCE_SELF_REPORTED = "self-reported"

FACTS = ("observed", "reachable", "executing", "attended")
RECEIPT_KINDS = (
    "observed", "acknowledged", "accepted", "refused", "run-linked", "launch-ambiguous",
    "result-linked",
)
CANCEL_KIND = "cancel-requested"
DECISION_KINDS = frozenset({"accepted", "refused"})
OUTCOMES = ("completed", "failed", "canceled", "abandoned", "not-started")
RUN_OUTCOMES = frozenset({"completed", "failed", "canceled"})

#: Every code the store answers with (contract section 3.6), and its HTTP status.
HTTP_STATUS = {
    "not-found": 404,
    "invalid-arguments": 422,
    "idempotency-conflict": 409,
    "instance-not-owned": 403,
    "instance-retired": 409,
    "fact-not-applicable": 422,
    "recipient-unknown": 422,
    "mode-not-supported": 422,
    "context-not-found": 422,
    "payload-too-large": 413,
    "message-id-conflict": 409,
    "already-decided": 409,
    "not-accepted": 409,
    "run-not-found": 404,
    "run-already-linked": 409,
    "already-recorded": 409,
    "already-resulted": 409,
    "not-sender": 403,
}

_ULID = "[0-9A-HJKMNP-TV-Z]{26}"
INSTANCE_ID_RE = re.compile(f"ins_{_ULID}")
MESSAGE_ID_RE = re.compile(f"msg_{_ULID}")
RUN_ID_RE = re.compile(f"run_{_ULID}")
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
CHANNEL_RE = re.compile(r"[a-z][a-z0-9.-]{0,63}")
WORK_LINK_RE = re.compile(r"vuoro-work://link/v1/[A-Za-z0-9_-]+")
CORRELATION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REASON_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
EVIDENCE_KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_RFC3339_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)")

_HOST_CLASSES = ("workstation", "devbox", "cloud-interactive", "cloud-unattended", "other")
_INTERACTIVITY = ("interactive", "non-interactive")
_WAKE_MODES = ("read-while-running", "attention-during-run", "start-after-stop")


class InboxRefused(ValueError):
    """A contract refusal: ``code`` is the wire code, ``details`` its context."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details) if details is not None else None

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 409)


def invalid(message: str) -> InboxRefused:
    return InboxRefused("invalid-arguments", message)


def canonical_bytes(value: Any) -> bytes:
    """Keys sorted, no insignificant whitespace, UTF-8: the work owner's
    idempotency-digest encoding (contract section 3.1)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# -- primitive checks --------------------------------------------------------

def _object(value: Any, name: str, *, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(value, Mapping):
        raise invalid(f"{name} must be an object")
    missing = [key for key in required if key not in value]
    if missing:
        raise invalid(f"{name} lacks {', '.join(missing)}")
    extra = sorted(set(value) - set(required) - set(optional))
    if extra:
        raise invalid(f"{name} does not accept {', '.join(extra)}")
    return dict(value)


def _string(value: Any, name: str, *, min_length: int = 0, max_length: int | None = None,
            pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str):
        raise invalid(f"{name} must be a string")
    if len(value) < min_length or (max_length is not None and len(value) > max_length):
        raise invalid(f"{name} has an invalid length")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise invalid(f"{name} is malformed")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise invalid(f"{name} must be an integer")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise invalid(f"{name} is out of range")
    return value


def _bounded_text(value: Any, name: str, *, max_bytes: int = MAX_TEXT_BYTES, min_length: int = 0) -> str:
    text = _string(value, name, min_length=min_length, max_length=max_bytes)
    if len(text.encode("utf-8")) > max_bytes:
        raise invalid(f"{name} is longer than {max_bytes} bytes of UTF-8")
    return text


def _timestamp(value: Any, name: str) -> str:
    text = _string(value, name, pattern=_RFC3339_RE)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise invalid(f"{name} is not an RFC 3339 timestamp") from exc
    return text


def instance_id(value: Any) -> str:
    return _string(value, "instance_id", pattern=INSTANCE_ID_RE)


def message_id(value: Any) -> str:
    return _string(value, "message_id", pattern=MESSAGE_ID_RE)


def idempotency_key(value: Any) -> str:
    return _string(value, "idempotency_key", pattern=IDEMPOTENCY_KEY_RE)


def principal_id(value: Any) -> str:
    return _string(value, "principal_id", min_length=1, max_length=512)


def _reason(value: Any, name: str = "reason") -> dict:
    reason = _object(value, name, required=("code", "text"))
    _string(reason["code"], f"{name}.code", pattern=REASON_CODE_RE)
    _bounded_text(reason["text"], f"{name}.text")
    return reason


# -- declarations (agent-declaration/v1) -------------------------------------

def validate_declaration(value: Any) -> dict:
    declaration = _object(value, "declaration", required=(
        "schema", "harness", "adapter", "host_class", "interactivity", "accepted_kinds", "channels"))
    if declaration["schema"] != DECLARATION_SCHEMA:
        raise invalid(f"declaration.schema must be {DECLARATION_SCHEMA}")
    for name in ("harness", "adapter"):
        part = declaration[name]
        if name == "adapter" and part is None:
            continue
        part = _object(part, f"declaration.{name}", required=("name", "version"))
        for key in ("name", "version"):
            _string(part[key], f"declaration.{name}.{key}", min_length=1, max_length=64)
    if declaration["host_class"] not in _HOST_CLASSES:
        raise invalid("declaration.host_class is not a known host class")
    if declaration["interactivity"] not in _INTERACTIVITY:
        raise invalid("declaration.interactivity must be interactive or non-interactive")
    kinds = declaration["accepted_kinds"]
    if not isinstance(kinds, list) or len(kinds) > 8 or any(kind != "continue" for kind in kinds) \
            or len(kinds) != len(set(kinds)):
        raise invalid("declaration.accepted_kinds admits only unique 'continue' entries")
    channels = declaration["channels"]
    if not isinstance(channels, list) or len(channels) > 8:
        raise invalid("declaration.channels must be a list of at most 8 channels")
    for index, raw in enumerate(channels):
        channel = _object(raw, f"declaration.channels[{index}]", required=("channel", "wake_modes"))
        _string(channel["channel"], f"declaration.channels[{index}].channel", pattern=CHANNEL_RE)
        modes = channel["wake_modes"]
        if not isinstance(modes, list) or not modes or len(modes) != len(set(map(str, modes))) \
                or any(mode not in _WAKE_MODES for mode in modes):
            raise invalid(f"declaration.channels[{index}].wake_modes is invalid")
    return declaration


# -- envelopes (agent-message/v1) --------------------------------------------

def validate_envelope(value: Any) -> dict:
    """Schema-shape check; size, mode and owner-record checks come after."""
    envelope = _object(value, "message", required=(
        "schema", "message_id", "recipient", "kind", "mode", "context", "body", "created_at"),
        optional=("artifacts", "not_after"))
    if envelope["schema"] != MESSAGE_SCHEMA:
        raise invalid(f"message.schema must be {MESSAGE_SCHEMA}")
    message_id(envelope["message_id"])
    recipient = _object(envelope["recipient"], "message.recipient", required=("principal_id",),
                        optional=("instance_id",))
    _string(recipient["principal_id"], "message.recipient.principal_id", min_length=1, max_length=512)
    if "instance_id" in recipient:
        _string(recipient["instance_id"], "message.recipient.instance_id", pattern=INSTANCE_ID_RE)
    if envelope["kind"] != "continue":
        raise invalid("message.kind must be continue")
    if envelope["mode"] not in ("request", "delegation"):
        raise invalid("message.mode must be request or delegation")
    context = _object(envelope["context"], "message.context", required=(),
                      optional=("item_id", "predecessor_run_id", "work_link", "correlation_id"))
    if "item_id" not in context and "predecessor_run_id" not in context:
        raise invalid("message.context names neither item_id nor predecessor_run_id")
    if "item_id" in context:
        _integer(context["item_id"], "message.context.item_id", minimum=1)
    if "predecessor_run_id" in context:
        _string(context["predecessor_run_id"], "message.context.predecessor_run_id", pattern=RUN_ID_RE)
    if "work_link" in context:
        _string(context["work_link"], "message.context.work_link", max_length=4096, pattern=WORK_LINK_RE)
    if "correlation_id" in context:
        _string(context["correlation_id"], "message.context.correlation_id", pattern=CORRELATION_ID_RE)
    body = _object(envelope["body"], "message.body", required=("text",))
    _string(body["text"], "message.body.text", max_length=16384)
    if "artifacts" in envelope:
        artifacts = envelope["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > 16:
            raise invalid("message.artifacts must be a list of at most 16 references")
        for index, raw in enumerate(artifacts):
            artifact = _object(raw, f"message.artifacts[{index}]", required=("uri", "sha256", "size_bytes"))
            _string(artifact["uri"], f"message.artifacts[{index}].uri", min_length=1, max_length=2048)
            _string(artifact["sha256"], f"message.artifacts[{index}].sha256", pattern=SHA256_RE)
            _integer(artifact["size_bytes"], f"message.artifacts[{index}].size_bytes", minimum=0)
    if "not_after" in envelope:
        _timestamp(envelope["not_after"], "message.not_after")
    _timestamp(envelope["created_at"], "message.created_at")
    return envelope


def admit_envelope(value: Any) -> tuple[dict, bytes]:
    """Validate an envelope for sending; returns it and its canonical bytes.

    Order: shape (invalid-arguments), then the 64 KiB canonical cap and the
    16 KiB UTF-8 body cap (payload-too-large), then mode (mode-not-supported).
    """
    envelope = validate_envelope(value)
    encoded = canonical_bytes(envelope)
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise InboxRefused("payload-too-large",
                           f"the canonical envelope is {len(encoded)} bytes; the limit is {MAX_ENVELOPE_BYTES}")
    if len(envelope["body"]["text"].encode("utf-8")) > MAX_BODY_TEXT_BYTES:
        raise InboxRefused("payload-too-large",
                           f"message.body.text is longer than {MAX_BODY_TEXT_BYTES} bytes of UTF-8")
    if envelope["mode"] == "delegation":
        raise InboxRefused("mode-not-supported", "delegation mode is not supported in v1")
    return envelope, encoded


# -- operation arguments -----------------------------------------------------

def register_arguments(arguments: Mapping[str, Any]) -> dict:
    args = _object(arguments, "arguments", required=("instance_id", "declaration", "idempotency_key"))
    instance_id(args["instance_id"])
    idempotency_key(args["idempotency_key"])
    validate_declaration(args["declaration"])
    return args


def observe_arguments(arguments: Mapping[str, Any]) -> dict:
    args = _object(arguments, "arguments", required=("instance_id", "fact", "valid_for_seconds", "idempotency_key"),
                   optional=("channel", "run_id"))
    instance_id(args["instance_id"])
    idempotency_key(args["idempotency_key"])
    if args["fact"] not in FACTS:
        raise invalid("fact must be one of " + ", ".join(FACTS))
    _integer(args["valid_for_seconds"], "valid_for_seconds",
             minimum=MIN_VALID_FOR_SECONDS, maximum=MAX_VALID_FOR_SECONDS)
    if (args["fact"] == "reachable") != ("channel" in args):
        raise invalid("channel is required for reachable and absent otherwise")
    if (args["fact"] == "executing") != ("run_id" in args):
        raise invalid("run_id is required for executing and absent otherwise")
    if "channel" in args:
        _string(args["channel"], "channel", pattern=CHANNEL_RE)
    if "run_id" in args:
        _string(args["run_id"], "run_id", pattern=RUN_ID_RE)
    return args


def retire_arguments(arguments: Mapping[str, Any]) -> dict:
    args = _object(arguments, "arguments", required=("instance_id", "reason", "idempotency_key"))
    instance_id(args["instance_id"])
    idempotency_key(args["idempotency_key"])
    _bounded_text(args["reason"], "reason", min_length=1)
    return args


def list_arguments(arguments: Mapping[str, Any]) -> bool:
    args = _object(arguments, "arguments", required=(), optional=("include_retired",))
    include = args.get("include_retired", True)
    if not isinstance(include, bool):
        raise invalid("include_retired must be a boolean")
    return include


def describe_arguments(arguments: Mapping[str, Any]) -> str:
    args = _object(arguments, "arguments", required=("principal_id",))
    return principal_id(args["principal_id"])


def send_arguments(arguments: Mapping[str, Any]) -> tuple[dict, bytes]:
    args = _object(arguments, "arguments", required=("message",))
    return admit_envelope(args["message"])


def read_arguments(arguments: Mapping[str, Any]) -> dict:
    args = _object(arguments, "arguments", required=("after_seq", "limit"), optional=("instance_id",))
    _integer(args["after_seq"], "after_seq", minimum=0)
    _integer(args["limit"], "limit", minimum=1, maximum=MAX_READ_LIMIT)
    if "instance_id" in args:
        instance_id(args["instance_id"])
    return args


def status_arguments(arguments: Mapping[str, Any]) -> str:
    args = _object(arguments, "arguments", required=("message_id",))
    return message_id(args["message_id"])


def cancel_arguments(arguments: Mapping[str, Any]) -> dict:
    args = _object(arguments, "arguments", required=("message_id", "reason"))
    message_id(args["message_id"])
    _bounded_text(args["reason"], "reason")
    return args


_RECEIPT_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "observed": ((), ()),
    "acknowledged": ((), ()),
    "accepted": ((), ()),
    "refused": (("reason",), ()),
    "run-linked": (("run_id",), ()),
    # Contract amendment (vuoro#219): requires accepted, not run-linked;
    # run_id is optional and, when given, must name a run in scope.
    "launch-ambiguous": (("detail",), ("run_id",)),
    "result-linked": (("outcome", "evidence_refs", "summary"), ("reason",)),
}


def receipt_arguments(arguments: Mapping[str, Any]) -> tuple[str, str, str, dict]:
    """``(message_id, instance_id, kind, fields)``: ``fields`` are the kind's
    own fields, the input of its field digest (contract section 3.3)."""
    if not isinstance(arguments, Mapping):
        raise invalid("arguments must be an object")
    kind = arguments.get("kind")
    if kind not in _RECEIPT_FIELDS:
        raise invalid("kind must be one of " + ", ".join(RECEIPT_KINDS))
    required, optional = _RECEIPT_FIELDS[kind]
    args = _object(arguments, "arguments", required=("message_id", "instance_id", "kind", *required),
                   optional=optional)
    message_id(args["message_id"])
    instance_id(args["instance_id"])
    fields = {key: args[key] for key in (*required, *optional) if key in args}
    if kind == "refused":
        _reason(fields["reason"])
    elif kind == "run-linked":
        _string(fields["run_id"], "run_id", pattern=RUN_ID_RE)
    elif kind == "launch-ambiguous":
        _bounded_text(fields["detail"], "detail")
        if "run_id" in fields:
            _string(fields["run_id"], "run_id", pattern=RUN_ID_RE)
    elif kind == "result-linked":
        if fields["outcome"] not in OUTCOMES:
            raise invalid("outcome must be one of " + ", ".join(OUTCOMES))
        refs = fields["evidence_refs"]
        if not isinstance(refs, list) or len(refs) > 32:
            raise invalid("evidence_refs must be a list of at most 32 references")
        for index, raw in enumerate(refs):
            ref = _object(raw, f"evidence_refs[{index}]", required=("kind", "ref"))
            _string(ref["kind"], f"evidence_refs[{index}].kind", pattern=EVIDENCE_KIND_RE)
            _string(ref["ref"], f"evidence_refs[{index}].ref", min_length=1, max_length=512)
        _bounded_text(fields["summary"], "summary")
        if "reason" in fields:
            _reason(fields["reason"])
        elif fields["outcome"] == "not-started":
            raise invalid("a not-started outcome requires a reason")
    return args["message_id"], args["instance_id"], kind, fields
