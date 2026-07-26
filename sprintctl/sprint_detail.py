"""Canonical snapshot payload for ``sprintctl sprint show --detail``.

The aggregate deliberately lives below both Click and the Vuoro adapter so a
served read can assemble the same contract inside one server-side snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import calc, maintain


def build_sprint_show_detail(
    store: Any,
    sprint: dict[str, Any],
    *,
    backend: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the exact ``sprint show --detail --json`` payload.

    Callers that need a coherent remote view must provide a snapshot-bound
    store.  This helper never opens a connection or reads client-local state.
    """

    observed_at = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "id": sprint["id"],
        "name": sprint["name"],
        "goal": sprint["goal"],
        "start_date": sprint["start_date"],
        "end_date": sprint["end_date"],
        "status": sprint["status"],
        "kind": sprint["kind"],
    }
    items = backend.list_work_items(store, sprint_id=sprint["id"])
    tracks = backend.list_tracks(store, sprint["id"])
    active_items = [item for item in items if item["status"] == "active"]
    risk = calc.sprint_overrun_risk(sprint, len(active_items), observed_at)
    pending_threshold = maintain._pending_stale_threshold()
    stale_count = sum(
        1
        for item in items
        if calc.item_staleness(
            item, observed_at, pending_threshold=pending_threshold
        )["is_stale"]
    )
    items_by_track: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        items_by_track.setdefault(item["track_id"], []).append(item)
    track_health = {
        track["name"]: calc.track_health(items_by_track.get(track["id"], []))
        for track in tracks
    }
    active_takeups = backend.list_active_takeups(store, sprint["id"])
    payload["detail"] = {
        "risk": risk,
        "stale_count": stale_count,
        "track_health": track_health,
        "takeup": {"active_count": len(active_takeups), "active": active_takeups},
    }
    return payload
