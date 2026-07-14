"""Pure observation-only shadow projection and parity evidence.

The shadow pilot needs a stable way to compare what a producer outbox observed
with an authoritative event sequence, without making the outbox or this
projection another authority path.  This module therefore accepts only known
observation records, canonicalizes the portable observation fields, and emits
JSON-safe comparison evidence.  It deliberately has no database or network
dependencies and never applies commands or remote decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Iterable, Mapping

from . import contracts, outbox


class UnsupportedShadowRecordError(ValueError):
    """A record is not a supported producer observation for the shadow pilot."""


class DuplicateShadowObservationError(ValueError):
    """A sequence contains the same observation identity more than once."""


class ParityClassification(StrEnum):
    """One mutually exclusive outcome for a shadow/authority observation pair."""

    EQUAL = "equal"
    MISSING = "missing"
    UNEXPECTED = "unexpected"
    MISMATCHED = "mismatched"


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """Canonical, non-authoritative representation of one supported observation."""

    event_id: str
    event_type: str
    actor: str
    occurred_at: str
    payload: dict[str, Any]
    runtime_session_id: str | None = None
    basis_revision: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _required_text(self.event_type, "event_type")
        _required_text(self.actor, "actor")
        _required_text(self.occurred_at, "occurred_at")
        if contracts.record_class_for_type(self.event_type) is not contracts.RecordClass.OBSERVATION:
            raise UnsupportedShadowRecordError(
                f"shadow projection only supports observations, not {self.event_type!r}"
            )
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))
        for field in (
            "runtime_session_id",
            "basis_revision",
            "correlation_id",
            "causation_id",
        ):
            value = getattr(self, field)
            if value is not None:
                _required_text(value, field)

    def to_dict(self) -> dict[str, Any]:
        """Return a defensive, JSON-safe copy suitable for pilot evidence."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "occurred_at": self.occurred_at,
            "payload": _json_object(self.payload, "payload"),
            "runtime_session_id": self.runtime_session_id,
            "basis_revision": self.basis_revision,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }


@dataclass(frozen=True, slots=True)
class ShadowProjection:
    """An ordered, read-only shadow view of supported producer observations."""

    observations: tuple[ShadowObservation, ...]

    def __post_init__(self) -> None:
        observations = tuple(self.observations)
        event_ids = [observation.event_id for observation in observations]
        if len(event_ids) != len(set(event_ids)):
            raise DuplicateShadowObservationError("shadow projection contains duplicate event_id values")
        object.__setattr__(self, "observations", observations)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation preserving the supplied sequence order."""
        return {"observations": [observation.to_dict() for observation in self.observations]}


@dataclass(frozen=True, slots=True)
class ParityEvidence:
    """Evidence for one identity in an authoritative/shadow comparison."""

    classification: ParityClassification
    event_id: str
    authoritative_index: int | None
    shadow_index: int | None
    authoritative: ShadowObservation | None
    shadow: ShadowObservation | None

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe evidence without exposing mutable source records."""
        return {
            "classification": self.classification.value,
            "event_id": self.event_id,
            "authoritative_index": self.authoritative_index,
            "shadow_index": self.shadow_index,
            "authoritative": self.authoritative.to_dict() if self.authoritative else None,
            "shadow": self.shadow.to_dict() if self.shadow else None,
        }


