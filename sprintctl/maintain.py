"""
Maintenance operations for sprintctl.

All functions are side-effectful (they write to the DB).
The check() function is the only read-only path.
"""

import os
from datetime import datetime, timedelta

from . import calc as _calc
from . import db as _db


def _force_item_done_for_carryover(conn, item_id: int) -> None:
    """
    Set a work item to 'done' without going through the state machine.

    ONLY valid for carryover: items being carried forward may be in
    pending/active/blocked, none of which have an allowed transition to
    'done'. Any other caller should use db.set_work_item_status() instead.
    """
    conn.execute(
        "UPDATE work_item SET status = 'done', "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (item_id,),
    )

DEFAULT_STALE_THRESHOLD = timedelta(hours=4)


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


# ---------------------------------------------------------------------------
# check (read-only diagnostic)
# ---------------------------------------------------------------------------

def check(
    conn,
    sprint_id: int,
    now: datetime,
    threshold: timedelta | None = None,
    pending_threshold: "timedelta | None | str" = "env",
) -> dict:
    """Return a diagnostic report for the sprint. No writes.

    pending_threshold controls staleness for pending (backlog) items:
    - "env" (default): read from SPRINTCTL_PENDING_STALE_THRESHOLD, else None
    - None: pending items are never stale
    - timedelta: custom threshold
    """
    if threshold is None:
        threshold = _stale_threshold()
    if pending_threshold == "env":
        pending_threshold = _pending_stale_threshold()

    sprint = _db.get_sprint(conn, sprint_id)
    if sprint is None:
        raise ValueError(f"Sprint #{sprint_id} not found")

    items = _db.list_work_items(conn, sprint_id=sprint_id)
    tracks = _db.list_tracks(conn, sprint_id)

    active_items = [it for it in items if it["status"] == "active"]
    risk = _calc.sprint_overrun_risk(sprint, len(active_items), now)

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
) -> list[dict]:
    """
    Transition stale active items to blocked. Emit a system event per item.
    Returns the list of items that were blocked.
    """
    if threshold is None:
        threshold = _stale_threshold()

    items = _db.list_work_items(conn, sprint_id=sprint_id, status="active")
    affected = []
    for item in items:
        info = _calc.item_staleness(item, now, threshold)
        if info["is_stale"]:
            _db.set_work_item_status(conn, item["id"], "blocked")
            _db.create_event(
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
) -> dict:
    """
    Execute all sweep actions for a sprint. Returns a summary dict.

    Actions:
    - Stale active items → blocked (with system event)
    - Auto-close overdue sprint with no active items (opt-in via auto_close)
    """
    if threshold is None:
        threshold = _stale_threshold()

    blocked = sweep_stale_items(conn, sprint_id, now, threshold)

    auto_closed = False
    if auto_close:
        sprint = _db.get_sprint(conn, sprint_id)
        active_remaining = _db.list_work_items(conn, sprint_id=sprint_id, status="active")
        risk = _calc.sprint_overrun_risk(sprint, len(active_remaining), now)
        if risk["overdue"] and not active_remaining:
            _db.set_sprint_status(conn, sprint_id, "closed")
            _db.create_event(
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
        "auto_closed": auto_closed,
    }


# ---------------------------------------------------------------------------
# carryover
# ---------------------------------------------------------------------------

def carryover(conn, from_sprint_id: int, to_sprint_id: int) -> list[dict]:
    """
    Move incomplete items (pending/active/blocked) from source sprint to
    target sprint. Each original item is marked done with a carryover payload.
    New items are created in the target sprint preserving track name and title.

    Returns a list of new item dicts created in the target sprint.
    """
    source = _db.get_sprint(conn, from_sprint_id)
    if source is None:
        raise ValueError(f"Source sprint #{from_sprint_id} not found")
    target = _db.get_sprint(conn, to_sprint_id)
    if target is None:
        raise ValueError(f"Target sprint #{to_sprint_id} not found")
    if from_sprint_id == to_sprint_id:
        raise ValueError("Source and target sprint must differ")

    incomplete_statuses = {"pending", "active", "blocked"}
    items = _db.list_work_items(conn, sprint_id=from_sprint_id)
    incomplete = [it for it in items if it["status"] in incomplete_statuses]

    created = []
    for item in incomplete:
        # Create matching item in target sprint (track created if absent)
        track_id = _db.get_or_create_track(conn, to_sprint_id, item["track_name"])
        new_id = _db.create_work_item(
            conn,
            to_sprint_id,
            track_id,
            item["title"],
            description=item.get("description", ""),
            assignee=item.get("assignee"),
        )

        # Emit carryover event on target sprint
        _db.create_event(
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

        _force_item_done_for_carryover(conn, item["id"])
        _db.create_event(
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

        created.append(_db.get_work_item(conn, new_id))

    conn.commit()
    return created
