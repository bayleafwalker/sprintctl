"""A bounded presentation of existing owner effect facts, with no verdict writer."""

from __future__ import annotations

import json
from typing import Any


def _quoted(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def render_effect_explanation(response: dict[str, Any]) -> str:
    """Do not promote an intent lifecycle or receipt predicate to target evidence."""
    snapshot = response["snapshot"]
    intent = snapshot["intent"]
    release = snapshot["current_release"]
    bound_digest = intent["release_digest"]
    current_digest = release["release_digest"] if release is not None else None
    release_comparison = ("unknown" if bound_digest is None or current_digest is None else
                          "matches" if bound_digest == current_digest else "mismatch")
    receipt = snapshot["receipt"]
    visibility = receipt["status"]
    if visibility == "not-found":
        visibility = "not found/visible for this caller"
    lines = [
        f"EFFECT FACTS: {response['repo_id']}#{intent['item_id']}",
        f"Intent: {_quoted(intent['intent_id'])}, revision {intent['revision']}",
        f"Owner snapshot identity: {snapshot['snapshot_digest']}",
        "Snapshot identity is not a seal of the entire response or receipt.",
        "REQUESTED BASIS: " + ("matches" if snapshot["requested_basis_matches"] else "MISMATCH"),
        "REQUESTED ARTIFACT: " + ("matches" if snapshot["artifact_digest_matches"] else "MISMATCH"),
        f"Owner artifact digest: {intent['artifact_sha256']}",
        f"Owner-recorded intent state: {_quoted(intent['state'])}",
        f"Owner work status: {_quoted(snapshot['current_item']['status'])}",
        f"Intent Release: {_quoted(bound_digest)}",
        f"Current Release: {_quoted(current_digest)}",
        f"Intent/current Release comparison: {release_comparison}",
        f"Current Release check-revision currentness: {release['revision_currentness'] if release else 'unknown'}",
        f"Check revision comparison: {receipt['check_revision_comparison']}",
        "EVIDENCE CLASSES:",
        "  Report: outcome reports are not included in this read.",
        f"  Receipt: {visibility}; artifact-check comparison: {receipt['comparison']}",
        f"  Receipt evidence digest: {_quoted(receipt['evidence_digest'])}",
        "  Acknowledgment: target acknowledgment is not included.",
        "  Observation: receipt matching concerns protected artifact checks; target effect observation is unknown.",
        "  Acceptance: intent state and Decision records are owner facts; terminal work acceptance is not established here.",
        "An accepted or applied lifecycle record does not establish an observed external effect.",
        f"Decision facts: {len(snapshot['decision_facts'])}/{snapshot['decision_count']}; "
        f"complete: {snapshot['decision_facts_complete']}",
        f"Decision collection digest: {snapshot['decision_digest'] or 'unknown'}",
    ]
    for decision in snapshot["decision_facts"][:5]:
        lines.append(f"  Decision {decision['id']}: {decision['kind']}, Release {_quoted(decision['release_digest'])}")
    if len(snapshot["decision_facts"]) > 5:
        lines.append("  Further decision facts are retained in JSON.")
    lines.extend([
        f"Reference records: {snapshot['reference_count']}; complete: {snapshot['reference_facts_complete']}",
        f"Reference collection digest: {snapshot['reference_set_digest'] or 'unknown'}",
        f"Evidence obligations: {len(snapshot['evidence_obligations'])}/{snapshot['evidence_obligation_count']}; "
        f"complete: {snapshot['evidence_obligations_complete']}",
    ])
    for obligation in snapshot["evidence_obligations"][:5]:
        suffix = " [label truncated]" if obligation["label_truncated"] else ""
        lines.append(f"  {_quoted(obligation['label'])}{suffix}: mapping {obligation['mapping']}")
    if len(snapshot["evidence_obligations"]) > 5:
        lines.append("  Further obligation records are retained in JSON.")
    lines.extend([
        "MISSING LINKS / NEXT CHECKS:",
        "  Terminal Decision binding, accepting identity/time and attempt history are not included.",
        "  Obligation-to-evidence mapping and target outcome require separate evidence.",
        f"  Lease settlement: {snapshot['lease_settlement']}",
        "  Inspect mismatches and incomplete collections before making any acceptance decision.",
        "authorizes_acceptance: false",
    ])
    return "\n".join(lines)
