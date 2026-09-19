from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import re
from typing import Any, ClassVar, Mapping, Sequence
from uuid import UUID

from . import decisions as _decisions

CONTEXT_CONTRACT_VERSION = "1"
HANDOFF_BUNDLE_TYPE = "handoff"
HANDOFF_BUNDLE_VERSION = "1"
ITEM_EDITED_EVENT_TYPE = "item-edited"
SPRINT_CLOSE_BOUNDARY_EVENT_TYPE = "sprint-close-boundary"
SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE = "sprint-close-boundary-imported"
SESSION_CAPSULE_RECORDED_EVENT_TYPE = "session-capsule.recorded"
ITEM_DECIDED_EVENT_TYPE = _decisions.ITEM_DECIDED_EVENT_TYPE
ITEM_DECIDED_IMPORTED_EVENT_TYPE = "item-decided-imported"
ITEM_EDITED_IMPORTED_EVENT_TYPE = "item-edited-imported"
# Request-scoped fields of an ``item-decided`` event.  They name a decision
# row and a request of the source database, so imported history drops them.
_ITEM_DECIDED_SOURCE_ONLY_FIELDS = ("idempotency_key", "decision_id")

# The capability-receipt surface was retired in PostgreSQL schema 14 / SQLite
# schema 23: accepted receipts became legacy accept decisions and drafted
# receipts legacy evidence.  The prefix stays reserved so no writer can bring
# a parallel acceptance record back.  Historical rows are read verbatim.
_RETIRED_CAPABILITY_RECEIPT_PREFIX = "capability-receipt"
_IMPORT_ONLY_EVENT_TYPES = {
    SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE,
    ITEM_DECIDED_IMPORTED_EVENT_TYPE,
    ITEM_EDITED_IMPORTED_EVENT_TYPE,
}

# A generic event is a note: it can describe a decision but never be one.
# These names are the vocabulary of work decisions and of the commands and
# authority records that carry them -- the decision kinds and resolutions,
# ``item-decided`` (reserved for the decision writer), the authority command
# and record types, and the terminal-status names an older client used.  A
# generic event under any of them could pose as the decision that closed an
# item, so the generic writer refuses them.  ``decision`` itself stays open:
# it is the knowledge-note type for a design decision and closes nothing.
DECISION_LIKE_EVENT_TYPES = frozenset(
    {
        *_decisions.DECISION_KINDS,
        *_decisions.RESOLUTIONS,
        _decisions.ITEM_DECIDED_EVENT_TYPE,
        "item.decide",
        "item.done",
        "item.transition",
        "item.transitioned",
        "decision.record",
        "work.decision.record",
        "work-decision.recorded",
        "work-decision",
        "item-done",
        "item-closed",
        "item-resolved",
        "item-accepted",
        "item-rejected",
        "item-withdrawn",
        "item-superseded",
    }
)


class DecisionLikeEventType(ValueError):
    """A generic event write named a decision-like event type."""


_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_TYPE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

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
    "release.commit-observed": RecordClass.OBSERVATION,
    "command.requested": RecordClass.AUTHORITY_COMMAND,
    "item.done": RecordClass.AUTHORITY_COMMAND,
    "item.transition": RecordClass.AUTHORITY_COMMAND,
    "sprint.activate": RecordClass.AUTHORITY_COMMAND,
    "sprint.close": RecordClass.AUTHORITY_COMMAND,
    "decision.record": RecordClass.AUTHORITY_COMMAND,
    "item.transitioned": RecordClass.REMOTE_DECISION,
    "sprint-activated": RecordClass.REMOTE_DECISION,
    "sprint-closed": RecordClass.REMOTE_DECISION,
    "work-decision.recorded": RecordClass.REMOTE_DECISION,
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


# Credential-shaped values (owner decision D3: prevention before insert).
# Each pattern is a well-known token or key format with a fixed prefix, so a
# match is a real credential far more often than a false positive.  Git SHAs,
# UUIDs and sha256 digests deliberately do not match.
_CREDENTIAL_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("age-secret-key", re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{50,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic-or-openai-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer-credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    (
        "uri-with-password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:(?!(?:\*+|<[^>]*>|\$\{[^}]*\}|redacted|password)@)[^\s/@]+@", re.I),
    ),
)


def credential_shape(text: str) -> str | None:
    """Return the name of the credential shape found in text, if any."""
    for name, pattern in _CREDENTIAL_VALUE_PATTERNS:
        if pattern.search(text):
            return name
    return None


