"""Canonical v1 ``usage --context`` aggregate.

Both the legacy CLI and the served work application call this module.  Keeping
the derivation here prevents a served client from reconstructing a snapshot by
making individually consistent but collectively racy reads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from . import contracts, maintain


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _summarize_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    tags = payload.get("tags")
    return {
        "id": event["id"], "event_id": event["id"],
        "event_type": event["event_type"], "created_at": event["created_at"],
        "actor": event["actor"], "work_item_id": event.get("work_item_id"),
        "summary": payload.get("summary") or event["event_type"],
        "detail": payload.get("detail"), "tags": tags if isinstance(tags, list) else [],
    }


def _waiting(store: Any, sprint_id: int, backend: Any) -> list[dict[str, Any]]:
    waiting = []
    for item in backend.list_work_items(store, sprint_id=sprint_id, status="pending"):
        unresolved = [
            blocker for blocker in backend.list_deps_blocking(store, item["id"])
            if blocker["blocker_status"] != "done"
        ]
        if unresolved:
            waiting.append({
                "id": item["id"], "title": item["title"], "track": item["track_name"],
                "assignee": item.get("assignee"), "unresolved_blockers": len(unresolved),
                "unresolved_blocker_ids": [row["item_id"] for row in unresolved],
                "unresolved_blocker_titles": [row["blocker_title"] for row in unresolved],
            })
    return waiting


def _conflicts(*, active_reservations, active_unclaimed_items, blocked_items, stale_items, waiting, now):
    conflicts = []
    stale = [row for row in active_reservations if row.get("stale")]
    if stale:
        conflicts.append({"kind": "stale-reservation", "severity": "warning", "summary": f"{len(stale)} active reservation(s) have been idle for four hours.", "reservation_ids": [row["id"] for row in stale], "item_ids": [row["work_item_id"] for row in stale]})
    if active_unclaimed_items:
        conflicts.append({"kind": "unclaimed-active-work", "reason_code": "active-item-without-live-claim", "severity": "warning", "summary": f"{len(active_unclaimed_items)} active item(s) have no live claim and need resume, handoff, or status triage.", "item_ids": [row["id"] for row in active_unclaimed_items]})
    if waiting:
        conflicts.append({"kind": "dependency-blocked", "severity": "warning", "summary": f"{len(waiting)} pending item(s) are waiting on unresolved blockers.", "item_ids": [row["id"] for row in waiting], "blocker_ids": sorted({bid for row in waiting for bid in row["unresolved_blocker_ids"]})})
    if blocked_items:
        conflicts.append({"kind": "blocked-work", "severity": "warning", "summary": f"{len(blocked_items)} item(s) are explicitly blocked and need triage.", "item_ids": [row["id"] for row in blocked_items]})
    if stale_items:
        conflicts.append({"kind": "stale-work", "severity": "warning", "summary": f"{len(stale_items)} item(s) are stale and may be drifting out of date.", "item_ids": [row["id"] for row in stale_items]})
    return conflicts


def _next_action(*, active_reservations, active_unclaimed_items, conflicts, ready_items, blocked_items, stale_items, waiting):
    if conflicts:
        first = conflicts[0]
        if first["kind"] == "stale-reservation":
            return {"kind": "review-stale-reservation", "summary": "Review or reassign the stale reservation.", "reservation_id": first["reservation_ids"][0], "item_id": first["item_ids"][0], "reason": first["summary"]}
        if first["kind"] == "unclaimed-active-work":
            item = active_unclaimed_items[0]
            return {"kind": "resume-unclaimed-active-item", "summary": f"Resume or triage active item #{item['id']} because it has no live claim.", "item_id": item["id"], "reason": first["summary"]}
        if first["kind"] == "dependency-blocked":
            item = waiting[0]
            return {"kind": "unblock-dependent-work", "summary": f"Resolve blocker #{item['unresolved_blocker_ids'][0]} to unblock item #{item['id']}.", "item_id": item["id"], "blocker_item_id": item["unresolved_blocker_ids"][0], "reason": first["summary"]}
        if first["kind"] == "blocked-work":
            item = blocked_items[0]
            return {"kind": "triage-blocked-item", "summary": f"Triage blocked item #{item['id']} before pulling new work.", "item_id": item["id"], "reason": first["summary"]}
        if first["kind"] == "stale-work":
            item = stale_items[0]
            return {"kind": "refresh-stale-item", "summary": f"Refresh stale item #{item['id']} before it drifts further.", "item_id": item["id"], "reason": first["summary"]}
    if active_reservations:
        row = active_reservations[0]
        return {"kind": "inspect-active-reservation", "summary": f"Inspect reserved item #{row['work_item_id']} before starting new work.", "reservation_id": row["id"], "item_id": row["work_item_id"], "reason": "Active reserved work already exists in this sprint."}
    if ready_items:
        item = ready_items[0]
        return {"kind": "start-ready-item", "summary": f"Start ready item #{item['id']} because it is unblocked and no active reservations are open.", "item_id": item["id"], "reason": "Ready work is available now."}
    if waiting:
        item = waiting[0]
        return {"kind": "resolve-blocker", "summary": f"Resolve blocker #{item['unresolved_blocker_ids'][0]} to unblock item #{item['id']}.", "item_id": item["id"], "blocker_item_id": item["unresolved_blocker_ids"][0], "reason": "All pending work is currently waiting on dependencies."}
    return {"kind": "no-action", "summary": "No immediate action is suggested from current sprint state.", "reason": "There is no ready, active, blocked, or stale work to prioritize."}


def build_context_contract(store: Any, sprint: dict[str, Any], now: datetime, *, backend: Any) -> dict[str, Any]:
    """Build the frozen ContextContract v1 from one backend snapshot."""
    active_reservations = backend.list_reservations_by_sprint(store, sprint["id"], active_only=True)
    report = maintain.check(store, sprint["id"], now, _m=backend)
    stale_items = [{"id": item["id"], "title": item["title"], "status": item["status"], "track": item["track_name"], "idle_seconds": item["idle_seconds"]} for item in report["stale_items"]]
    all_items = backend.list_work_items(store, sprint_id=sprint["id"])
    blocked_items = [{"id": item["id"], "title": item["title"], "track": item["track_name"]} for item in all_items if item["status"] == "blocked"]
    active_items = [{"id": item["id"], "title": item["title"], "track": item["track_name"]} for item in all_items if item["status"] == "active"]
    active_unclaimed = [item for item in active_items if item["id"] not in {row["work_item_id"] for row in active_reservations}]
    ready_items = [{"id": item["id"], "title": item["title"], "track": item["track_name"]} for item in backend.get_ready_items(store, sprint["id"])]
    waiting = _waiting(store, sprint["id"], backend)
    recent_decisions = [_summarize_event(event) for event in reversed(backend.list_knowledge_candidates(store, sprint["id"])[-5:])]
    conflicts = _conflicts(active_reservations=active_reservations, active_unclaimed_items=active_unclaimed, blocked_items=blocked_items, stale_items=stale_items, waiting=waiting, now=now)
    conflicts.extend(row for row in report["findings"] if row["reason_code"] != "active-item-without-live-claim")
    return contracts.ContextContract(
        sprint={key: sprint.get(key) for key in ("id", "name", "goal", "status", "start_date", "end_date")},
        summary={"total": len(all_items), "done": sum(item["status"] == "done" for item in all_items), "active": len(active_items), "pending": sum(item["status"] == "pending" for item in all_items), "blocked": len(blocked_items), "stale": len(stale_items), "ready": len(ready_items), "waiting_on_dependencies": len(waiting), "active_reservations": len(active_reservations), "active_unreserved": len(active_unclaimed)},
        active_reservations=active_reservations, active_unclaimed_items=active_unclaimed, conflicts=conflicts,
        ready_items=ready_items, blocked_items=blocked_items, stale_items=stale_items,
        recent_decisions=recent_decisions,
        next_action=_next_action(active_reservations=active_reservations, active_unclaimed_items=active_unclaimed, conflicts=conflicts, ready_items=ready_items, blocked_items=blocked_items, stale_items=stale_items, waiting=waiting),
    ).to_dict()
