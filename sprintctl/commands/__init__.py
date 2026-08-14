"""Explicit registration for command modules extracted from :mod:`cli`.

Command modules must be importable without importing ``sprintctl.cli``.  The
root CLI imports this package once, then attaches the known command groups at
the final construction point immediately before its served-mode inventory and
guard installation.
"""

from __future__ import annotations

import click

from . import db, doctor, lifecycle, operations, remote_schema, repo, reservation, session, transfer, work


_RUNTIME_INTERNALS = {"_RUNTIME", "_sync_runtime", "_wrap_runtime_callbacks", "register"}
_RUNTIME_MODULES = (work, operations, lifecycle, session)


def _refresh_runtime_modules(runtime: dict[str, object]) -> None:
    """Broadcast the assembled CLI namespace to every callback module.

    Extracted command groups still call helpers defined by sibling groups.  A
    module registered early therefore needs the later group's helpers before a
    callback is invoked; the original monolithic ``cli.py`` provided that
    shared namespace implicitly.
    """
    values = {name: value for name, value in runtime.items() if not name.startswith("__")}
    for module in _RUNTIME_MODULES:
        runtime_table = getattr(module, "_RUNTIME", None)
        if isinstance(runtime_table, dict):
            runtime_table.update(values)


def _merge_runtime_exports(module, runtime: dict[str, object]) -> None:
    """Expose extracted helper seams to later command modules and compatibility callers."""
    runtime.update(
        {
            name: value
            for name, value in vars(module).items()
            if not name.startswith("__") and name not in _RUNTIME_INTERNALS
        }
    )
    _refresh_runtime_modules(runtime)


def register_commands(root: click.Group, *, get_store: repo.GetStore) -> None:
    """Attach extracted command groups to the supplied root in stable order."""
    # ``repo`` historically preceded ``remote-schema`` in the root Click
    # insertion order. Keep that observable help/inventory ordering stable.
    repo.register(root, get_store=get_store)
    remote_schema.register(root)


def register_doctor_command(root: click.Group) -> None:
    """Attach the diagnostic command at the root's historical first position."""
    doctor.register(root)


def register_db_commands(root: click.Group, *, get_store: db.GetStore) -> None:
    """Attach the historically mid-file database maintenance group."""
    db.register(root, get_store=get_store)


def register_transfer_commands(root: click.Group, *, get_conn: transfer.GetConn) -> None:
    """Attach the top-level sprint export/import commands."""
    transfer.register(root, get_conn=get_conn)


def register_work_commands(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach sprint and work-item command groups."""
    work.register(root, runtime=runtime)
    _merge_runtime_exports(work, runtime)


def register_operations_commands(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach event, authority, pilot, and projection-read groups."""
    operations.register(root, runtime=runtime)
    _merge_runtime_exports(operations, runtime)


def register_takeup_maintain_commands(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach takeup and maintenance groups at their historical position."""
    lifecycle.register_takeup_maintain(root, runtime=runtime)
    _merge_runtime_exports(lifecycle, runtime)


def register_reservation_commands(root: click.Group) -> None:
    """Attach credential-free reservation commands."""
    reservation.register(root)


def register_session_commands(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach handoff, session, context, and migration commands."""
    session.register(root, runtime=runtime)
    _merge_runtime_exports(session, runtime)


# Compatibility aliases for private seams that historically lived in cli.py.
remote_schema_group = remote_schema.remote_schema
_remote_schema_store = remote_schema._remote_schema_store
remote_schema_check_cmd = remote_schema.remote_schema_check_cmd
remote_schema_migrate_cmd = remote_schema.remote_schema_migrate_cmd
remote_schema_stage_maintenance_bridge_cmd = (
    remote_schema.remote_schema_stage_maintenance_bridge_cmd
)
repo_group = repo.repo
repo_list = repo.repo_list
repo_delete = repo.repo_delete
db_group = db.db_group
db_vacuum = db.db_vacuum
db_integrity = db.db_integrity
db_recover_from_remote = db.db_recover_from_remote
export_cmd = transfer.export_cmd
import_cmd = transfer.import_cmd
sprint_group = work.sprint
item_group = work.item
event_group = operations.event
authority_group = operations.authority_commands
projection_reads_group = operations.projection_reads_group
takeup_group = lifecycle.takeup
maintain_group = lifecycle.maintain
reservation_group = reservation.reservation
handoff_cmd = session.handoff_cmd
agent_protocol_cmd = session.agent_protocol_cmd
next_work_cmd = session.next_work_cmd
context_candidates_cmd = session.context_candidates_cmd
session_group = session.session
usage_cmd = session.usage_cmd
git_context_cmd = session.git_context_cmd
render_cmd = session.render_cmd
migrate_to_remote_cmd = session.migrate_to_remote_cmd
remote_backfill_cmd = session.remote_backfill_cmd
doctor_cmd = doctor.doctor_cmd
