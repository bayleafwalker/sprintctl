"""Click-free construction of the sprint handoff bundle contract.

The served work application and the legacy CLI share this module.  The
application is deliberately given client-observed git context: a service
process must never inspect its own checkout and present that as the caller's
worktree state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import context_contract, contracts


def _previous_handoff_generated(store: Any, sprint_id: int, backend: Any) -> dict | None:
    for event in reversed(backend.list_events(store, sprint_id)):
        if event["event_type"] == "handoff-generated":
            return event
    return None


def _delta_since_last_handoff(*, previous_handoff: dict | None, items: list[dict], all_events: list[dict], active_reservations: list[dict]) -> dict:
    previous_handoff_at = previous_handoff["created_at"] if previous_handoff else None
    if previous_handoff_at is None:
        return {"previous_handoff_at": None, "item_ids_touched": [], "event_count": len(all_events), "reservation_ids_touched": []}
    return {
        "previous_handoff_at": previous_handoff_at,
        "item_ids_touched": [item["id"] for item in items if item["updated_at"] > previous_handoff_at],
        "event_count": sum(1 for event in all_events if event["id"] > previous_handoff["id"]),
        "reservation_ids_touched": [
            row["id"] for row in active_reservations
            if row.get("last_activity_at") and row["last_activity_at"] > previous_handoff_at
        ],
    }


def build_handoff_bundle(store: Any, sprint: dict, events_limit: int, *, backend: Any, version: str, git_context: dict | None = None, now: datetime | None = None) -> dict:
    """Build the canonical bundle without Click, transport, or filesystem I/O."""
    now = now or datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    context = context_contract.build_context_contract(store, sprint, now, backend=backend)
    items = backend.list_work_items(store, sprint_id=sprint["id"])
    items_with_refs = []
    for item in items:
        enriched = {**item}
        refs = backend.list_refs(store, item["id"])
        if refs:
            enriched["refs"] = refs
        items_with_refs.append(enriched)
    recent_events = backend.list_events_limited(store, sprint["id"], limit=events_limit)
    all_events = backend.list_events(store, sprint["id"])
    previous_handoff = _previous_handoff_generated(store, sprint["id"], backend)
    active_items = [{"id": item["id"], "title": item["title"], "track": item["track_name"]} for item in items if item["status"] == "active"]
    return contracts.HandoffBundle(
        sprintctl_version=version, generated_at=generated_at,
        generated_from={"command": "sprintctl handoff", "events_limit": events_limit},
        sprint=dict(sprint), summary=context["summary"], active_reservations=context["active_reservations"], conflicts=context["conflicts"],
        work={"active_items": active_items, "active_unclaimed_items": context["active_unclaimed_items"], "ready_items": context["ready_items"], "blocked_items": context["blocked_items"], "stale_items": context["stale_items"]},
        recent_decisions=context["recent_decisions"], recent_events=[context_contract._summarize_event(event) for event in recent_events], next_action=context["next_action"],
        delta_since_last_handoff=_delta_since_last_handoff(previous_handoff=previous_handoff, items=items_with_refs, all_events=all_events, active_reservations=context["active_reservations"]),
        freshness={"generated_at": generated_at, "previous_handoff_at": previous_handoff["created_at"] if previous_handoff else None, "stale_item_count": len(context["stale_items"]), "active_reservation_count": len(context["active_reservations"]), "dirty_file_count": len(git_context["dirty_files"]) if git_context else 0},
        evidence={"dirty_files": git_context["dirty_files"] if git_context else [], "items_with_refs": sum(1 for item in items_with_refs if item.get("refs")), "total_refs": sum(len(item.get("refs", [])) for item in items_with_refs), "recent_event_count": len(recent_events), "recent_decision_count": len(context["recent_decisions"]), "validation_outcomes": []},
        git_context=git_context,
        reservation_model={"ownership_proof": None, "reassign_command": "sprintctl reservation reassign", "stale_after_hours": 4},
        resume_instructions=["Read this handoff bundle first.", "Refresh live state with 'sprintctl usage --context --json'.", "List active reservations with 'sprintctl reservation list --all --json'."],
        agent_shutdown_protocol={"required_before_termination": ["Reassign or release each active reservation.", "Run 'sprintctl handoff' to produce a new bundle."], "resumption_hint": "Incoming agents may reserve or reassign without a credential."},
        items=items_with_refs, events=recent_events,
    ).to_dict()


def record_handoff_generated(store: Any, sprint_id: int, bundle: dict, *, backend: Any, actor: str) -> int:
    """Append one non-deduplicated generation event; actor is authenticated upstream."""
    return backend.create_event(store, sprint_id=sprint_id, actor=actor, event_type="handoff-generated", source_type="system", payload={"summary": f"Handoff bundle generated for sprint #{sprint_id}", "detail": "Generated a working-memory handoff bundle for the next session.", "bundle_version": bundle["bundle_version"], "events_limit": bundle["generated_from"]["events_limit"]})
