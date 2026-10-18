"""Private shared reservation transitions; drivers own SQL and transactions.

This is implementation reuse, not a provider contract or a composition API.
Reservation coordination remains advisory and separate from hosted work leases.

Each valid public lifecycle mutation owns its transaction. SQLite refuses an existing
transaction without committing or rolling back caller work. PostgreSQL follows
its public mutation convention: commit an implicit transaction before starting
this owned transaction (including pending caller writes). Managed outer and
two-phase transactions refuse that commit before lifecycle writes. Initial
invalid calls and terminal release replays avoid this precommit; a valid peek
can still become invalid before locked revalidation, after the precommit.

Admission uses a separate noncommitting participant after backend-specific
validation and locking. Public admission wrappers and the native replay
ledger owner retain their distinct transaction policies.

The stale sweep is atomic for its frozen selected batch, not for the entire
maintenance command. No retry or cross-operation transaction API is exposed.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Protocol

from . import reservation as model
from . import reservation_policy as policy
from . import releases


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


class _AdmissionParticipant(Protocol):
    """Backend admission has already run; these operations never own a transaction."""

    def release_basis(self) -> tuple[dict, str, int]: ...
    def freeze_release(self, item: dict, revision: str, revise_count: int,
                       actor: str, contract: dict) -> str: ...
    def active_reservations(self) -> list[dict]: ...
    def interrupt_execution(self, selected: list[dict], now: str, reason: str) -> None: ...
    def insert_reservation(self, session_id: str, actor: str, role: str, now: str,
                           correlation_ref: str | None, release_digest: str | None) -> int: ...
    def read_inserted(self, reservation_id: int) -> dict: ...
    def event_item(self) -> dict: ...
    def event(self, item: dict, row: dict, kind: str, actor: str, payload: dict) -> None: ...


def _reserve_admitted(p: _AdmissionParticipant, work_item_id: int, *, actor: str,
                      session_id: str, role: str, now: str,
                      correlation_ref: str | None, interrupt_existing: bool,
                      expected_revision: str | None, contract: dict) -> dict:
    """Share admission effects without moving validation, locks or commit ownership.

    SQLite resolves a basis only when needed; PG supplies its already locked
    basis for every role. Native keyed replay remains outside this participant.
    """
    release_digest = None
    if role == 'execution' or expected_revision is not None:
        item, revision, revise_count = p.release_basis()
        if expected_revision is not None and not releases.basis_matches(expected_revision, revision):
            raise releases.StaleReleaseBasis(work_item_id, expected_revision, revision)
        if role == 'execution':
            release_digest = p.freeze_release(item, revision, revise_count, actor, contract)
    existing = p.active_reservations()
    interrupted = [row for row in existing if row['role'] == 'execution'] if interrupt_existing else []
    if interrupted:
        p.interrupt_execution(interrupted, now, f'interrupted by {actor} ({session_id})')
    reservation_id = p.insert_reservation(session_id, actor, role, now, correlation_ref, release_digest)
    row = p.read_inserted(reservation_id)
    item = p.event_item()
    interrupted_ids = {old['id'] for old in interrupted}
    remaining = [old for old in existing if old['id'] not in interrupted_ids]
    for old in interrupted:
        p.event(item, old, 'reservation.interrupted', actor,
            {'reservation_id': old['id'], 'reason': 'explicit-takeover', 'replacement_id': reservation_id})
    p.event(item, row, 'reservation.reserved', actor,
        {'reservation_id': reservation_id, 'session_id': session_id, 'role': role,
         'correlation_ref': correlation_ref, 'interrupt_existing': interrupt_existing,
         'conflicting_reservation_ids': [old['id'] for old in remaining],
         'release_digest': row['release_digest']})
    return model.annotate_conflicts(model.display(row), remaining)
