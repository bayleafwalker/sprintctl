"""
Maintenance operations for sprintctl.

All functions are side-effectful (they write to the DB).
The check() function is the only read-only path.
"""

import json
import os
from datetime import datetime, timedelta

from . import calc as _calc
from . import db as _db


def _force_item_done_for_carryover(conn, item_id: int, *, _m=None) -> None:
    """
    Set a work item to 'done' without going through the state machine.

    ONLY valid for carryover: items being carried forward may be in
    pending/active/blocked, none of which have an allowed transition to
    'done'. Any other caller should use db.set_work_item_status() instead.
    """
    if _m is not None and _m is not _db:
        _m.force_item_done_for_carryover(conn, item_id)
        return
    conn.execute(
        "UPDATE work_item SET status = 'done', "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (item_id,),
    )

DEFAULT_STALE_THRESHOLD = timedelta(hours=4)
_CODE_EVIDENCE_EVENT_TYPES = frozenset({
    "commit",
    "commit.created",
    "git.commit",
    "work.completed",
})
_SESSION_END_EVENT_TYPES = frozenset({"session.ended", "session.end-inferred"})


def _stale_threshold() -> timedelta:
    raw = os.environ.get("SPRINTCTL_STALE_THRESHOLD")
    if raw:
        return timedelta(hours=float(raw))
    return DEFAULT_STALE_THRESHOLD


def _pending_stale_threshold() -> "timedelta | None":
    """Returns the staleness threshold for pending items, or None (never stale)."""
    raw = os.environ.get("SPRINTCTL_PENDING_STALE_THRESHOLD")
    if raw:
        return timedelta(hours=float(raw))
    return None


def _event_payload(event: dict) -> dict:
    raw = event.get("payload") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    nested = raw.get("payload")
    return nested if isinstance(nested, dict) else raw


def _event_has_item_link(event: dict, payload: dict) -> bool:
    if event.get("work_item_id") is not None:
        return True
    if payload.get("work_item_id") is not None or payload.get("item_id") is not None:
        return True
    target = payload.get("target")
    if isinstance(target, dict) and str(target.get("ref", "")).startswith("wi:"):
        return True
    refs = payload.get("refs")
    if isinstance(refs, dict) and refs.get("work_item_id") is not None:
        return True
    if isinstance(refs, list) and any(str(ref).startswith("wi:") for ref in refs):
        return True
    return False


def _event_has_code_evidence(event: dict, payload: dict) -> bool:
    event_type = event.get("event_type")
    if event_type in _CODE_EVIDENCE_EVENT_TYPES:
        return True
    if event_type not in _SESSION_END_EVENT_TYPES:
        return False
    git = payload.get("git")
    if not isinstance(git, dict):
        git = payload
    if git.get("commits") or git.get("touched_paths") or git.get("diff_stat"):
        return True
    base = git.get("base_commit")
    head = git.get("head_commit")
    return bool(base and head and base != head)


