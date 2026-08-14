from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, ClassVar, Mapping, Sequence
from uuid import UUID

CONTEXT_CONTRACT_VERSION = "1"
HANDOFF_BUNDLE_TYPE = "handoff"
HANDOFF_BUNDLE_VERSION = "1"
CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE = "capability-receipt-drafted"
CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE = "capability-receipt-drafted-imported"
ITEM_EDITED_EVENT_TYPE = "item-edited"
SPRINT_CLOSE_BOUNDARY_EVENT_TYPE = "sprint-close-boundary"
SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE = "sprint-close-boundary-imported"
SESSION_CAPSULE_RECORDED_EVENT_TYPE = "session-capsule.recorded"

_CAPABILITY_RECEIPT_EVENT_PREFIX = "capability-receipt-"
_IMPORT_ONLY_EVENT_TYPES = {
    CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE,
    SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE,
}

_PORTABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_RECEIPT_REQUIRED_FIELDS = {
    "project",
    "receipt_id",
    "receipt_path",
    "receipt_sha256",
}
_CAPABILITY_RECEIPT_OPTIONAL_FIELDS = {"boundary_summary"}
_BOUNDARY_SUMMARY_MAX_LENGTH = 280
_RECORD_TYPE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_CREDENTIAL_REF = re.compile(r"^sha256:[0-9a-f]{64}$")

_SECRET_FIELD_NAMES = {
    "claim_token",
    "token",
    "credential",
    "secret",
    "password",
    "api_key",
    "api_token",
    "access_token",
    "refresh_token",
    "private_key",
    "authorization",
}


class RecordClass(StrEnum):
    """The three protocol-level meanings a portable record can carry."""

    OBSERVATION = "observation"
    AUTHORITY_COMMAND = "authority-command"
    REMOTE_DECISION = "remote-decision"


# This is deliberately a taxonomy, not a transport registry.  Adding a record
# here does not write it to an outbox or teach a backend how to arbitrate it.
SPRINTCTL_RECORD_TYPE_CLASSES: dict[str, RecordClass] = {
    "note.recorded": RecordClass.OBSERVATION,
    "decision.recorded": RecordClass.OBSERVATION,
    "work.completed": RecordClass.OBSERVATION,
    SESSION_CAPSULE_RECORDED_EVENT_TYPE: RecordClass.OBSERVATION,
    "doc-ref.added": RecordClass.OBSERVATION,
    "command.requested": RecordClass.AUTHORITY_COMMAND,
    "item.done": RecordClass.AUTHORITY_COMMAND,
    "item.transition": RecordClass.AUTHORITY_COMMAND,
    "sprint.activate": RecordClass.AUTHORITY_COMMAND,
    "sprint.close": RecordClass.AUTHORITY_COMMAND,
    "capability-receipt.accept": RecordClass.AUTHORITY_COMMAND,
    "item.transitioned": RecordClass.REMOTE_DECISION,
    "sprint-activated": RecordClass.REMOTE_DECISION,
    "sprint-closed": RecordClass.REMOTE_DECISION,
    "capability-receipt.accepted": RecordClass.REMOTE_DECISION,
    "command.rejected": RecordClass.REMOTE_DECISION,
}


def record_class_for_type(record_type: str) -> RecordClass:
    """Return the matrix-defined semantic class for a sprintctl record type."""
    if not isinstance(record_type, str) or not _RECORD_TYPE.fullmatch(record_type):
        raise ValueError("record_type must be a lowercase dotted, dashed, or underscored name")
    try:
        return SPRINTCTL_RECORD_TYPE_CLASSES[record_type]
    except KeyError as exc:
        raise ValueError(f"record_type is not classified by the sprintctl matrix: {record_type}") from exc


