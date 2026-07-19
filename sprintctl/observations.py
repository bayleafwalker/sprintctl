"""Typed, item-linked evidence observations for the producer outbox.

This module interprets only the two additive observation contracts used to
link session exhaust to sprint execution memory.  It never opens an authority
backend or applies an item transition: callers can append while offline and
later render the retained basis revision against a separately supplied
current revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

from . import contracts, outbox


ITEM_EVIDENCE_SCHEMA_VERSION = "sprintctl-item-evidence/v1"
SESSION_CAPSULE_SCHEMA_VERSION = "session-capsule/v1"
SESSION_CAPSULE_RECORDED = contracts.SESSION_CAPSULE_RECORDED_EVENT_TYPE
WORK_COMPLETED = "work.completed"
ITEM_EVIDENCE_EVENT_TYPES = (WORK_COMPLETED, SESSION_CAPSULE_RECORDED)

_EVIDENCE_REF_KINDS = frozenset(
    {
        "git-commit",
        "sprint-event",
        "document",
        "verification-result",
        "release",
        "artifact",
    }
)
_SHA256_REVISION_PREFIX = "sha256:"


class BasisClassification(StrEnum):
    """How an observation's authored basis relates to a supplied read basis."""

    UNBASED = "unbased"
    UNRESOLVED = "unresolved"
    CURRENT = "current"
    ANACHRONISTIC = "anachronistic"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """An immutable evidence pointer shared with AgentOps artifact contracts."""

    kind: str
    source: str
    revision: str

    def __post_init__(self) -> None:
        if self.kind not in _EVIDENCE_REF_KINDS:
            allowed = ", ".join(sorted(_EVIDENCE_REF_KINDS))
            raise ValueError(f"evidence ref kind must be one of: {allowed}")
        _required_text(self.source, "evidence ref source")
        _required_text(self.revision, "evidence ref revision")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "source": self.source,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class BasisVisibility:
    """Explicit stale-basis metadata for one retained observation."""

    observed_revision: str | None
    current_revision: str | None
    classification: BasisClassification

    def to_dict(self) -> dict[str, str | None]:
        return {
            "observed_revision": self.observed_revision,
            "current_revision": self.current_revision,
            "classification": self.classification.value,
        }


