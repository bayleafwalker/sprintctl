"""Credential-free advisory reservations.

A reservation is a detector, not a lease.  It does not authorize item
mutations, and it does not serialize them either: any number of reservations
may be active on one work item at once.  ``reserve`` therefore always records
the reservation and *reports* the overlap it found, rather than refusing to
register the second actor -- refusing would either turn the ledger into
de-facto locking or push the second actor into working unrecorded, which is
the worst outcome a coordination ledger can produce.

Interrupting somebody else's reservation stays available, but it is a separate
and explicit act (``--interrupt-existing``), never a side effect of wanting to
start work.

Roles describe the work relationship, so overlap can be classified: two
``execution`` reservations on one item deserve a warning, while ``execution``
beside ``verification`` or ``observation`` is ordinary.

This module is intentionally SQL-free so the SQLite and PostgreSQL facades
expose identical semantics.  It stores and reports facts only; how old is
"too old" is operator policy and lives in :mod:`sprintctl.reservation_policy`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import reservation_policy as _policy


ROLES = ("execution", "verification", "observation")
DEFAULT_ROLE = "execution"

#: Pre-v3 role names accepted on input and folded into the taxonomy above.
#: ``coordinate`` is not a work relationship -- orchestration is session and
#: project context -- so a coordinator reservation is an observation of the
#: item it is coordinating.
ROLE_ALIASES = {
    "execute": "execution",
    "review": "verification",
    "inspect": "observation",
    "coordinate": "observation",
}


class ReservationConflict(ValueError):
    """A reservation operation was refused by a repository-level condition.

    Overlap is never a refusal.  This signals something about the repository
    -- currently only an active exact-plan maintenance capability, whose
    window is defined by there being no live reservations at all.
    """


def normalize_role(role: str | None) -> str:
    if role is None:
        return DEFAULT_ROLE
    candidate = str(role).strip().lower()
    candidate = ROLE_ALIASES.get(candidate, candidate)
    if candidate not in ROLES:
        raise ValueError(f"invalid reservation role {role!r}; expected one of {', '.join(ROLES)}")
    return candidate


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
    result["stale"] = result["state"] == "active" and age >= int(_policy.stale_after().total_seconds())
    return result


def conflict_view(row: dict[str, Any]) -> dict[str, Any]:
    """The compact shape in which an overlapping reservation is reported."""
    return {
        key: row[key]
        for key in ("id", "work_item_id", "actor", "session_id", "role", "state", "last_activity_at")
        if key in row
    }


def annotate_conflicts(row: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach the overlap this reservation was created into.

    ``conflict`` is informational: every reservation listed here is live and
    remains live.  ``severity`` is ``warning`` only when two sessions claim to
    be *executing* the same item, which is the case an operator should look
    at; any other overlap is normal collaboration.
    """
    result = dict(row)
    conflicts = [conflict_view(other) for other in others]
    result["conflict"] = bool(conflicts)
    result["conflicting_reservations"] = conflicts
    executing = row.get("role") == "execution" and any(
        other.get("role") == "execution" for other in others
    )
    result["conflict_severity"] = "warning" if executing else ("informational" if conflicts else "none")
    return result
