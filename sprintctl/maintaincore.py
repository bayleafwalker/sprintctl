"""Shared carryover workflow; each participant belongs to one transaction."""
from dataclasses import dataclass
from typing import Callable

from . import contracts, decisions, calc
from datetime import datetime, timedelta


@dataclass(frozen=True)
class CarryoverParticipant:
    """Noncommitting operations supplied by the transaction-owning backend.

    Creation uses the existing track/work-item cores, and ``decide`` uses the
    backend's locked Decision primitive including its backend consequences.
    """
    get_sprint: Callable[[int], dict | None]
    lock_item: Callable[[int], dict | None]
    get_track: Callable[[int], dict | None]
    create_track: Callable[[int, str], int]
    create_item: Callable[..., int]
    insert_event: Callable[..., int]
    decide: Callable[[dict, dict], dict]
    get_item: Callable[[int], dict | None]


def carryover_item(p: CarryoverParticipant, source: int, target: int, item_id: int) -> dict | None:
    """Carry one still-open source, or skip a stale selection without writes."""
    if source == target:
        raise ValueError("Source and target sprint must differ")
    for label, sprint_id in (("Source", source), ("Target", target)):
        if p.get_sprint(sprint_id) is None:
            raise ValueError(f"{label} sprint #{sprint_id} not found")
    # Lock and re-read before creating even a target track. A competing move
    # sees the committed terminal source, so it cannot leave another copy.
    item = p.lock_item(item_id)
    if item is None or item["sprint_id"] != source or item["status"] not in {"pending", "active", "blocked"}:
        return None
    track = p.get_track(item["track_id"])
    track_id = p.create_track(target, track["name"])
    new_id = p.create_item(target, track_id, item["title"],
                           description=item.get("description", ""), assignee=item.get("assignee"))

    def generic_event(sprint_id, kind, *, work_item_id, payload):
        contracts.require_generic_event_write_allowed(kind)
        return p.insert_event(sprint_id, "maintain-carryover", kind,
                              source_type="system", work_item_id=work_item_id, payload=payload)

    generic_event(target, "carryover", work_item_id=new_id,
                  payload={"from_sprint_id": source, "original_item_id": item_id,
                           "original_status": item["status"]})
    decision = decisions.normalize_decision(
        "supersede", actor="maintain-carryover",
        rationale=f"carried over to sprint #{target} as item #{new_id}",
        superseded_by_item_id=new_id,
    )
    recorded = p.decide(item, decision)
    p.insert_event(source, decision["actor"], decisions.ITEM_DECIDED_EVENT_TYPE,
                   work_item_id=item_id,
                   payload=decisions.decided_event_payload(recorded, None, legacy_remark=False))
    generic_event(source, "carried-out", work_item_id=item_id,
                  payload={"to_sprint_id": target, "new_item_id": new_id})
    # Resolve the response before the backend commits; failed result reads
    # must not turn a reported failure into a silently committed copy.
    copied = p.get_item(new_id)
    if copied is None:
        raise ValueError(f"Carried item #{new_id} could not be read")
    return copied


@dataclass(frozen=True)
class StaleItemParticipant:
    """Noncommitting status/event operations in one backend-owned transaction."""
    lock_item: Callable[[int], dict | None]
    get_item: Callable[[int], dict | None]
    get_track: Callable[[int], dict | None]
    block_item: Callable[[int], None]
    insert_event: Callable[..., int]


def block_stale_item(
    p: StaleItemParticipant, sprint_id: int, item_id: int,
    now: datetime, threshold: timedelta,
) -> dict | None:
    """Recheck current work and return its public pretransition projection."""
    locked = p.lock_item(item_id)
    if locked is None or locked["sprint_id"] != sprint_id or locked["status"] != "active":
        return None
    item = p.get_item(item_id)
    if item is None:
        raise ValueError(f"Item #{item_id} projection could not be read")
    track = p.get_track(item["track_id"])
    if track is None:
        raise ValueError(f"Item #{item_id} track could not be read")
    # list_work_items includes track_name; get_work_item alone does not.
    # Preserve that public pretransition projection using current locked data.
    item = {**item, "track_name": track["name"]}
    info = calc.item_staleness(item, now, threshold)
    if not info["is_stale"]:
        return None
    contracts.require_generic_event_write_allowed("auto-blocked-stale")
    p.block_item(item_id)
    p.insert_event(
        sprint_id, "maintain-sweep", "auto-blocked-stale",
        source_type="system", work_item_id=item_id,
        payload={"idle_seconds": info["idle_seconds"],
                 "threshold_seconds": int(threshold.total_seconds())},
    )
    return item