@dataclass(frozen=True, slots=True)
class ItemEvidenceObservation:
    """Validated read model for an item-linked producer observation."""

    event_id: str
    event_type: str
    actor: str
    occurred_at: str
    repo_id: str
    sprint_id: int
    work_item_id: int
    runtime_session_id: str
    summary: str | None
    evidence_refs: tuple[EvidenceRef, ...]
    capsule_ref: EvidenceRef | None
    basis: BasisVisibility
    correlation_id: str | None
    causation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
            "repo_id": self.repo_id,
            "sprint_id": self.sprint_id,
            "work_item_id": self.work_item_id,
            "runtime_session_id": self.runtime_session_id,
            "summary": self.summary,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "capsule_ref": self.capsule_ref.to_dict() if self.capsule_ref else None,
            "basis": self.basis.to_dict(),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "authority_mutated": False,
        }


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty string without surrounding whitespace")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _canonical_uuid(value: str | UUID | None, field: str) -> str:
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def evidence_ref(value: EvidenceRef | Mapping[str, Any], *, field: str = "evidence ref") -> EvidenceRef:
    """Parse one exact immutable-ref object without accepting embedded evidence."""
    if isinstance(value, EvidenceRef):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    source = dict(value)
    expected = {"kind", "source", "revision"}
    if set(source) != expected:
        missing = sorted(expected - set(source))
        unknown = sorted(set(source) - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(f"{field} must contain exactly kind, source, and revision ({'; '.join(details)})")
    return EvidenceRef(
        kind=source["kind"],
        source=source["source"],
        revision=source["revision"],
    )


def _capsule_ref(value: EvidenceRef | Mapping[str, Any]) -> EvidenceRef:
    ref = evidence_ref(value, field="capsule_ref")
    if ref.kind != "artifact":
        raise ValueError("capsule_ref.kind must be artifact")
    digest = ref.revision.removeprefix(_SHA256_REVISION_PREFIX)
    if not ref.revision.startswith(_SHA256_REVISION_PREFIX) or len(digest) != 64:
        raise ValueError("capsule_ref.revision must be sha256:<64 lowercase hex characters>")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("capsule_ref.revision must be sha256:<64 lowercase hex characters>")
    return ref


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_item_evidence_observation(
    *,
    event_type: str,
    actor: str,
    repo_id: str,
    sprint_id: int,
    work_item_id: int,
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] = (),
    summary: str | None = None,
    capsule_ref: EvidenceRef | Mapping[str, Any] | None = None,
    basis_revision: str | None = None,
    event_id: str | UUID | None = None,
    authored_at: str,
    correlation_id: str | UUID | None = None,
    causation_id: str | UUID | None = None,
) -> contracts.Observation:
    """Build one strict item-linked observation envelope.

    The AgentOps-owned capsule remains external; only its immutable pointer is
    accepted.  ``work.completed`` records require evidence and never imply the
    separate ``item.done`` authority command.
    """
    if event_type not in ITEM_EVIDENCE_EVENT_TYPES:
        allowed = ", ".join(ITEM_EVIDENCE_EVENT_TYPES)
        raise ValueError(f"item evidence event_type must be one of: {allowed}")
    actor = _required_text(actor, "actor")
    repo_id = _required_text(repo_id, "repo_id")
    sprint_id = _positive_int(sprint_id, "sprint_id")
    work_item_id = _positive_int(work_item_id, "work_item_id")
    parsed_evidence = tuple(
        evidence_ref(value, field=f"evidence_refs[{index}]")
        for index, value in enumerate(evidence_refs)
    )
    if len({(ref.kind, ref.source, ref.revision) for ref in parsed_evidence}) != len(parsed_evidence):
        raise ValueError("evidence_refs must not contain duplicates")

    if event_type == WORK_COMPLETED:
        if not parsed_evidence:
            raise ValueError("work.completed requires at least one evidence_ref")
        summary = _required_text(summary, "summary")
        if capsule_ref is not None:
            raise ValueError("capsule_ref is only valid for session-capsule.recorded")
        parsed_capsule = None
        payload: dict[str, Any] = {
            "schema_version": ITEM_EVIDENCE_SCHEMA_VERSION,
            "summary": summary,
            "evidence_refs": [ref.to_dict() for ref in parsed_evidence],
        }
    else:
        if summary is not None:
            raise ValueError("summary is only valid for work.completed")
        if capsule_ref is None:
            raise ValueError("session-capsule.recorded requires capsule_ref")
        parsed_capsule = _capsule_ref(capsule_ref)
        payload = {
            "schema_version": ITEM_EVIDENCE_SCHEMA_VERSION,
            "artifact_schema_version": SESSION_CAPSULE_SCHEMA_VERSION,
            "capsule_ref": parsed_capsule.to_dict(),
            "evidence_refs": [ref.to_dict() for ref in parsed_evidence],
        }

    return contracts.Observation(
        event_id=_canonical_uuid(event_id, "event_id"),
        record_type=event_type,
        schema_version=ITEM_EVIDENCE_SCHEMA_VERSION,
        actor=actor,
        authored_at=authored_at,
        refs={
            "repo_id": repo_id,
            "sprint_id": sprint_id,
            "work_item_id": work_item_id,
        },
        payload=payload,
        basis_revision=basis_revision,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload_digest=_payload_digest(payload),
        artifact_digest=(
            parsed_capsule.revision.removeprefix(_SHA256_REVISION_PREFIX)
            if parsed_capsule is not None
            else None
        ),
    )


def append_item_evidence_observation(
    conn,
    *,
    runtime_session_id: str,
    **observation_fields: Any,
) -> outbox.OutboxRecord:
    """Validate and durably append item evidence without opening authority."""
    runtime_session_id = _required_text(runtime_session_id, "runtime_session_id")
    observation = build_item_evidence_observation(**observation_fields)
    return outbox.append_record(
        conn,
        observation,
        runtime_session_id=runtime_session_id,
    )