@dataclass(frozen=True, slots=True)
class ParityReport:
    """Deterministic structured parity evidence for a single comparison run."""

    evidence: tuple[ParityEvidence, ...]

    @property
    def is_equal(self) -> bool:
        """Whether every observed identity has an equal authoritative counterpart."""
        return all(item.classification is ParityClassification.EQUAL for item in self.evidence)

    @property
    def counts(self) -> dict[str, int]:
        """Return every classification count, including zero-valued categories."""
        counts = {classification.value: 0 for classification in ParityClassification}
        for item in self.evidence:
            counts[item.classification.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """Return a complete JSON-safe report for later pilot orchestration."""
        return {
            "is_equal": self.is_equal,
            "counts": self.counts,
            "evidence": [item.to_dict() for item in self.evidence],
        }


def project_observations(
    records: Iterable[outbox.OutboxRecord | Mapping[str, Any]],
) -> ShadowProjection:
    """Build a deterministic shadow projection from supported outbox observations.

    The input order remains visible in the resulting projection.  Commands,
    remote decisions, and unknown event types are rejected before their payload
    is read, so this read-side cannot reinterpret an authority operation.
    """
    observations = tuple(_coerce_observation(record) for record in records)
    return ShadowProjection(observations)


def compare_parity(
    authoritative: ShadowProjection | Iterable[outbox.OutboxRecord | Mapping[str, Any]],
    shadow: ShadowProjection | Iterable[outbox.OutboxRecord | Mapping[str, Any]],
) -> ParityReport:
    """Compare shadow observations with an authoritative observation sequence.

    Evidence follows the authoritative sequence first, then any extra shadow
    observations in shadow order.  Matching uses the immutable ``event_id``;
    shared identities are equal only when their canonical projected bodies are
    identical.  The function is pure and does not mutate either input.
    """
    authoritative_projection = _as_projection(authoritative)
    shadow_projection = _as_projection(shadow)
    shadow_by_id = {observation.event_id: (index, observation) for index, observation in enumerate(shadow_projection.observations)}
    authoritative_ids = {observation.event_id for observation in authoritative_projection.observations}
    evidence: list[ParityEvidence] = []

    for authoritative_index, authoritative_observation in enumerate(authoritative_projection.observations):
        shadow_item = shadow_by_id.get(authoritative_observation.event_id)
        if shadow_item is None:
            evidence.append(
                ParityEvidence(
                    classification=ParityClassification.MISSING,
                    event_id=authoritative_observation.event_id,
                    authoritative_index=authoritative_index,
                    shadow_index=None,
                    authoritative=authoritative_observation,
                    shadow=None,
                )
            )
            continue
        shadow_index, shadow_observation = shadow_item
        classification = (
            ParityClassification.EQUAL
            if authoritative_observation == shadow_observation
            else ParityClassification.MISMATCHED
        )
        evidence.append(
            ParityEvidence(
                classification=classification,
                event_id=authoritative_observation.event_id,
                authoritative_index=authoritative_index,
                shadow_index=shadow_index,
                authoritative=authoritative_observation,
                shadow=shadow_observation,
            )
        )

    for shadow_index, shadow_observation in enumerate(shadow_projection.observations):
        if shadow_observation.event_id not in authoritative_ids:
            evidence.append(
                ParityEvidence(
                    classification=ParityClassification.UNEXPECTED,
                    event_id=shadow_observation.event_id,
                    authoritative_index=None,
                    shadow_index=shadow_index,
                    authoritative=None,
                    shadow=shadow_observation,
                )
            )
    return ParityReport(tuple(evidence))


def _as_projection(
    value: ShadowProjection | Iterable[outbox.OutboxRecord | Mapping[str, Any]],
) -> ShadowProjection:
    return value if isinstance(value, ShadowProjection) else project_observations(value)


def _coerce_observation(value: outbox.OutboxRecord | Mapping[str, Any]) -> ShadowObservation:
    if isinstance(value, outbox.OutboxRecord):
        source: Mapping[str, Any] = {
            "record_class": value.record_class,
            "event_id": value.event_id,
            "event_type": value.event_type,
            "actor": value.actor,
            "occurred_at": value.occurred_at,
            "payload": value.payload,
            "runtime_session_id": value.runtime_session_id,
            "basis_revision": value.basis_revision,
            "correlation_id": value.correlation_id,
            "causation_id": value.causation_id,
        }
    elif isinstance(value, Mapping):
        source = value
    else:
        raise TypeError("shadow records must be outbox records or mappings")

    record_class = source.get("record_class")
    if record_class != contracts.RecordClass.OBSERVATION.value:
        raise UnsupportedShadowRecordError("shadow projection only accepts observation records")
    event_type = source.get("event_type")
    if contracts.record_class_for_type(event_type) is not contracts.RecordClass.OBSERVATION:
        raise UnsupportedShadowRecordError(f"shadow projection only supports observations, not {event_type!r}")
    try:
        return ShadowObservation(
            event_id=source["event_id"],
            event_type=event_type,
            actor=source["actor"],
            occurred_at=source["occurred_at"],
            payload=source["payload"],
            runtime_session_id=source.get("runtime_session_id"),
            basis_revision=source.get("basis_revision"),
            correlation_id=source.get("correlation_id"),
            causation_id=source.get("causation_id"),
        )
    except KeyError as exc:
        raise ValueError(f"shadow observation is missing required field {exc.args[0]!r}") from exc


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"shadow {field} must be a non-empty string")
    return value


def _json_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"shadow {field} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"shadow {field} keys must be strings")
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        canonical = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"shadow {field} must contain JSON-safe values") from exc
    if not isinstance(canonical, dict):  # Defensive: mapping input must decode as an object.
        raise ValueError(f"shadow {field} must be a JSON object")
    return canonical


__all__ = [
    "DuplicateShadowObservationError",
    "ParityClassification",
    "ParityEvidence",
    "ParityReport",
    "ShadowObservation",
    "ShadowProjection",
    "UnsupportedShadowRecordError",
    "compare_parity",
    "project_observations",
]
