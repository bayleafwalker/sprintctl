"""Click-free construction of the sprint handoff bundle contract.

The served work application and the legacy CLI share this module.  The
application is deliberately given client-observed git context: a service
process must never inspect its own checkout and present that as the caller's
worktree state.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any, Mapping

from . import context_contract, contracts
from . import reservation as _reservation
from . import reservation_policy as _policy


def _previous_handoff_generated(store: Any, sprint_id: int, backend: Any) -> dict | None:
    for event in reversed(backend.list_events(store, sprint_id)):
        if event["event_type"] == "handoff-generated":
            return event
    return None


def checkpoint_from(git_context: Mapping[str, Any] | None, generated_at: str) -> dict | None:
    """The durable part of a handed-off session's position, and nothing more.

    A checkpoint is `git-commit + evidence`: the revision the work was at, the branch it
    was on, the worktree it was in, and how much was uncommitted. It is deliberately not
    the diff -- Git already holds the content, and a bundle that carried it would be
    manufacturing a second copy of something Git owns. `dirty_file_count` is the evidence
    a resumer needs: a checkpoint taken over a dirty tree does not identify the state of
    the work by its revision alone, and the resumer has to be told so.
    """
    if not git_context or not git_context.get("sha"):
        return None
    dirty = git_context.get("dirty_files") or []
    return {
        "sha": git_context["sha"],
        "branch": git_context.get("branch"),
        "worktree": git_context.get("worktree"),
        "dirty_file_count": len(dirty),
        "recorded_at": generated_at,
    }


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, Mapping) else None


def _last_recorded_checkpoint(all_events: list[dict]) -> dict | None:
    """The most recent checkpoint any handoff recorded -- not the most recent handoff.

    The two differ, and the difference is load-bearing. A resuming process has no
    worktree, so the bundle it generates carries no git context and records no
    checkpoint; if this read only the latest handoff event, the act of resuming would
    erase the very position it had just read. So it scans back to the last handoff that
    actually observed a revision. A checkpoint is superseded by a newer checkpoint, never
    by the absence of one.

    Bundles written before checkpoints were persisted have no `checkpoint` key, and are
    skipped rather than treated as an error: the field is additive in both directions,
    which is what lets it roll out without a flag day.
    """
    for event in reversed(all_events):
        if event.get("event_type") != "handoff-generated":
            continue
        payload = _event_payload(event)
        checkpoint = payload.get("checkpoint") if payload else None
        if isinstance(checkpoint, Mapping):
            return dict(checkpoint)
    return None


def _after(value: Any, cutoff: datetime | None) -> bool:
    """Is this timestamp later than the cutoff, whatever type the backend returned?

    Two backends answer with two types: sqlite returns ISO strings, pg returns
    ``datetime`` (``pg.py`` says so in its own header, and ``reservation.display``
    copies the row through without converting). Comparing them directly raises
    ``TypeError: '>' not supported between 'datetime.datetime' and 'str'``, which the
    served transport reports as ``operation-handler-failed`` -- and it only reaches the
    reservation branch when a sprint has an active reservation, so the handoff bundle
    failed exactly when a session had been interrupted mid-work and needed it most.

    Comparing instants rather than strings also fixes a quieter error the string form
    had: ``"...:47Z"`` sorts after ``"...:47.5Z"`` because ``Z`` > ``.``, so a whole-
    second timestamp counted as later than a fractional one inside the same second.
    """
    if value is None or cutoff is None:
        return False
    try:
        return _reservation.parse_time(value) > cutoff
    except (TypeError, ValueError):
        return False


def _delta_since_last_handoff(*, previous_handoff: dict | None, items: list[dict], all_events: list[dict], active_reservations: list[dict]) -> dict:
    previous_handoff_at = previous_handoff["created_at"] if previous_handoff else None
    if previous_handoff_at is None:
        return {"previous_handoff_at": None, "item_ids_touched": [], "event_count": len(all_events), "reservation_ids_touched": []}
    try:
        cutoff = _reservation.parse_time(previous_handoff_at)
    except (TypeError, ValueError):
        cutoff = None
    return {
        "previous_handoff_at": previous_handoff_at,
        "item_ids_touched": [item["id"] for item in items if _after(item.get("updated_at"), cutoff)],
        "event_count": sum(1 for event in all_events if event["id"] > previous_handoff["id"]),
        "reservation_ids_touched": [
            row["id"] for row in active_reservations if _after(row.get("last_activity_at"), cutoff)
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
        work={"active_items": active_items, "active_unreserved_items": context["active_unreserved_items"], "ready_items": context["ready_items"], "blocked_items": context["blocked_items"], "stale_items": context["stale_items"]},
        recent_decisions=context["recent_decisions"], recent_events=[context_contract._summarize_event(event) for event in recent_events], next_action=context["next_action"],
        delta_since_last_handoff=_delta_since_last_handoff(previous_handoff=previous_handoff, items=items_with_refs, all_events=all_events, active_reservations=context["active_reservations"]),
        freshness={"generated_at": generated_at, "previous_handoff_at": previous_handoff["created_at"] if previous_handoff else None, "stale_item_count": len(context["stale_items"]), "active_reservation_count": len(context["active_reservations"]), "dirty_file_count": len(git_context["dirty_files"]) if git_context else 0},
        evidence={"dirty_files": git_context["dirty_files"] if git_context else [], "items_with_refs": sum(1 for item in items_with_refs if item.get("refs")), "total_refs": sum(len(item.get("refs", [])) for item in items_with_refs), "recent_event_count": len(recent_events), "recent_decision_count": len(context["recent_decisions"]), "validation_outcomes": []},
        git_context=git_context,
        # `git_context` is what THIS caller observes and the server echoes back; it is
        # null for a caller that has no worktree, which is every resuming process. The
        # checkpoint is what the LAST session recorded, and it is the half that has to
        # survive the interruption -- so a fresh process reads its predecessor's position
        # here rather than being handed back its own emptiness.
        last_checkpoint=_last_recorded_checkpoint(all_events),
        reservation_model={"ownership_proof": None, "reassign_command": "sprintctl reservation reassign",
                           "exclusive": False, **_policy.describe()},
        resume_instructions=["Read this handoff bundle first.", "Refresh live state with 'sprintctl usage --context --json'.", "List active reservations with 'sprintctl reservation list --all --json'."],
        agent_shutdown_protocol={"required_before_termination": ["Reassign or release each active reservation.", "Run 'sprintctl handoff' to produce a new bundle."], "resumption_hint": "Incoming agents may reserve or reassign without a credential."},
        items=items_with_refs, events=recent_events,
    ).to_dict()


def record_handoff_generated(store: Any, sprint_id: int, bundle: dict, *, backend: Any, actor: str) -> int:
    """Append one non-deduplicated generation event; actor is authenticated upstream.

    The checkpoint is persisted here because nothing else does. Until 2026-08-29 this
    event recorded only that a handoff had happened -- the bundle's own `git_context` was
    computed by the client, sent to the server, echoed back, and dropped -- so a session's
    position died with the process that held it. That is what made resumption unprovable:
    a fresh process could recover the session identity and the work authority and still
    have no idea which revision the work was at.
    """
    checkpoint = checkpoint_from(bundle.get("git_context"), bundle["generated_at"])
    payload = {
        "summary": f"Handoff bundle generated for sprint #{sprint_id}",
        "detail": "Generated a working-memory handoff bundle for the next session.",
        "bundle_version": bundle["bundle_version"],
        "events_limit": bundle["generated_from"]["events_limit"],
    }
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    return backend.create_event(store, sprint_id=sprint_id, actor=actor, event_type="handoff-generated", source_type="system", payload=payload)
