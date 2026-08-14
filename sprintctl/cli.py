"""Sprintctl Click composition root."""

from __future__ import annotations

from functools import wraps

import click

from . import __version__
from . import commands as _commands
from . import served_routes as _served_routes
from . import cli_runtime as _cli_runtime

# Command modules retain private compatibility seams during the staged
# extraction.  Export the runtime namespace deliberately (including private
# helpers), rather than using ``import *`` which omits those seams and makes
# Click callbacks bypass the composition root's monkeypatch points.
globals().update(
    {
        name: value
        for name, value in vars(_cli_runtime).items()
        if not name.startswith("__")
    }
)


def _get_project_stores(obj, project_value):
    """Keep the root's injectable store seam for project-scope callers."""
    return _cli_runtime._get_project_stores(
        obj, project_value, get_store=_get_store
    )


@click.group()
@click.version_option(__version__, prog_name="sprintctl")
@click.option("--repo-id", default=None, help="Explicit repository scope for this invocation")
@click.option(
    "--allow-markerless-nonlocal",
    is_flag=True,
    default=False,
    help="Permit one remote/served invocation without a repository marker when used with --repo-id",
)
@click.pass_context
def cli(ctx: click.Context, repo_id: str | None, allow_markerless_nonlocal: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj.setdefault("conn", None)
    ctx.obj["explicit_repo_id"] = repo_id
    ctx.obj["allow_markerless_nonlocal"] = allow_markerless_nonlocal

_commands.register_doctor_command(cli)
doctor_cmd = _commands.doctor_cmd

# ---------------------------------------------------------------------------
# sprint / item
# ---------------------------------------------------------------------------

_commands.register_work_commands(cli, runtime=globals())
sprint = _commands.sprint_group
item = _commands.item_group

# ---------------------------------------------------------------------------
# event / authority / pilot / projection reads
# ---------------------------------------------------------------------------

_commands.register_operations_commands(cli, runtime=globals())
event = _commands.event_group
authority_commands = _commands.authority_group
pilot = _commands.pilot_group
projection_reads_group = _commands.projection_reads_group
# takeup / maintain
# ---------------------------------------------------------------------------

_commands.register_takeup_maintain_commands(cli, runtime=globals())
takeup = _commands.takeup_group
maintain = _commands.maintain_group

# Database maintenance historically registered here, between ``maintain`` and
# the export/import commands. Keep that insertion point stable while the
# command implementation lives in ``commands.db``.
_commands.register_db_commands(cli, get_store=lambda obj: _get_store(obj))
db_group = _commands.db_group
db_vacuum = _commands.db_vacuum
db_integrity = _commands.db_integrity
db_recover_from_remote = _commands.db_recover_from_remote

# render
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

_commands.register_transfer_commands(cli, get_conn=lambda obj: _get_conn(obj))
export_cmd = _commands.export_cmd
import_cmd = _commands.import_cmd



# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

_commands.register_claim_commands(cli, runtime=globals())
claim = _commands.claim_group
# handoff / session / migration
# ---------------------------------------------------------------------------

_commands.register_session_commands(cli, runtime=globals())
handoff_cmd = _commands.handoff_cmd
agent_protocol_cmd = _commands.agent_protocol_cmd
next_work_cmd = _commands.next_work_cmd
context_candidates_cmd = _commands.context_candidates_cmd
session = _commands.session_group
usage_cmd = _commands.usage_cmd
git_context_cmd = _commands.git_context_cmd
render_cmd = _commands.render_cmd
migrate_to_remote_cmd = _commands.migrate_to_remote_cmd
remote_backfill_cmd = _commands.remote_backfill_cmd




# Compatibility aliases for the extracted command's historical cli.py seams.
remote_schema = _commands.remote_schema_group
_remote_schema_store = _commands._remote_schema_store
remote_schema_check_cmd = _commands.remote_schema_check_cmd
remote_schema_migrate_cmd = _commands.remote_schema_migrate_cmd
remote_schema_stage_maintenance_bridge_cmd = (
    _commands.remote_schema_stage_maintenance_bridge_cmd
)
repo = _commands.repo_group
repo_list = _commands.repo_list
repo_delete = _commands.repo_delete


_commands.register_commands(cli, get_store=lambda obj: _get_store(obj))


def _click_leaf_paths(command: click.Command, prefix: tuple[str, ...] = ()) -> set[str]:
    """Return every executable Click leaf below ``command``.

    This intentionally inspects the constructed Click tree, not a duplicate
    hand-maintained list.  The import-time equality assertion makes adding a
    command a conscious served-mode decision.
    """
    if isinstance(command, click.Group):
        return {
            path
            for name, child in command.commands.items()
            for path in _click_leaf_paths(child, (*prefix, name))
        }
    return {" ".join(prefix)}


def _install_served_command_guards(command: click.Command, prefix: tuple[str, ...] = ()) -> None:
    """Wrap each Click leaf before its callback can construct a backend store."""
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            _install_served_command_guards(child, (*prefix, name))
        return

    command_path = " ".join(prefix)
    callback = command.callback
    assert callback is not None, f"Click leaf {command_path!r} has no callback"

    @wraps(callback)
    def guarded_callback(*args, __callback=callback, __path=command_path, **kwargs):
        _guard_served_command(__path, kwargs)
        return __callback(*args, **kwargs)

    # Regression tests inspect this marker to prove the structural inventory
    # is enforced, rather than merely documented.
    guarded_callback.__served_guard_path__ = command_path
    command.callback = guarded_callback


_CLICK_LEAF_PATHS = _click_leaf_paths(cli)
assert _CLICK_LEAF_PATHS == set(_served_routes.SERVED_COMMAND_DISPOSITIONS), (
    "SERVED_COMMAND_DISPOSITIONS must classify every Click leaf exactly; "
    f"unclassified={sorted(_CLICK_LEAF_PATHS - set(_served_routes.SERVED_COMMAND_DISPOSITIONS))}, "
    f"stale={sorted(set(_served_routes.SERVED_COMMAND_DISPOSITIONS) - _CLICK_LEAF_PATHS)}"
)
_install_served_command_guards(cli)
