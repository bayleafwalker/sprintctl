"""Observation-only bridge from existing events to a producer outbox.

The shadow-pilot integration calls :func:`mirror_event` after its existing
authority write has completed.  This module deliberately receives an already
formed ``contracts.RecordEnvelope`` (or its JSON representation) and only
ever appends a classified observation to the separate producer outbox.  It
does not know about, open, or mutate either current authority backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import sqlite3
from typing import Any, Mapping

from . import contracts, outbox


class MirrorDisposition(StrEnum):
    """The explicit outcome of attempting to mirror an authority event."""

    MIRRORED = "mirrored"
    REFUSED_AUTHORITY_COMMAND = "refused-authority-command"
    REFUSED_REMOTE_DECISION = "refused-remote-decision"


@dataclass(frozen=True, slots=True)
class MirrorResult:
    """A successful append or an intentional no-op.

    ``record`` is populated only for ``MIRRORED``.  A caller can use
    ``mirrored`` to decide whether it should schedule synchronization without
    treating a command or remote decision refusal as an error.
    """

    disposition: MirrorDisposition
    event_id: str
    record_type: str
    record: outbox.OutboxRecord | None = None

    @property
    def mirrored(self) -> bool:
        return self.disposition is MirrorDisposition.MIRRORED


def canonical_event_envelope(
    event: contracts.RecordEnvelope | Mapping[str, Any],
) -> contracts.RecordEnvelope:
    """Parse an event and return a JSON-safe canonical contract envelope.

    Mapping inputs must be complete contract envelopes.  The JSON round trip
    provides a defensive copy and ensures later integration cannot place
    Python-only values in the transport payload.
    """
    if isinstance(event, contracts.RecordEnvelope):
        parsed = event
    elif isinstance(event, Mapping):
        parsed = contracts.record_from_dict(event)
    else:
        raise TypeError("event must be a RecordEnvelope or complete envelope mapping")

    # ``to_dict`` already validates the contract.  Canonical JSON also gives
    # callers a deterministic, detached payload to hand to the durable outbox.
    value = json.loads(
        json.dumps(parsed.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    return contracts.record_from_dict(value)


def mirror_event(
    producer_outbox: sqlite3.Connection,
    event: contracts.RecordEnvelope | Mapping[str, Any],
    *,
    runtime_session_id: str | None = None,
) -> MirrorResult:
    """Mirror one classified observation into ``producer_outbox``.

    The original event's globally unique ``event_id`` is passed straight to
    :func:`outbox.append_observation`, making retries idempotent by the
    authoritative event identity rather than by a locally minted key.  The
    complete canonical envelope is retained as the outbox payload so no
    contract metadata is lost during the dual-record pilot.

    Authority commands and remote decisions are intentionally explicit
    no-ops: this producer never turns them into local observations and never
    projects or writes authoritative state.
    """
    envelope = canonical_event_envelope(event)

    if envelope.record_class is contracts.RecordClass.AUTHORITY_COMMAND:
        return MirrorResult(
            MirrorDisposition.REFUSED_AUTHORITY_COMMAND,
            envelope.event_id,
            envelope.record_type,
        )
    if envelope.record_class is contracts.RecordClass.REMOTE_DECISION:
        return MirrorResult(
            MirrorDisposition.REFUSED_REMOTE_DECISION,
            envelope.event_id,
            envelope.record_type,
        )

    # The contract taxonomy currently has exactly three classes.  Keep this
    # fail-closed should that taxonomy grow before this pilot is updated.
    if envelope.record_class is not contracts.RecordClass.OBSERVATION:
        raise ValueError(f"unsupported record class: {envelope.record_class}")

    record = outbox.append_observation(
        producer_outbox,
        event_type=envelope.record_type,
        actor=envelope.actor,
        payload=envelope.to_dict(),
        runtime_session_id=runtime_session_id,
        basis_revision=envelope.basis_revision,
        correlation_id=envelope.correlation_id,
        causation_id=envelope.causation_id,
        occurred_at=envelope.authored_at,
        event_id=envelope.event_id,
    )
    return MirrorResult(MirrorDisposition.MIRRORED, envelope.event_id, envelope.record_type, record)
