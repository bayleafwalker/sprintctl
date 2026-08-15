"""Operator policy for what reservation age *means*.

The reservation ledger stores facts: when a reservation was created and when
its session last did attributable work.  It deliberately holds no opinion
about when that age becomes interesting -- that is maintenance policy, and it
belongs to the operator running the repository, not to the coordination model.

Two horizons are configurable:

``stale_after``
    Read surfaces mark an active reservation ``stale`` past this age.  It is a
    display heuristic; nothing expires and no state changes.

``interrupt_after``
    The *explicitly invoked* ``maintain sweep`` may interrupt reservations
    idle for longer than this.  Nothing in the background applies it: no
    reservation ever changes state because time passed.
"""

from __future__ import annotations

from datetime import timedelta
import os
from typing import Any


DEFAULT_STALE_AFTER = timedelta(hours=4)
DEFAULT_INTERRUPT_AFTER = timedelta(days=7)

STALE_AFTER_ENV = "SPRINTCTL_RESERVATION_STALE_AFTER_HOURS"
INTERRUPT_AFTER_ENV = "SPRINTCTL_RESERVATION_INTERRUPT_AFTER_DAYS"


def _positive_float(env: str) -> float | None:
    raw = os.environ.get(env)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{env} must be a positive number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{env} must be a positive number, got {raw!r}")
    return value


def stale_after() -> timedelta:
    """How long an idle active reservation is displayed as fresh."""
    hours = _positive_float(STALE_AFTER_ENV)
    return DEFAULT_STALE_AFTER if hours is None else timedelta(hours=hours)


def interrupt_after() -> timedelta:
    """How idle a reservation must be before an operator sweep may interrupt it."""
    days = _positive_float(INTERRUPT_AFTER_ENV)
    return DEFAULT_INTERRUPT_AFTER if days is None else timedelta(days=days)


def sweep_reason(threshold: timedelta | None = None) -> str:
    """Audit text recorded on reservations an explicit sweep interrupts."""
    window = interrupt_after() if threshold is None else threshold
    hours = window.total_seconds() / 3600
    if hours >= 24 and hours % 24 == 0:
        span = f"{int(hours // 24)}-day"
    else:
        span = f"{hours:g}-hour"
    return f"{span} inactivity sweep"


def describe() -> dict[str, Any]:
    """Policy horizons, named once, for every surface that publishes them.

    Agent-facing protocol output and handoff bundles both advertise these, and
    they drifted into two spellings of the same number.  Keeping the field
    names here means the next horizon added shows up on both surfaces instead
    of only the one whose author remembered.
    """
    return {
        "stale_after_hours": stale_after().total_seconds() / 3600,
        "maintenance_interrupt_after_days": interrupt_after().total_seconds() / 86400,
        "maintenance_interrupt_trigger": "explicit 'sprintctl maintain sweep' only",
    }
