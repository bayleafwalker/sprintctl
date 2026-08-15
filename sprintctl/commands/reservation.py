"""The v0.3 credential-free reservation CLI."""

from __future__ import annotations

import json
import os
from typing import Any

import click

from .. import db as _db
from .. import backend as _backend
from .. import served as _served


def _session(value: str | None) -> str:
    return value or os.environ.get("SPRINTCTL_RUNTIME_SESSION_ID") or "interactive"


@click.group("reservation")
def reservation() -> None:
    """Coordinate work with advisory reservations."""


@reservation.command("reserve")
@click.option("--item-id", type=int, required=True)
@click.option("--actor", required=True)
@click.option("--session-id", default=None)
@click.option("--role", type=click.Choice(_db.RESERVATION_ROLES), default=_db.DEFAULT_RESERVATION_ROLE,
              help="Work relationship: execution, verification, or observation")
@click.option("--correlation-ref", default=None, help="ActionQ execution or receipt reference")
@click.option("--interrupt-existing", "interrupt_existing", is_flag=True, default=False,
              help="Deliberately interrupt the item's active execution reservations first")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def reserve(obj: dict[str, Any], item_id: int, actor: str, session_id: str | None, role: str,
            correlation_ref: str | None, interrupt_existing: bool, as_json: bool) -> None:
    """Register a reservation on a work item.

    Overlapping reservations are allowed and reported, not refused: a
    reservation is a coordination signal, not a lease.
    """
    served = _served_result(obj, "work.reservation.reserve", {"item_id": item_id, "actor": actor, "session_id": _session(session_id), "role": role, "correlation_ref": correlation_ref, "interrupt_existing": interrupt_existing})
    if served is not None:
        _echo(served["reservation"], as_json)
        return
    conn, _ = _db_store(obj)
    try:
        row = _db.reserve(conn, item_id, actor=actor, session_id=_session(session_id), role=role,
                          correlation_ref=correlation_ref, interrupt_existing=interrupt_existing)
    except _db.ReservationConflict as exc:
        raise click.ClickException(str(exc)) from exc
    _echo(row, as_json)


@reservation.command("touch")
@click.option("--id", "reservation_id", type=int, required=True)
@click.option("--session-id", default=None)
@click.option("--correlation-ref", default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def touch(obj, reservation_id, session_id, correlation_ref, as_json) -> None:
    served = _served_result(obj, "work.reservation.touch", {"reservation_id": reservation_id, "session_id": _session(session_id), "correlation_ref": correlation_ref})
    if served is not None:
        _echo(served["reservation"], as_json)
        return
    conn, _ = _db_store(obj)
    try:
        row = _db.touch_reservation(conn, reservation_id, session_id=_session(session_id), correlation_ref=correlation_ref)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo(row, as_json)


@reservation.command("reassign")
@click.option("--id", "reservation_id", type=int, required=True)
@click.option("--actor", required=True)
@click.option("--session-id", required=True)
@click.option("--correlation-ref", default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def reassign(obj, reservation_id, actor, session_id, correlation_ref, as_json) -> None:
    served = _served_result(obj, "work.reservation.reassign", {"reservation_id": reservation_id, "actor": actor, "session_id": session_id, "correlation_ref": correlation_ref})
    if served is not None:
        _echo(served["reservation"], as_json)
        return
    conn, _ = _db_store(obj)
    try:
        row = _db.reassign_reservation(conn, reservation_id, actor=actor, session_id=session_id, correlation_ref=correlation_ref)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo(row, as_json)


@reservation.command("release")
@click.option("--id", "reservation_id", type=int, required=True)
@click.option("--actor", default=None)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def release(obj, reservation_id, actor, as_json) -> None:
    served = _served_result(obj, "work.reservation.release", {"reservation_id": reservation_id, "actor": actor})
    if served is not None:
        _echo(served["reservation"], as_json)
        return
    conn, _ = _db_store(obj)
    try:
        row = _db.release_reservation(conn, reservation_id, actor=actor)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo(row, as_json)


@reservation.command("list")
@click.option("--item-id", type=int, default=None)
@click.option("--active-only/--all", default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def list_(obj, item_id, active_only, as_json) -> None:
    served = _served_result(obj, "work.read.reservations", {"item_id": item_id, "active_only": active_only})
    if served is not None:
        _echo(served["reservations"], as_json)
        return
    conn, _ = _db_store(obj)
    rows = _db.list_reservations(conn, work_item_id=item_id, active_only=active_only)
    _echo(rows, as_json)


@reservation.command("show")
@click.option("--id", "reservation_id", type=int, required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def show(obj, reservation_id, as_json) -> None:
    served = _served_result(obj, "work.read.reservation", {"reservation_id": reservation_id})
    if served is not None:
        _echo(served["reservation"], as_json)
        return
    conn, _ = _db_store(obj)
    row = _db.get_reservation(conn, reservation_id)
    if row is None:
        raise click.ClickException(f"Reservation #{reservation_id} not found")
    _echo(row, as_json)


def _db_store(obj):
    conn = obj.get("conn")
    if conn is None:
        conn = _db.get_connection()
        _db.init_db(conn)
        obj["conn"] = conn
    return conn, _db


def _served_result(obj: dict[str, Any], operation: str, arguments: dict[str, Any]) -> dict | None:
    """Keep served reservation commands out of SQLite and direct PostgreSQL."""
    config = _backend.load_backend_config(
        explicit_repo_id=obj.get("explicit_repo_id"),
        allow_markerless_nonlocal=obj.get("allow_markerless_nonlocal", False),
    )
    if config.mode != "served":
        return None
    assert config.served_profile is not None and config.repo_id is not None
    return _served.reservation_operation(config.served_profile, operation, arguments, repo_id=config.repo_id)


def _echo(value, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, list):
        for row in value:
            click.echo(f"#{row['id']} item #{row['work_item_id']} {row['actor']} {row['state']}")
    else:
        click.echo(f"Reservation #{value['id']} on item #{value['work_item_id']}: {value['state']}")
        for other in value.get("conflicting_reservations") or []:
            click.echo(
                f"  conflict: reservation #{other['id']} {other['role']} held by "
                f"{other['actor']} (session {other['session_id']}) is also active"
            )
        if value.get("conflict_severity") == "warning":
            click.echo("  warning: two sessions are executing this item; coordinate before editing.")


def register(root: click.Group) -> None:
    root.add_command(reservation)