def reject_credential_shaped_values(value: Any, field: str) -> None:
    """Refuse any string (key or value) anywhere in value that looks like a credential."""
    if isinstance(value, str):
        shape = credential_shape(value)
        if shape is not None:
            raise ValueError(f"{field} contains a credential-shaped value ({shape}); it must not be recorded")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            reject_credential_shaped_values(str(key), f"{field} key")
            reject_credential_shaped_values(nested, f"{field}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            reject_credential_shaped_values(nested, f"{field}[{index}]")


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
        "decision.record": "item",
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

    if record_type == "decision.record":
        source = _strict_fields(
            payload,
            field="payload",
            required={"kind"},
            optional={
                "rationale",
                "evidence_digests",
                "release_digest",
                "superseded_by_aggregate_uuid",
            },
        )
        fields = _decisions.normalize_decision_fields(
            source["kind"],
            rationale=source.get("rationale", ""),
            evidence_digests=source.get("evidence_digests", []),
            release_digest=source.get("release_digest"),
        )
        superseded_by = None
        if "superseded_by_aggregate_uuid" in source:
            superseded_by = _canonical_uuid(
                source["superseded_by_aggregate_uuid"],
                "payload.superseded_by_aggregate_uuid",
            )
        if (fields["kind"] == "supersede") != (superseded_by is not None):
            raise ValueError(
                "payload.superseded_by_aggregate_uuid is required for, and only for, supersede"
            )
        result = {
            "kind": fields["kind"],
            "rationale": fields["rationale"],
            "evidence_digests": fields["evidence_digests"],
        }
        if fields["release_digest"] is not None:
            result["release_digest"] = fields["release_digest"]
        if superseded_by is not None:
            result["superseded_by_aggregate_uuid"] = superseded_by
        return result

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
    if original_event_type == ITEM_DECIDED_EVENT_TYPE:
        for field in _ITEM_DECIDED_SOURCE_ONLY_FIELDS:
            source_payload.pop(field, None)
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
    if event_type == SPRINT_CLOSE_BOUNDARY_EVENT_TYPE:
        return canonicalize_sprint_close_boundary_payload(payload)
    if event_type == SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE:
        return _canonicalize_imported_typed_event_payload(
            payload,
            original_event_type=SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
        )
    if event_type == ITEM_DECIDED_IMPORTED_EVENT_TYPE:
        return _canonicalize_imported_typed_event_payload(
            payload,
            original_event_type=ITEM_DECIDED_EVENT_TYPE,
        )
    if event_type == ITEM_EDITED_IMPORTED_EVENT_TYPE:
        return _canonicalize_imported_typed_event_payload(
            payload,
            original_event_type=ITEM_EDITED_EVENT_TYPE,
        )
    # Retired capability-receipt history is carried verbatim; writing a new
    # one is refused by require_generic_event_write_allowed.
    return dict(payload or {})


def require_generic_event_write_allowed(event_type: str) -> None:
    """Reject event names whose provenance requires an internal workflow."""
    if event_type == ITEM_EDITED_EVENT_TYPE:
        raise ValueError("item-edited is reserved; use the item edit operation")
    if is_decision_like_event_type(event_type):
        raise DecisionLikeEventType(
            f"{event_type} is reserved: it is a decision-like event type, and a "
            "note cannot pose as a work decision; record one with item decide"
        )
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
    if is_retired_capability_receipt_type(event_type):
        raise ValueError(
            f"{event_type} is reserved: capability receipts were retired; "
            "acceptance is recorded only as a work decision"
        )


def is_decision_like_event_type(event_type: str) -> bool:
    """True for an event type a generic note may not use (see above)."""
    return isinstance(event_type, str) and event_type.strip().lower() in DECISION_LIKE_EVENT_TYPES


def is_retired_capability_receipt_type(event_type: str) -> bool:
    """True for any event or record type in the retired receipt namespace."""
    return isinstance(event_type, str) and event_type.startswith(
        _RETIRED_CAPABILITY_RECEIPT_PREFIX
    )


def is_archive_only_event_type(event_type: str) -> bool:
    return event_type in _IMPORT_ONLY_EVENT_TYPES


def requires_archive_import_handling(event_type: str, *, demote_item_edits: bool = False) -> bool:
    """True for event types an import must carry as archive-only history.

    ``demote_item_edits`` is for sprint import, which creates new items: the
    source's ``item-edited`` events describe edits of a different item, so
    they are kept as history instead of counting toward the new item's edit
    revision.  Whole-repository transfers keep them, because they carry the
    item they belong to.
    """
    types = {
        SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
        SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE,
        ITEM_DECIDED_EVENT_TYPE,
        ITEM_DECIDED_IMPORTED_EVENT_TYPE,
        ITEM_EDITED_IMPORTED_EVENT_TYPE,
    }
    if demote_item_edits:
        types.add(ITEM_EDITED_EVENT_TYPE)
    return event_type in types


def canonicalize_event_for_archive_import(
    event_type: str,
    payload: Mapping[str, Any] | None,
    source_event_id: int,
    *,
    demote_item_edits: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Demote local-authority events to explicit non-authoritative history.

    An imported ``item-decided`` event loses its idempotency key and decision
    id: both belong to the source database, and a key left in place would let
    an imported event answer a new request's replay lookup.
    """
    canonical_payload = canonicalize_event_payload(event_type, payload)
    imported_types = {
        SPRINT_CLOSE_BOUNDARY_EVENT_TYPE: SPRINT_CLOSE_BOUNDARY_IMPORTED_EVENT_TYPE,
        ITEM_DECIDED_EVENT_TYPE: ITEM_DECIDED_IMPORTED_EVENT_TYPE,
    }
    if demote_item_edits:
        imported_types[ITEM_EDITED_EVENT_TYPE] = ITEM_EDITED_IMPORTED_EVENT_TYPE
    imported_type = imported_types.get(event_type)
    if imported_type is None:
        return event_type, canonical_payload
    if event_type == ITEM_DECIDED_EVENT_TYPE:
        for field in _ITEM_DECIDED_SOURCE_ONLY_FIELDS:
            canonical_payload.pop(field, None)
    imported_payload = {
        "source_event_id": source_event_id,
        "source_event_type": event_type,
        "source_payload": canonical_payload,
    }
    return imported_type, canonicalize_event_payload(imported_type, imported_payload)


from .handoff_contract import ContextContract, HandoffBundle