def _truth_findings(
    sprint: dict,
    items: list[dict],
    active_reservations: list[dict],
    events: list[dict],
    risk: dict,
) -> list[dict]:
    findings: list[dict] = []
    if risk["overdue"]:
        days_overdue = abs(risk["days_remaining"])
        findings.append({
            "kind": "sprint-overdue",
            "reason_code": "active-sprint-overdue",
            "severity": "warning",
            "sprint_id": sprint["id"],
            "days_overdue": days_overdue,
            "summary": (
                f"Active sprint #{sprint['id']} is {days_overdue} day(s) overdue; "
                "review it explicitly rather than auto-closing."
            ),
        })
    if sprint["status"] == "active" and items and all(item["status"] == "done" for item in items):
        findings.append({
            "kind": "all-items-done",
            "reason_code": "active-sprint-all-items-done",
            "severity": "warning",
            "sprint_id": sprint["id"],
            "item_ids": sorted(item["id"] for item in items),
            "summary": (
                f"Active sprint #{sprint['id']} has all {len(items)} item(s) done; "
                "completion still requires an explicit sprint-close decision."
            ),
        })
    reserved_item_ids = {reservation["work_item_id"] for reservation in active_reservations}
    unreserved_item_ids = sorted(
        item["id"] for item in items
        if item["status"] == "active" and item["id"] not in reserved_item_ids
    )
    if unreserved_item_ids:
        findings.append({
            "kind": "unreserved-active-work",
            "reason_code": "active-item-without-reservation",
            "severity": "warning",
            "sprint_id": sprint["id"],
            "item_ids": unreserved_item_ids,
            "summary": (
                f"{len(unreserved_item_ids)} active item(s) have no reservation and need "
                "status triage."
            ),
        })
    unlinked_evidence = []
    for event in events:
        payload = _event_payload(event)
        if _event_has_code_evidence(event, payload) and not _event_has_item_link(event, payload):
            unlinked_evidence.append({
                "event_id": event["id"],
                "event_type": event["event_type"],
            })
    if unlinked_evidence:
        unlinked_evidence.sort(key=lambda evidence: evidence["event_id"])
        findings.append({
            "kind": "unlinked-code-evidence",
            "reason_code": "code-evidence-without-item-link",
            "severity": "warning",
            "sprint_id": sprint["id"],
            "event_ids": [evidence["event_id"] for evidence in unlinked_evidence],
            "evidence": unlinked_evidence,
            "summary": (
                f"{len(unlinked_evidence)} code-bearing event(s) have no work-item link; "
                "retain them for explicit reconciliation."
            ),
        })
    return findings


# ---------------------------------------------------------------------------
# check (read-only diagnostic)
# ---------------------------------------------------------------------------

def check(
    conn,
    sprint_id: int,
    now: datetime,
    threshold: timedelta | None = None,
    pending_threshold: "timedelta | None | str" = "env",
    *,
    _m=None,
) -> dict:
    """Return a diagnostic report for the sprint. No writes.

    pending_threshold controls staleness for pending (backlog) items:
    - "env" (default): read from SPRINTCTL_PENDING_STALE_THRESHOLD, else None
    - None: pending items are never stale
    - timedelta: custom threshold
    """
    m = _m if _m is not None else _db
    if threshold is None:
        threshold = _stale_threshold()
    if pending_threshold == "env":
        pending_threshold = _pending_stale_threshold()

    sprint = m.get_sprint(conn, sprint_id)
    if sprint is None:
        raise ValueError(f"Sprint #{sprint_id} not found")

    items = m.list_work_items(conn, sprint_id=sprint_id)
    tracks = m.list_tracks(conn, sprint_id)

    active_items = [it for it in items if it["status"] == "active"]
    risk = _calc.sprint_overrun_risk(sprint, len(active_items), now)
    active_reservations = m.list_reservations_by_sprint(conn, sprint_id, active_only=True)
    events = m.list_events(conn, sprint_id)

    stale = [
        it for it in items
        if _calc.item_staleness(it, now, threshold, pending_threshold)["is_stale"]
    ]
    stale_details = [
        {**it, **_calc.item_staleness(it, now, threshold, pending_threshold)}
        for it in stale
    ]

    track_health = {}
    items_by_track: dict[int, list[dict]] = {}
    for it in items:
        items_by_track.setdefault(it["track_id"], []).append(it)
    for track in tracks:
        track_health[track["name"]] = _calc.track_health(
            items_by_track.get(track["id"], [])
        )

    return {
        "sprint": sprint,
        "risk": risk,
        "stale_items": stale_details,
        "track_health": track_health,
        "findings": _truth_findings(sprint, items, active_reservations, events, risk),
        "threshold": threshold,
        "pending_threshold": pending_threshold,
    }


# ---------------------------------------------------------------------------
# sweep (mutating)
# ---------------------------------------------------------------------------

def sweep_stale_items(
    conn,
    sprint_id: int,
    now: datetime,
    threshold: timedelta | None = None,
    *,
    _m=None,
) -> list[dict]:
    """
    Transition stale active items to blocked. Emit a system event per item.
    Returns the list of items that were blocked.
    """
    m = _m if _m is not None else _db
    if threshold is None:
        threshold = _stale_threshold()

    items = m.list_work_items(conn, sprint_id=sprint_id, status="active")
    affected = []
    for item in items:
        info = _calc.item_staleness(item, now, threshold)
        if info["is_stale"]:
            m.set_work_item_status(conn, item["id"], "blocked")
            m.create_event(
                conn,
                sprint_id,
                actor="maintain-sweep",
                event_type="auto-blocked-stale",
                source_type="system",
                work_item_id=item["id"],
                payload={
                    "idle_seconds": info["idle_seconds"],
                    "threshold_seconds": int(threshold.total_seconds()),
                },
            )
            affected.append(item)
    return affected




