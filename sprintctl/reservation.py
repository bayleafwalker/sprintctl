"""Credential-free advisory reservations.

Reservations deliberately do not authorize item mutations.  They are a small,
durable coordination ledger: a conflicting execute reservation is refused by
default, while an explicit override records the interruption before creating
the replacement.  This module is intentionally SQL-only so the SQLite and
PostgreSQL facades expose identical semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


STALE_AFTER = timedelta(hours=4)
INTERRUPT_AFTER = timedelta(days=7)
ROLES = ("inspect", "execute", "review", "coordinate")


class ReservationConflict(ValueError):
    """An active execute reservation already exists for the work item."""


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def display(row: dict[str, Any], *, now: str | datetime | None = None) -> dict[str, Any]:
    result = dict(row)
    current = parse_time(now) if now is not None else datetime.now(timezone.utc)
    age = max(0, int((current - parse_time(result["last_activity_at"])).total_seconds()))
    result["activity_age_seconds"] = age
    result["stale"] = result["state"] == "active" and age >= int(STALE_AFTER.total_seconds())
    return result