def _canonical_uuid(value: str | UUID | None, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a UUID")
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string without surrounding whitespace")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field)


def _canonical_timestamp(value: Any) -> str:
    timestamp = _required_string(value, "authored_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("authored_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("authored_at must include a timezone")
    return timestamp


def _canonical_json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    try:
        canonical = json.loads(json.dumps(dict(value), separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain JSON-compatible values") from exc
    if not isinstance(canonical, dict):  # Defensive; json.loads above always returns a dict here.
        raise ValueError(f"{field} must be a JSON object")
    return canonical


def _reject_secret_material(value: Any, field: str) -> None:
    """Reject raw proof or credential material anywhere in a command body."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SECRET_FIELD_NAMES:
                raise ValueError(f"{field} must not contain secret field {key!r}")
            _reject_secret_material(nested, f"{field}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_secret_material(nested, f"{field}[{index}]")


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _positive_ttl(value: Any) -> int:
    value = _positive_int(value, "payload.ttl_seconds")
    if value > 86_400:
        raise ValueError("payload.ttl_seconds must be at most 86400")
    return value


def _credential_ref(value: Any) -> str:
    value = _required_string(value, "payload.credential_ref")
    if not _CREDENTIAL_REF.fullmatch(value):
        raise ValueError("payload.credential_ref must be sha256:<64 lowercase hex characters>")
    return value


def _canonical_claim_metadata(value: Any) -> dict[str, Any]:
    source = _strict_fields(
        value,
        field="payload.metadata",
        required=set(),
        optional={
            "runtime_session_id",
            "instance_id",
            "branch",
            "worktree_path",
            "commit_sha",
            "pr_ref",
            "hostname",
            "pid",
        },
    )
    result: dict[str, Any] = {}
    for field in (
        "runtime_session_id",
        "instance_id",
        "branch",
        "worktree_path",
        "commit_sha",
        "pr_ref",
        "hostname",
    ):
        if field in source:
            result[field] = _optional_string(source[field], f"payload.metadata.{field}")
    if "pid" in source:
        result["pid"] = _positive_int(source["pid"], "payload.metadata.pid")
    return result


def _strict_fields(
    value: Mapping[str, Any],
    *,
    field: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    source = _canonical_json_object(value, field)
    optional = optional or set()
    unknown = sorted(set(source) - required - optional)
    missing = sorted(required - set(source))
    if unknown:
        raise ValueError(f"{field} has unknown fields: " + ", ".join(unknown))
    if missing:
        raise ValueError(f"{field} is missing fields: " + ", ".join(missing))
    _reject_secret_material(source, field)
    return source


def _canonical_authority_refs(record_type: str, refs: Mapping[str, Any]) -> dict[str, Any]:
    expected_aggregate = {
        "item.transition": "item",
        "item.done": "item",
        "sprint.activate": "sprint",
        "sprint.close": "sprint",
        "capability-receipt.accept": "sprint",
    }.get(record_type)
    if expected_aggregate is None:
        raise ValueError(
            f"authority command {record_type!r} must use a specific command payload contract"
        )
    required = {"repo_id", "aggregate_type"}
    optional = {"aggregate_id"}
    required.add("aggregate_uuid")
    source = _strict_fields(refs, field="refs", required=required, optional=optional)
    repo_id = _canonical_uuid(source["repo_id"], "refs.repo_id")
    if repo_id is None:
        raise ValueError("refs.repo_id must be a UUID")
    if source["aggregate_type"] != expected_aggregate:
        raise ValueError(
            f"refs.aggregate_type must be {expected_aggregate!r} for {record_type}"
        )
    result: dict[str, Any] = {
        "repo_id": repo_id,
        "aggregate_type": expected_aggregate,
    }
    aggregate_uuid = _canonical_uuid(source["aggregate_uuid"], "refs.aggregate_uuid")
    if aggregate_uuid is None:
        raise ValueError("refs.aggregate_uuid must be a UUID")
    result["aggregate_uuid"] = aggregate_uuid
    if "aggregate_id" in source:
        result["aggregate_id"] = _positive_int(source["aggregate_id"], "refs.aggregate_id")
    return result


def _canonical_authority_payload(record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if record_type in {"item.transition", "item.done"}:
        source = _strict_fields(
            payload,
            field="payload",
            required={"to_status"},
            optional=set(),
        )
        allowed_statuses = {"pending", "active", "done", "blocked"}
        to_status = _required_string(source["to_status"], "payload.to_status")
        if to_status not in allowed_statuses:
            raise ValueError("payload.to_status must be pending, active, done, or blocked")
        if record_type == "item.done" and to_status != "done":
            raise ValueError("item.done payload.to_status must be 'done'")
        result: dict[str, Any] = {"to_status": to_status}
        return result

    if record_type in {"sprint.activate", "sprint.close"}:
        return _strict_fields(payload, field="payload", required=set())


    if record_type == "capability-receipt.accept":
        source = _strict_fields(payload, field="payload", required={"pointer"})
        pointer = canonicalize_capability_receipt_drafted_payload(source["pointer"])
        _reject_secret_material(pointer, "payload.pointer")
        return {"pointer": pointer}

    raise ValueError(f"no authority command payload contract for {record_type}")


def _optional_sha256(value: Any, field: str) -> str | None:
    value = _optional_string(value, field)
    if value is not None and not _LOWERCASE_SHA256.fullmatch(value):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True, slots=True)
class RecordEnvelope:
    """Portable record fields common to observations, commands, and decisions."""

    event_id: str | UUID
    record_type: str
    schema_version: str
    actor: str
    authored_at: str
    refs: Mapping[str, Any]
    payload: Mapping[str, Any]
    basis_revision: str | None = None
    correlation_id: str | UUID | None = None
    causation_id: str | UUID | None = None
    payload_digest: str | None = None
    artifact_digest: str | None = None

    record_class: ClassVar[RecordClass]

    def __post_init__(self) -> None:
        expected_class = record_class_for_type(self.record_type)
        if expected_class != self.record_class:
            raise ValueError(
                f"record_type {self.record_type!r} is {expected_class.value}, "
                f"not {self.record_class.value}"
            )
        event_id = _canonical_uuid(self.event_id, "event_id")
        assert event_id is not None
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "schema_version", _required_string(self.schema_version, "schema_version"))
        object.__setattr__(self, "actor", _required_string(self.actor, "actor"))
        object.__setattr__(self, "authored_at", _canonical_timestamp(self.authored_at))
        object.__setattr__(self, "refs", _canonical_json_object(self.refs, "refs"))
        object.__setattr__(self, "payload", _canonical_json_object(self.payload, "payload"))
        object.__setattr__(self, "basis_revision", _optional_string(self.basis_revision, "basis_revision"))
        object.__setattr__(self, "correlation_id", _canonical_uuid(self.correlation_id, "correlation_id"))
        object.__setattr__(self, "causation_id", _canonical_uuid(self.causation_id, "causation_id"))
        object.__setattr__(self, "payload_digest", _optional_sha256(self.payload_digest, "payload_digest"))
        object.__setattr__(self, "artifact_digest", _optional_sha256(self.artifact_digest, "artifact_digest"))

    @property
    def offline_bufferable(self) -> bool:
        return self.record_class is RecordClass.OBSERVATION

    @property
    def requires_remote_arbitration(self) -> bool:
        return self.record_class is RecordClass.AUTHORITY_COMMAND

    @property
    def remote_authored(self) -> bool:
        return self.record_class is RecordClass.REMOTE_DECISION

    def to_dict(self) -> dict[str, Any]:
        """Return a defensively copied, stable-order JSON-ready envelope."""
        return {
            "event_id": self.event_id,
            "record_type": self.record_type,
            "record_class": self.record_class.value,
            "schema_version": self.schema_version,
            "actor": self.actor,
            "authored_at": self.authored_at,
            "refs": _canonical_json_object(self.refs, "refs"),
            "payload": _canonical_json_object(self.payload, "payload"),
            "basis_revision": self.basis_revision,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload_digest": self.payload_digest,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True, slots=True)
class Observation(RecordEnvelope):
    """A union/dedup-safe fact that a producer may append offline."""

    record_class: ClassVar[RecordClass] = RecordClass.OBSERVATION


@dataclass(frozen=True, slots=True)
class AuthorityCommand(RecordEnvelope):
    """A request that cannot take effect without remote arbitration."""

    record_class: ClassVar[RecordClass] = RecordClass.AUTHORITY_COMMAND

    def __post_init__(self) -> None:
        RecordEnvelope.__post_init__(self)
        if self.basis_revision is None:
            raise ValueError(f"basis_revision is required for authority command {self.record_type}")
        refs = _canonical_authority_refs(self.record_type, self.refs)
        payload = _canonical_authority_payload(self.record_type, self.payload)
        object.__setattr__(self, "refs", refs)
        object.__setattr__(self, "payload", payload)


@dataclass(frozen=True, slots=True)
class RemoteDecision(RecordEnvelope):
    """An outcome that only the owning remote authority may author."""

    record_class: ClassVar[RecordClass] = RecordClass.REMOTE_DECISION


_RECORD_CLASS_TYPES: dict[RecordClass, type[RecordEnvelope]] = {
    RecordClass.OBSERVATION: Observation,
    RecordClass.AUTHORITY_COMMAND: AuthorityCommand,
    RecordClass.REMOTE_DECISION: RemoteDecision,
}
_RECORD_ENVELOPE_FIELDS = {
    "event_id",
    "record_type",
    "record_class",
    "schema_version",
    "actor",
    "authored_at",
    "refs",
    "payload",
    "basis_revision",
    "correlation_id",
    "causation_id",
    "payload_digest",
    "artifact_digest",
}


def record_from_dict(value: Mapping[str, Any]) -> RecordEnvelope:
    """Parse a JSON envelope and reject a class that conflicts with its type."""
    if not isinstance(value, Mapping):
        raise ValueError("record envelope must be an object")
    source = dict(value)
    unknown = sorted(set(source) - _RECORD_ENVELOPE_FIELDS)
    missing = sorted(_RECORD_ENVELOPE_FIELDS - set(source))
    if unknown:
        raise ValueError("record envelope has unknown fields: " + ", ".join(unknown))
    if missing:
        raise ValueError("record envelope is missing fields: " + ", ".join(missing))
    try:
        record_class = RecordClass(source.pop("record_class"))
    except (TypeError, ValueError) as exc:
        raise ValueError("record_class must be observation, authority-command, or remote-decision") from exc
    return _RECORD_CLASS_TYPES[record_class](**source)


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(value)


def _copy_mapping_list(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(value) for value in values]


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(tag) for tag in value if str(tag).strip()]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return []


def canonicalize_decision_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    summary = source.pop("summary", None)
    detail = source.pop("detail", None)
    tags = _normalize_tags(source.pop("tags", []))

    result: dict[str, Any] = {
        "summary": str(summary) if summary is not None else "decision",
        "detail": detail if detail is None or isinstance(detail, str) else str(detail),
        "tags": tags,
    }
    for field in ("evidence_item_id", "evidence_event_id", "git_branch", "git_sha", "git_worktree"):
        if field in source:
            result[field] = source.pop(field)
    result.update(source)
    return result


def canonicalize_claim_handoff_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    summary = source.pop("summary", None)
    detail = source.pop("detail", None)
    tags = _normalize_tags(source.pop("tags", ["claims", "handoff", "coordination"]))

    result: dict[str, Any] = {
        "summary": str(summary) if summary is not None else "claim-handoff",
        "detail": detail if detail is None or isinstance(detail, str) else str(detail),
        "tags": tags,
        "operation": source.pop("operation", "handoff"),
        "mode": source.pop("mode", "rotate"),
        "legacy_adopted": bool(source.pop("legacy_adopted", False)),
        "token_rotated": bool(source.pop("token_rotated", False)),
        "from_identity": dict(source.pop("from_identity", {})),
        "to_identity": dict(source.pop("to_identity", {})),
    }
    result.update(source)
    return result


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def canonicalize_sprint_taken_up_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    summary = source.pop("summary", None)
    detail = source.pop("detail", None)
    tags = _normalize_tags(source.pop("tags", ["takeup"]))

    result: dict[str, Any] = {
        "summary": str(summary) if summary is not None else "sprint takeup",
        "detail": _normalize_optional_string(detail),
        "tags": tags or ["takeup"],
        "actor_kind": str(source.pop("actor_kind", "agent")),
        "hostname": _normalize_optional_string(source.pop("hostname", None)),
        "pid": _normalize_optional_int(source.pop("pid", None)),
        "instance_id": _normalize_optional_string(source.pop("instance_id", None)),
        "runtime_session_id": _normalize_optional_string(source.pop("runtime_session_id", None)),
        "context": _normalize_optional_string(source.pop("context", None)),
        "forced": bool(source.pop("forced", False)),
    }
    result.update(source)
    return result


def canonicalize_sprint_released_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    summary = source.pop("summary", None)
    detail = source.pop("detail", None)
    tags = _normalize_tags(source.pop("tags", ["takeup"]))

    result: dict[str, Any] = {
        "summary": str(summary) if summary is not None else "sprint release",
        "detail": _normalize_optional_string(detail),
        "tags": tags or ["takeup"],
        "actor_kind": str(source.pop("actor_kind", "agent")),
        "hostname": _normalize_optional_string(source.pop("hostname", None)),
        "pid": _normalize_optional_int(source.pop("pid", None)),
        "instance_id": _normalize_optional_string(source.pop("instance_id", None)),
        "runtime_session_id": _normalize_optional_string(source.pop("runtime_session_id", None)),
        "reason": _normalize_optional_string(source.pop("reason", None)),
        "matched_takeup_event_id": _normalize_optional_int(
            source.pop("matched_takeup_event_id", None)
        ),
    }
    result.update(source)
    return result


def _require_typed_string(source: Mapping[str, Any], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string without surrounding whitespace")
    return value


def canonicalize_capability_receipt_drafted_payload(
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the privacy-preserving pointer stored for a private receipt draft."""
    if not isinstance(payload, Mapping):
        raise ValueError("capability-receipt-drafted payload must be an object")
    source = dict(payload)
    allowed = _CAPABILITY_RECEIPT_REQUIRED_FIELDS | _CAPABILITY_RECEIPT_OPTIONAL_FIELDS
    unknown = sorted(set(source) - allowed)
    if unknown:
        raise ValueError(
            "capability-receipt-drafted payload has unknown fields: " + ", ".join(unknown)
        )
    missing = sorted(_CAPABILITY_RECEIPT_REQUIRED_FIELDS - set(source))
    if missing:
        raise ValueError(
            "capability-receipt-drafted payload is missing fields: " + ", ".join(missing)
        )

    project = _require_typed_string(source, "project")
    receipt_id = _require_typed_string(source, "receipt_id")
    if not _PORTABLE_IDENTIFIER.fullmatch(project):
        raise ValueError("project must be a portable identifier")
    if not _PORTABLE_IDENTIFIER.fullmatch(receipt_id):
        raise ValueError("receipt_id must be a portable identifier")
    if not receipt_id.startswith(f"{project}."):
        raise ValueError("receipt_id must start with '<project>.'")

    receipt_path = _require_typed_string(source, "receipt_path")
    expected_path = (
        f"/projects/dev/_artifacts/{project}/capability/receipts/{receipt_id}.json"
    )
    if receipt_path != expected_path:
        raise ValueError(f"receipt_path must be exactly {expected_path}")

    receipt_sha256 = _require_typed_string(source, "receipt_sha256")
    if not _LOWERCASE_SHA256.fullmatch(receipt_sha256):
        raise ValueError("receipt_sha256 must be 64 lowercase hexadecimal characters")

    result = {
        "project": project,
        "receipt_id": receipt_id,
        "receipt_path": receipt_path,
        "receipt_sha256": receipt_sha256,
    }
    if "boundary_summary" in source:
        summary = _require_typed_string(source, "boundary_summary")
        if "\n" in summary or "\r" in summary or len(summary) > _BOUNDARY_SUMMARY_MAX_LENGTH:
            raise ValueError("boundary_summary must be one line of at most 280 characters")
        result["boundary_summary"] = summary
    return result


def canonicalize_sprint_close_boundary_payload(
    payload: Mapping[str, Any] | None,
) -> dict[str, str]:
    if payload != {"previous_status": "active", "status": "closed"}:
        raise ValueError(
            "sprint-close-boundary payload must be exactly "
            "{'previous_status': 'active', 'status': 'closed'}"
        )
    return {"previous_status": "active", "status": "closed"}


def _canonicalize_imported_typed_event_payload(
    payload: Mapping[str, Any] | None,
    *,
    original_event_type: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{original_event_type}-imported payload must be an object")
    source = dict(payload)
    expected_fields = {"source_event_id", "source_event_type", "source_payload"}
    if set(source) != expected_fields:
        raise ValueError(
            f"{original_event_type}-imported payload must contain exactly "
            "source_event_id, source_event_type, and source_payload"
        )
    source_event_id = source["source_event_id"]
    if isinstance(source_event_id, bool) or not isinstance(source_event_id, int) or source_event_id < 1:
        raise ValueError("source_event_id must be a positive integer")
    if source["source_event_type"] != original_event_type:
        raise ValueError(f"source_event_type must be {original_event_type}")
    source_payload = canonicalize_event_payload(original_event_type, source["source_payload"])
    return {
        "source_event_id": source_event_id,
        "source_event_type": original_event_type,
        "source_payload": source_payload,
    }


def canonicalize_event_payload(event_type: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if event_type == "decision":
        return canonicalize_decision_payload(payload)
    if event_type in {"claim-handoff", "claim-ownership-corrected"}:
        return canonicalize_claim_handoff_payload(payload)
    if event_type == "sprint-taken-up":
        return canonicalize_sprint_taken_up_payload(payload)
    if event_type == "sprint-released":
        return canonicalize_sprint_released_payload(payload)
    if event_type == CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE:
        return canonicalize_capability_receipt_drafted_payload(payload)
    if event_type == SPRINT_CLOSE_BOUNDARY_EVENT_TYPE:
        return canonicalize_sprint_close_boundary_payload(payload)
    if event_type == CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE:
        return _canonicalize_imported_typed_event_payload(
            payload,
            original_event_type=CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
        )
    if event_type == SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE:
        return _canonicalize_imported_typed_event_payload(
            payload,
            original_event_type=SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
        )
    if event_type.startswith(_CAPABILITY_RECEIPT_EVENT_PREFIX):
        raise ValueError(f"unsupported reserved capability receipt event type: {event_type}")
    return dict(payload or {})


def require_generic_event_write_allowed(event_type: str) -> None:
    """Reject event names whose provenance requires an internal workflow."""
    if event_type == ITEM_EDITED_EVENT_TYPE:
        raise ValueError("item-edited is reserved; use the item edit operation")
    if event_type == SESSION_CAPSULE_RECORDED_EVENT_TYPE:
        raise ValueError(
            "session-capsule.recorded is reserved; use event observation add"
        )
    if event_type == SPRINT_CLOSE_BOUNDARY_EVENT_TYPE:
        raise ValueError(
            "sprint-close-boundary is reserved; use the atomic sprint close operation"
        )
    if event_type in _IMPORT_ONLY_EVENT_TYPES:
        raise ValueError(f"{event_type} is reserved for archive import")
    if (
        event_type.startswith(_CAPABILITY_RECEIPT_EVENT_PREFIX)
        and event_type != CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
    ):
        raise ValueError(f"unsupported reserved capability receipt event type: {event_type}")


def is_archive_only_event_type(event_type: str) -> bool:
    return event_type in _IMPORT_ONLY_EVENT_TYPES


def requires_archive_import_handling(event_type: str) -> bool:
    return event_type in {
        CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
        SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
        CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE,
        SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE,
    }


def canonicalize_event_for_archive_import(
    event_type: str,
    payload: Mapping[str, Any] | None,
    source_event_id: int,
) -> tuple[str, dict[str, Any]]:
    """Demote local-authority events to explicit non-authoritative history."""
    canonical_payload = canonicalize_event_payload(event_type, payload)
    imported_types = {
        CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE: CAPABILITY_RECEIPT_DRAFTED_IMPORTED_EVENT_TYPE,
        SPRINT_CLOSE_BOUNDARY_EVENT_TYPE: SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE,
    }
    imported_type = imported_types.get(event_type)
    if imported_type is None:
        return event_type, canonical_payload
    imported_payload = {
        "source_event_id": source_event_id,
        "source_event_type": event_type,
        "source_payload": canonical_payload,
    }
    return imported_type, canonicalize_event_payload(imported_type, imported_payload)


def _read_capability_receipt_bytes(receipt_path: str) -> bytes:
    path = Path(receipt_path)
    if not path.is_file():
        raise ValueError(f"capability receipt file does not exist: {receipt_path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read capability receipt file: {receipt_path}: {exc}") from exc


def verify_capability_receipt_draft_pointer(
    payload: Mapping[str, Any],
    *,
    sprint_id: int,
    boundary_event_id: int,
) -> None:
    """Verify local pointer facts without replacing Agentops semantic validation."""
    canonical = canonicalize_capability_receipt_drafted_payload(payload)
    receipt_bytes = _read_capability_receipt_bytes(canonical["receipt_path"])
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    if digest != canonical["receipt_sha256"]:
        raise ValueError("receipt_sha256 does not match the referenced file")
    try:
        receipt = json.loads(receipt_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("capability receipt file must contain valid UTF-8 JSON") from exc
    if not isinstance(receipt, Mapping):
        raise ValueError("capability receipt file must contain a JSON object")

    expected_fields = {
        "schema_version": "capability-receipt/v1",
        "id": canonical["receipt_id"],
        "project": canonical["project"],
        "status": "draft",
        "publication": "private",
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            raise ValueError(f"capability receipt {field} must be {expected!r}")

    boundary = receipt.get("boundary")
    if not isinstance(boundary, Mapping) or boundary.get("kind") != "sprint-close":
        raise ValueError("capability receipt boundary.kind must be 'sprint-close'")
    boundary_ref = boundary.get("ref")
    if not isinstance(boundary_ref, Mapping) or boundary_ref.get("kind") != "sprint-event":
        raise ValueError("capability receipt boundary.ref.kind must be 'sprint-event'")
    expected_source = f"sprintctl:{canonical['project']}:sprint:{sprint_id}"
    if boundary_ref.get("source") != expected_source:
        raise ValueError(f"capability receipt boundary.ref.source must be {expected_source!r}")
    expected_revision = f"event:{boundary_event_id}"
    if boundary_ref.get("revision") != expected_revision:
        raise ValueError(
            f"capability receipt boundary.ref.revision must be {expected_revision!r}"
        )


from .handoff_contract import ContextContract, HandoffBundle