def sweep(
    conn,
    sprint_id: int,
    now: datetime,
    threshold: timedelta | None = None,
    auto_close: bool = False,
    *,
    _m=None,
) -> dict:
    """
    Execute all sweep actions for a sprint. Returns a summary dict.

    Actions:
    - Stale active items → blocked (with system event)
    - Reservations idle longer than the operator's ``interrupt_after``
      policy (default seven days) are interrupted. This happens because a
      sweep was run, never because time passed.
    - Auto-close overdue sprint with no active items (opt-in via auto_close)
    """
    m = _m if _m is not None else _db
    if threshold is None:
        threshold = _stale_threshold()

    blocked = sweep_stale_items(conn, sprint_id, now, threshold, _m=m)
    interrupted_reservations = m.sweep_stale_reservations(
        conn, now=now.strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    auto_closed = False
    if auto_close:
        sprint = m.get_sprint(conn, sprint_id)
        active_remaining = m.list_work_items(conn, sprint_id=sprint_id, status="active")
        risk = _calc.sprint_overrun_risk(sprint, len(active_remaining), now)
        if risk["overdue"] and not active_remaining:
            m.set_sprint_status(conn, sprint_id, "closed")
            m.create_event(
                conn,
                sprint_id,
                actor="maintain-sweep",
                event_type="auto-closed-overdue",
                source_type="system",
                payload={"days_overdue": abs(risk["days_remaining"])},
            )
            auto_closed = True

    return {
        "blocked_items": blocked,
        "stale_reservations_interrupted": interrupted_reservations,
        "auto_closed": auto_closed,
    }


# ---------------------------------------------------------------------------
# carryover
# ---------------------------------------------------------------------------

def carryover(conn, from_sprint_id: int, to_sprint_id: int, *, _m=None) -> list[dict]:
    """
    Move incomplete items (pending/active/blocked) from source sprint to
    target sprint. Each original item is marked done with a carryover payload.
    New items are created in the target sprint preserving track name and title.

    Returns a list of new item dicts created in the target sprint.
    """
    m = _m if _m is not None else _db
    source = m.get_sprint(conn, from_sprint_id)
    if source is None:
        raise ValueError(f"Source sprint #{from_sprint_id} not found")
    target = m.get_sprint(conn, to_sprint_id)
    if target is None:
        raise ValueError(f"Target sprint #{to_sprint_id} not found")
    if from_sprint_id == to_sprint_id:
        raise ValueError("Source and target sprint must differ")

    incomplete_statuses = {"pending", "active", "blocked"}
    items = m.list_work_items(conn, sprint_id=from_sprint_id)
    incomplete = [it for it in items if it["status"] in incomplete_statuses]

    created = []
    for item in incomplete:
        # Create matching item in target sprint (track created if absent)
        track_id = m.get_or_create_track(conn, to_sprint_id, item["track_name"])
        new_id = m.create_work_item(
            conn,
            to_sprint_id,
            track_id,
            item["title"],
            description=item.get("description", ""),
            assignee=item.get("assignee"),
        )

        # Emit carryover event on target sprint
        m.create_event(
            conn,
            to_sprint_id,
            actor="maintain-carryover",
            event_type="carryover",
            source_type="system",
            work_item_id=new_id,
            payload={
                "from_sprint_id": from_sprint_id,
                "original_item_id": item["id"],
                "original_status": item["status"],
            },
        )

        _force_item_done_for_carryover(conn, item["id"], _m=m)
        m.create_event(
            conn,
            from_sprint_id,
            actor="maintain-carryover",
            event_type="carried-out",
            source_type="system",
            work_item_id=item["id"],
            payload={
                "to_sprint_id": to_sprint_id,
                "new_item_id": new_id,
            },
        )

        created.append(m.get_work_item(conn, new_id))

    if m is _db:
        conn.commit()
    return created
