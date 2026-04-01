from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

CONTEXT_CONTRACT_VERSION = "1"
HANDOFF_BUNDLE_TYPE = "handoff"
HANDOFF_BUNDLE_VERSION = "1"


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(value)


def _copy_mapping_list(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(value) for value in values]


@dataclass(frozen=True, slots=True)
class ContextContract:
    sprint: Mapping[str, Any]
    summary: Mapping[str, Any]
    active_claims: Sequence[Mapping[str, Any]]
    conflicts: Sequence[Mapping[str, Any]]
    ready_items: Sequence[Mapping[str, Any]]
    blocked_items: Sequence[Mapping[str, Any]]
    stale_items: Sequence[Mapping[str, Any]]
    recent_decisions: Sequence[Mapping[str, Any]]
    next_action: Mapping[str, Any]
    contract_version: str = CONTEXT_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "sprint": _copy_mapping(self.sprint),
            "summary": _copy_mapping(self.summary),
            "active_claims": _copy_mapping_list(self.active_claims),
            "conflicts": _copy_mapping_list(self.conflicts),
            "ready_items": _copy_mapping_list(self.ready_items),
            "blocked_items": _copy_mapping_list(self.blocked_items),
            "stale_items": _copy_mapping_list(self.stale_items),
            "recent_decisions": _copy_mapping_list(self.recent_decisions),
            "next_action": _copy_mapping(self.next_action),
        }


@dataclass(frozen=True, slots=True)
class HandoffBundle:
    sprintctl_version: str
    generated_at: str
    generated_from: Mapping[str, Any]
    sprint: Mapping[str, Any]
    summary: Mapping[str, Any]
    active_claims: Sequence[Mapping[str, Any]]
    conflicts: Sequence[Mapping[str, Any]]
    work: Mapping[str, Any]
    recent_decisions: Sequence[Mapping[str, Any]]
    recent_events: Sequence[Mapping[str, Any]]
    next_action: Mapping[str, Any]
    delta_since_last_handoff: Mapping[str, Any]
    freshness: Mapping[str, Any]
    evidence: Mapping[str, Any]
    git_context: Mapping[str, Any] | None
    claim_identity_model: Mapping[str, Any]
    resume_instructions: Sequence[str]
    agent_shutdown_protocol: Mapping[str, Any]
    items: Sequence[Mapping[str, Any]]
    events: Sequence[Mapping[str, Any]]
    bundle_type: str = HANDOFF_BUNDLE_TYPE
    bundle_version: str = HANDOFF_BUNDLE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_type": self.bundle_type,
            "bundle_version": self.bundle_version,
            "sprintctl_version": self.sprintctl_version,
            "generated_at": self.generated_at,
            "generated_from": _copy_mapping(self.generated_from),
            "sprint": _copy_mapping(self.sprint),
            "summary": _copy_mapping(self.summary),
            "active_claims": _copy_mapping_list(self.active_claims),
            "conflicts": _copy_mapping_list(self.conflicts),
            "work": _copy_mapping(self.work),
            "recent_decisions": _copy_mapping_list(self.recent_decisions),
            "recent_events": _copy_mapping_list(self.recent_events),
            "next_action": _copy_mapping(self.next_action),
            "delta_since_last_handoff": _copy_mapping(self.delta_since_last_handoff),
            "freshness": _copy_mapping(self.freshness),
            "evidence": _copy_mapping(self.evidence),
            "git_context": _copy_mapping(self.git_context) if self.git_context is not None else None,
            "claim_identity_model": _copy_mapping(self.claim_identity_model),
            "resume_instructions": list(self.resume_instructions),
            "agent_shutdown_protocol": _copy_mapping(self.agent_shutdown_protocol),
            "items": _copy_mapping_list(self.items),
            "events": _copy_mapping_list(self.events),
        }