def classify_basis(
    observed_revision: str | None,
    current_revision: str | None,
) -> BasisVisibility:
    """Make stale, current, and unresolved bases explicit without discarding any."""
    if observed_revision is None:
        classification = BasisClassification.UNBASED
    elif current_revision is None:
        classification = BasisClassification.UNRESOLVED
    elif observed_revision == current_revision:
        classification = BasisClassification.CURRENT
    else:
        classification = BasisClassification.ANACHRONISTIC
    return BasisVisibility(observed_revision, current_revision, classification)


def project_item_evidence(
    record: outbox.OutboxRecord,
    *,
    current_revision: str | None = None,
) -> ItemEvidenceObservation:
    """Validate a transported record and expose its item and basis linkage."""
    if not isinstance(record, outbox.OutboxRecord):
        raise TypeError("record must be an OutboxRecord")
    if record.record_class != outbox.OBSERVATION or record.event_type not in ITEM_EVIDENCE_EVENT_TYPES:
        raise ValueError("record is not a supported item evidence observation")
    if record.runtime_session_id is None:
        raise ValueError("item evidence observation requires runtime_session_id")
    if _payload_digest(record.payload) != record.payload_sha256:
        raise ValueError("item evidence transport payload digest does not match its payload")
    envelope = contracts.record_from_dict(record.payload)
    if not isinstance(envelope, contracts.Observation):
        raise ValueError("item evidence payload must be an observation envelope")
    if envelope.event_id != record.event_id or envelope.record_type != record.event_type:
        raise ValueError("item evidence envelope identity does not match its transport record")
    if envelope.actor != record.actor or envelope.authored_at != record.occurred_at:
        raise ValueError("item evidence envelope authorship does not match its transport record")
    if (
        envelope.basis_revision != record.basis_revision
        or envelope.correlation_id != record.correlation_id
        or envelope.causation_id != record.causation_id
    ):
        raise ValueError("item evidence envelope correlation metadata does not match transport")
    if envelope.schema_version != ITEM_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            f"item evidence schema_version must be {ITEM_EVIDENCE_SCHEMA_VERSION}"
        )
    refs = dict(envelope.refs)
    if set(refs) != {"repo_id", "sprint_id", "work_item_id"}:
        raise ValueError("item evidence refs must contain exactly repo_id, sprint_id, and work_item_id")
    repo_id = _required_text(refs["repo_id"], "refs.repo_id")
    sprint_id = _positive_int(refs["sprint_id"], "refs.sprint_id")
    work_item_id = _positive_int(refs["work_item_id"], "refs.work_item_id")

    payload = dict(envelope.payload)
    if payload.get("schema_version") != ITEM_EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            f"item evidence payload schema_version must be {ITEM_EVIDENCE_SCHEMA_VERSION}"
        )
    if envelope.payload_digest != _payload_digest(payload):
        raise ValueError("item evidence envelope payload_digest does not match its payload")
    raw_evidence = payload.get("evidence_refs")
    if not isinstance(raw_evidence, list):
        raise ValueError("item evidence payload.evidence_refs must be an array")
    parsed_evidence = tuple(
        evidence_ref(value, field=f"payload.evidence_refs[{index}]")
        for index, value in enumerate(raw_evidence)
    )
    if len({(ref.kind, ref.source, ref.revision) for ref in parsed_evidence}) != len(parsed_evidence):
        raise ValueError("payload.evidence_refs must not contain duplicates")

    if record.event_type == WORK_COMPLETED:
        if set(payload) != {"schema_version", "summary", "evidence_refs"}:
            raise ValueError("work.completed payload has unexpected fields")
        if not parsed_evidence:
            raise ValueError("work.completed requires at least one evidence_ref")
        summary = _required_text(payload["summary"], "payload.summary")
        parsed_capsule = None
        if envelope.artifact_digest is not None:
            raise ValueError("work.completed must not carry artifact_digest")
    else:
        expected = {
            "schema_version",
            "artifact_schema_version",
            "capsule_ref",
            "evidence_refs",
        }
        if set(payload) != expected:
            raise ValueError("session-capsule.recorded payload has unexpected fields")
        if payload["artifact_schema_version"] != SESSION_CAPSULE_SCHEMA_VERSION:
            raise ValueError(
                f"artifact_schema_version must be {SESSION_CAPSULE_SCHEMA_VERSION}"
            )
        summary = None
        parsed_capsule = _capsule_ref(payload["capsule_ref"])
        expected_artifact_digest = parsed_capsule.revision.removeprefix(
            _SHA256_REVISION_PREFIX
        )
        if envelope.artifact_digest != expected_artifact_digest:
            raise ValueError("session capsule artifact_digest does not match capsule_ref")

    return ItemEvidenceObservation(
        event_id=record.event_id,
        event_type=record.event_type,
        actor=record.actor,
        occurred_at=record.occurred_at,
        repo_id=repo_id,
        sprint_id=sprint_id,
        work_item_id=work_item_id,
        runtime_session_id=record.runtime_session_id,
        summary=summary,
        evidence_refs=parsed_evidence,
        capsule_ref=parsed_capsule,
        basis=classify_basis(record.basis_revision, current_revision),
        correlation_id=record.correlation_id,
        causation_id=record.causation_id,
    )


