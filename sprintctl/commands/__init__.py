"""Explicit registration for command modules extracted from :mod:`cli`.

Command modules must be importable without importing ``sprintctl.cli``.  The
root CLI imports this package once, then attaches the known command groups at
the final construction point immediately before its served-mode inventory and
guard installation.
"""

from __future__ import annotations

from collections.abc import Callable

import click

from . import remote_schema


# Keep this ordered tuple explicit.  Adding a command module is a deliberate
# registration change, rather than an import-time filesystem/package scan.
_COMMAND_REGISTRARS: tuple[Callable[[click.Group], None], ...] = (
    remote_schema.register,
)


def register_commands(root: click.Group) -> None:
    """Attach extracted command groups to the supplied root in stable order."""
    for register in _COMMAND_REGISTRARS:
        register(root)


# Compatibility aliases for private seams that historically lived in cli.py.
remote_schema_group = remote_schema.remote_schema
_remote_schema_store = remote_schema._remote_schema_store
remote_schema_check_cmd = remote_schema.remote_schema_check_cmd
remote_schema_migrate_cmd = remote_schema.remote_schema_migrate_cmd
remote_schema_stage_maintenance_bridge_cmd = (
    remote_schema.remote_schema_stage_maintenance_bridge_cmd
)
