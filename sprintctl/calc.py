from datetime import datetime, timedelta
from typing import Optional

DEFAULT_STALE_THRESHOLD = timedelta(hours=4)
# pending items are untouched backlog — suppress staleness by default.
# Set SPRINTCTL_PENDING_STALE_THRESHOLD (hours) to enable a long-idle check.
DEFAULT_PENDING_STALE_THRESHOLD: Optional[timedelta] = None


def _naive_utc(dt: datetime) -> datetime:
    """Strip tzinfo so comparisons with SQLite-sourced naive datetimes always work."""
    return dt.replace(tzinfo=None)


def item_staleness(
    item: dict,
    now: datetime,
    threshold: timedelta = DEFAULT_STALE_THRESHOLD,
    pending_threshold: Optional[timedelta] = DEFAULT_PENDING_STALE_THRESHOLD,
) -> dict:
    """Returns staleness info for a single work item.

    - active items: stale when idle > threshold
    - pending items: stale when idle > pending_threshold (None = never stale)
    - done/blocked: never stale
    """
    updated = datetime.fromisoformat(item["updated_at"]).replace(tzinfo=None)
    delta = _naive_utc(now) - updated
    status = item["status"]

    if status == "active":
        is_stale = delta > threshold
    elif status == "pending":
        is_stale = pending_threshold is not None and delta > pending_threshold
    else:
        is_stale = False

    return {
        "item_id": item["id"],
        "idle_seconds": int(delta.total_seconds()),
        "is_stale": is_stale,
        "status": status,
    }


def track_health(items: list[dict]) -> dict:
    """Summarise status distribution for a track."""
    counts = {"pending": 0, "active": 0, "done": 0, "blocked": 0}
    for it in items:
        counts[it["status"]] += 1
    total = len(items)
    return {
        "total": total,
        "counts": counts,
        "blocked_ratio": counts["blocked"] / total if total else 0.0,
        "done_ratio": counts["done"] / total if total else 0.0,
    }


def sprint_overrun_risk(sprint: dict, active_items: int, now: datetime) -> dict:
    """Flag if sprint is approaching end with significant open work."""
    end = datetime.fromisoformat(sprint["end_date"]).replace(tzinfo=None)
    remaining = end - _naive_utc(now)
    return {
        "days_remaining": remaining.days,
        "active_items": active_items,
        "at_risk": remaining.days <= 2 and active_items > 0,
        "overdue": remaining.days < 0 and sprint["status"] == "active",
    }
