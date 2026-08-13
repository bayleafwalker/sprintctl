"""Explicit registration for command modules extracted from :mod:`cli`.

Command modules must be importable without importing ``sprintctl.cli``.  The
root CLI imports this package once, then attaches the known command groups at
the final construction point immediately before its served-mode inventory and
guard installation.
"""

from __future__ import annotations

import click

from . import remote_schema, repo


def register_commands(root: click.Group, *, get_store: repo.GetStore) -> None:
    """Attach extracted command groups to the supplied root in stable order."""
    # ``repo`` historically preceded ``remote-schema`` in the root Click
    # insertion order. Keep that observable help/inventory ordering stable.
    repo.register(root, get_store=get_store)
    remote_schema.register(root)


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
