"""Private shared reservation transitions; drivers own SQL and transactions.

This is implementation reuse, not a provider contract or a composition API.
Reservation coordination remains advisory and separate from hosted work leases.

Each valid public mutation owns its transaction. SQLite refuses an existing
transaction without committing or rolling back caller work. PostgreSQL follows
its public mutation convention: commit an implicit transaction before starting
this owned transaction (including pending caller writes). Managed outer and
two-phase transactions refuse that commit before lifecycle writes. Initial
invalid calls and terminal release replays avoid this precommit; a valid peek
can still become invalid before locked revalidation, after the precommit.

The stale sweep is atomic for its frozen selected batch, not for the entire
maintenance command. No retry or cross-operation transaction API is exposed.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Protocol

from . import reservation as model
from . import reservation_policy as policy


class Driver(Protocol):
    def peek(self, ident: int) -> dict | None: ...
    def transaction(self) -> AbstractContextManager[None]: ...
    def lock_one(self, ident: int) -> tuple[dict, dict] | None: ...
    def lock_stale(self, cutoff: str) -> list[tuple[dict, dict]]: ...
    def touch(self, row: dict, now: str, correlation: str | None) -> dict: ...
    def reassign(self, row: dict, now: str, actor: str, session: str, correlation: str | None) -> dict: ...
    def release(self, row: dict, now: str) -> dict: ...
    def interrupt(self, row: dict, now: str, reason: str) -> dict: ...
    def event(self, item: dict, row: dict, kind: str, actor: str, payload: dict) -> None: ...


def _touch_basis(row: dict | None, ident: int, session: str) -> dict:
    if row is None:
        raise ValueError(f"Reservation #{ident} not found")
    if row['state'] != 'active':
        raise ValueError(f"Reservation #{ident} is {row['state']}")
    if row['session_id'] != session:
        raise ValueError(f"Reservation #{ident} belongs to another session")
    return row


def _active(row: dict | None, ident: int) -> dict:
    if row is None or row['state'] != 'active':
        raise ValueError(f"Reservation #{ident} is not active")
    return row


def touch(driver: Driver, ident: int, *, session_id: str, correlation_ref: str | None) -> dict:
    # Preserve ordinary invalid-call behavior before a PG public precommit.
    # This peek is never the write precondition: revalidate the locked row.
    _touch_basis(driver.peek(ident), ident, session_id)
    with driver.transaction():
        locked = driver.lock_one(ident)
        row = _touch_basis(locked[1] if locked else None, ident, session_id)
        return model.display(driver.touch(row, model.now_text(), correlation_ref))


def reassign(driver: Driver, ident: int, *, actor: str, session_id: str,
             correlation_ref: str | None) -> dict:
    _active(driver.peek(ident), ident)
    with driver.transaction():
        locked = driver.lock_one(ident)
        row = _active(locked[1] if locked else None, ident)
        assert locked is not None
        updated = driver.reassign(row, model.now_text(), actor, session_id, correlation_ref)
        driver.event(locked[0], updated, 'reservation.reassigned', actor,
            {'reservation_id': ident, 'previous_actor': row['actor'],
             'previous_session_id': row['session_id']})
        return model.display(updated)


def release(driver: Driver, ident: int, *, actor: str | None) -> dict:
    row = driver.peek(ident)
    if row is None:
        raise ValueError(f"Reservation #{ident} not found")
    if row['state'] != 'active':
        return model.display(row)  # No unnecessary precommit on terminal replay.
    with driver.transaction():
        locked = driver.lock_one(ident)
        if locked is None:
            raise ValueError(f"Reservation #{ident} not found")
        item, row = locked
        if row['state'] != 'active':
            return model.display(row)
        updated = driver.release(row, model.now_text())
        driver.event(item, updated, 'reservation.released', actor or row['actor'],
                     {'reservation_id': ident})
        return model.display(updated)


def sweep(driver: Driver, *, now: str | None, interrupt_after: timedelta | None) -> list[dict]:
    window = policy.interrupt_after() if interrupt_after is None else interrupt_after
    reason = policy.sweep_reason(window)
    now = now or model.now_text()
    cutoff = (model.parse_time(now) - window).strftime('%Y-%m-%dT%H:%M:%SZ')
    with driver.transaction():
        # Drivers freeze a finite ID population and lock/recheck it. Updating
        # only those rows keeps events paired with actual transitions.
        selected = driver.lock_stale(cutoff)
        result = []
        for item, row in selected:
            updated = driver.interrupt(row, now, reason)
            driver.event(item, updated, 'reservation.interrupted', 'maintenance',
                         {'reservation_id': row['id'], 'reason': reason})
            result.append(model.display(updated, now=now))
        return result