def transport_record_from_mapping(value: Mapping[str, Any]) -> outbox.OutboxRecord:
    """Restore one JSON-safe cached transport record for evidence projection."""
    if not isinstance(value, Mapping):
        raise TypeError("transport record must be an object")
    source = dict(value)
    expected = {
        "origin_stream_id",
        "origin_seq",
        "event_id",
        "schema_version",
        "record_class",
        "event_type",
        "actor",
        "runtime_session_id",
        "occurred_at",
        "basis_revision",
        "correlation_id",
        "causation_id",
        "payload",
        "payload_sha256",
        "created_at",
    }
    if set(source) != expected:
        raise ValueError("cached transport record has unexpected fields")
    payload = source["payload"]
    if not isinstance(payload, Mapping):
        raise ValueError("cached transport record payload must be an object")
    return outbox.OutboxRecord(
        origin_stream_id=source["origin_stream_id"],
        origin_seq=source["origin_seq"],
        event_id=source["event_id"],
        schema_version=source["schema_version"],
        record_class=source["record_class"],
        event_type=source["event_type"],
        actor=source["actor"],
        runtime_session_id=source["runtime_session_id"],
        occurred_at=source["occurred_at"],
        basis_revision=source["basis_revision"],
        correlation_id=source["correlation_id"],
        causation_id=source["causation_id"],
        payload=dict(payload),
        payload_sha256=source["payload_sha256"],
        created_at=source["created_at"],
    )


def list_item_evidence(
    records: Iterable[outbox.OutboxRecord],
    *,
    work_item_id: int | None = None,
    event_type: str | None = None,
    current_revision: str | None = None,
) -> list[ItemEvidenceObservation]:
    """Project and filter typed evidence while ignoring unrelated observations."""
    if work_item_id is not None:
        work_item_id = _positive_int(work_item_id, "work_item_id")
    if event_type is not None and event_type not in ITEM_EVIDENCE_EVENT_TYPES:
        raise ValueError("event_type is not a supported item evidence observation")
    result = []
    for record in records:
        if record.record_class != outbox.OBSERVATION or record.event_type not in ITEM_EVIDENCE_EVENT_TYPES:
            continue
        projected = project_item_evidence(record, current_revision=current_revision)
        if work_item_id is not None and projected.work_item_id != work_item_id:
            continue
        if event_type is not None and projected.event_type != event_type:
            continue
        result.append(projected)
    return result


__all__ = [
    "BasisClassification",
    "BasisVisibility",
    "EvidenceRef",
    "ITEM_EVIDENCE_EVENT_TYPES",
    "ITEM_EVIDENCE_SCHEMA_VERSION",
    "ItemEvidenceObservation",
    "SESSION_CAPSULE_RECORDED",
    "SESSION_CAPSULE_SCHEMA_VERSION",
    "WORK_COMPLETED",
    "append_item_evidence_observation",
    "build_item_evidence_observation",
    "classify_basis",
    "evidence_ref",
    "list_item_evidence",
    "project_item_evidence",
    "transport_record_from_mapping",
]
