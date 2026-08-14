"""Sprint and work-item command groups.

The callbacks retain the existing CLI runtime seams through the injected
runtime mapping. This keeps the command modules independent from cli.py.
"""

import json
import os
import re
import secrets
import sqlite3
import socket
import stat
import subprocess
import sys
import time
import uuid
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO
from urllib.parse import urlsplit

import click

from .. import __version__
from .. import application as _application
from .. import backend as _backend
from .. import authority as _authority
from .. import authority_config as _authority_config
from .. import commands as _commands
from .. import context_candidates as _context_candidates
from .. import context_contract as _context_contract
from .. import contracts as _contracts
from .. import db as _db
from .. import doctor as _doctor
from .. import maintain as _maintain
from .. import observations as _observations
from .. import outbox as _outbox
from .. import pg as _pg
from .. import project as _project
from .. import projection as _projection
from .. import projection_reads as _projection_reads
from .. import served as _served
from .. import served_routes as _served_routes
from .. import sync as _sync
from ..cli_support import _redacted_postgres_error
from ..render import render_sprint_doc


@click.group()
def sprint() -> None:
    """Manage sprints."""


@sprint.command("create")
@click.option("--name", required=True, help="Sprint name")
@click.option("--goal", default="", help="Sprint goal")
@click.option("--start", "start_date", default=None, help="Start date (YYYY-MM-DD, optional)")
@click.option("--end", "end_date", default=None, help="End date (YYYY-MM-DD, optional)")
@click.option(
    "--status",
    default="planned",
    type=click.Choice(["planned", "active", "closed"]),
    help="Initial status",
)
@click.option(
    "--kind",
    default="active_sprint",
    type=click.Choice(["active_sprint", "backlog", "archive"]),
    help="Sprint kind (default: active_sprint)",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output created sprint as JSON")
@click.pass_obj
def sprint_create(obj, name, goal, start_date, end_date, status, kind, as_json) -> None:
    """Create a new sprint."""
    config = _served_config_or_none(obj)
    if config is not None:
        context = _resolved_context(config)
        result = _run_served(
            "sprint create",
            _served.sprint_create,
            config.served_profile,
            repo_id=config.repo_id,
            name=name,
            goal=goal,
            start_date=start_date,
            end_date=end_date,
            status=status,
            kind=kind,
            resolved_context=context,
        )
        created = result["sprint"]
        if as_json:
            click.echo(json.dumps(created, indent=2))
            return
        click.echo(f"Created sprint #{created['id']}: {created['name']}")
        click.echo(_render_resolved_context(context))
        return
    store, m = _get_store(obj)
    sid = m.create_sprint(store, name, goal, start_date, end_date, status, kind=kind)
    if status == "active":
        _emit_audit_event(
            "sprint.opened",
            summary=f"Sprint {sid} opened",
            refs=[f"sprint:{sid}"],
            metadata={"sprint_id": sid, "event_type": "sprint-opened"},
        )
    if as_json:
        sprint = m.get_sprint(store, sid)
        assert sprint is not None
        click.echo(json.dumps(sprint, indent=2))
        return
    click.echo(f"Created sprint #{sid}: {name}")


@sprint.command("show")
@click.option("--id", "sprint_id", type=str, default=None, help="Sprint ID or repo#id")
@click.option("--detail", is_flag=True, default=False, help="Include sprint health, track health, and stale item count")
@click.option("--watch", "watch_mode", is_flag=True, default=False, help="Refresh output in a loop until interrupted")
@click.option("--interval", type=float, default=30.0, show_default=True, help="Watch refresh interval in seconds")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_show(obj, sprint_id: str | None, detail, watch_mode, interval, as_json) -> None:
    """Show a sprint (defaults to active sprint)."""
    if watch_mode and as_json:
        click.echo("Error: --watch cannot be combined with --json.", err=True)
        sys.exit(1)
    if interval <= 0:
        click.echo("Error: --interval must be > 0.", err=True)
        sys.exit(1)

    if sprint_id is not None:
        sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")

    config = _served_config_or_none(obj)
    if config is not None:
        def render_once() -> None:
            context = _resolved_context(config)
            result = _run_served(
                "sprint show --detail" if detail else "sprint show",
                _served.read_sprint_detail if detail else _served.read_sprint,
                config.served_profile,
                repo_id=config.repo_id, sprint_id=sprint_id, resolved_context=context,
            )
            payload = result["sprint"] if detail else _collect_sprint_show_payload(None, result["sprint"], detail=False)
            if as_json:
                click.echo(json.dumps(payload, indent=2))
            else:
                _emit_sprint_show_text(payload, detail=detail)
                click.echo(_render_resolved_context(context))

        if not watch_mode:
            render_once()
            return
        try:
            while True:
                cleared = _clear_terminal_for_watch()
                if not cleared:
                    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    click.echo(f"\n--- sprintctl watch refresh {stamp} ---")
                render_once()
                time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\nWatch mode stopped.")
        return

    store, m = _get_store(obj)
    def render_once() -> None:
        if sprint_id is not None:
            sprint = m.get_sprint(store, sprint_id)
        else:
            sprint = _resolve_implicit_sprint(store, m=m, option_name="--id")
        if sprint is None:
            click.echo("No sprint found. Use --id to specify one.", err=True)
            sys.exit(1)

        payload = _collect_sprint_show_payload(store, sprint, detail=detail, m=m)
        if as_json:
            click.echo(json.dumps(payload, indent=2))
            return
        _emit_sprint_show_text(payload, detail=detail)

    if not watch_mode:
        render_once()
        return

    try:
        while True:
            cleared = _clear_terminal_for_watch()
            if not cleared:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                click.echo(f"\n--- sprintctl watch refresh {stamp} ---")
            render_once()
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nWatch mode stopped.")


def _served_sprint_status(config, sprint_id, new_status, actor, as_json) -> None:
    """Served-mode ``sprint status``: routes to ``work.lifecycle.arbitrate``.

    Only ``sprint.activate`` (-> "active") and ``sprint.close`` (-> "closed")
    exist in authority.py's dispatch table -- no sprint status ever
    transitions *to* "planned" (``SPRINT_TRANSITIONS`` in db.py has no target
    of "planned" from any source status), so there is no record_type for that
    target and served mode fails closed rather than guessing.
    """
    if new_status not in ("active", "closed"):
        click.echo(
            "Error: served sprint status has no work.lifecycle.arbitrate mapping for "
            f"a transition to {new_status!r} (no sprint status ever transitions to "
            "'planned'); use SPRINTCTL_BACKEND=local.",
            err=True,
        )
        sys.exit(1)

    context = _resolved_context(config)
    identity = _run_served(
        "sprint status",
        _served.identity_current,
        config.served_profile,
        repo_id=config.repo_id,
        resolved_context=context,
    )
    authenticated_actor = identity["actor"]
    if actor is not None and actor != authenticated_actor:
        click.echo(
            f"Note: served mode records the authenticated identity "
            f"({authenticated_actor}); --actor {actor!r} was not sent and is ignored.",
            err=True,
        )
    actor = authenticated_actor
    read_result = _run_served(
        "sprint status",
        _served.read_sprints,
        config.served_profile,
        repo_id=config.repo_id,
        include_backlog=True,
        include_archive=True,
        resolved_context=context,
    )
    sprint = next(
        (s for s in read_result["sprints"] if s["id"] == sprint_id), None
    )
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.\n{_render_resolved_context(context)}", err=True)
        sys.exit(1)
    current = sprint["status"]
    record_type = "sprint.activate" if new_status == "active" else "sprint.close"
    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    try:
        durable = _mint_authority_command_record(
            record_type=record_type,
            actor=actor,
            refs={
                "repo_id": _authority_repo_uuid(rollout_paths.repo_root),
                "aggregate_type": "sprint",
                "aggregate_uuid": sprint["aggregate_uuid"],
                "aggregate_id": sprint_id,
            },
            payload={},
            basis_revision=_authority.sprint_revision(sprint),
            outbox_path=rollout_paths.outbox_path,
        )
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    decision = _run_served(
        "sprint status",
        _served.lifecycle_arbitrate,
        config.served_profile,
        repo_id=config.repo_id,
        record=_served_record_argument(durable),
        resolved_context=context,
    )
    if decision["outcome"] != "accepted":
        click.echo(
            f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}",
            err=True,
        )
        sys.exit(1)
    effect = decision["effect"]
    boundary_event_id = effect.get("boundary_event_id")
    boundary_revision = effect.get("boundary_revision")
    if new_status == "active":
        _emit_audit_event(
            "sprint.opened",
            summary=f"Sprint {sprint_id} opened",
            refs=[f"sprint:{sprint_id}"],
            metadata={"sprint_id": sprint_id, "event_type": "sprint-opened"},
        )
    elif new_status == "closed":
        _emit_audit_event(
            "sprint.closed",
            summary=f"Sprint {sprint_id} closed",
            refs=[f"sprint:{sprint_id}"],
            metadata={
                "sprint_id": sprint_id,
                "event_type": "sprint-closed",
                "boundary_event_id": boundary_event_id,
                "boundary_revision": boundary_revision,
                "actor": actor,
            },
        )
    if as_json:
        payload = {"sprint_id": sprint_id, "previous": current, "status": new_status}
        if boundary_event_id is not None:
            payload["boundary_event_id"] = boundary_event_id
            payload["boundary_revision"] = boundary_revision
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Sprint #{sprint_id} status: {current} -> {new_status}")
    if boundary_event_id is not None:
        click.echo(
            f"Sprint-close boundary event #{boundary_event_id} (revision {boundary_revision})"
        )
    click.echo(_render_resolved_context(context))


@sprint.command("status")
@click.option("--id", "sprint_id", type=str, required=True, help="Sprint ID or repo#id")
@click.option(
    "--status",
    "new_status",
    required=True,
    type=click.Choice(["planned", "active", "closed"]),
    help="New status",
)
@click.option("--actor", default=None, help="Actor name (defaults to the current OS user)")
@click.option(
    "--expected-revision",
    default=None,
    help="Required expected sprint status revision for direct local transitions",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_status(obj, sprint_id, new_status, actor, expected_revision, as_json) -> None:
    """Update a sprint's status (enforces allowed transitions)."""
    sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    config = _served_config_or_none(obj)
    if config is not None:
        if expected_revision is not None:
            click.echo(
                "Error: --expected-revision is a direct-backend CAS option; "
                "served lifecycle commands already carry their immutable basis revision.",
                err=True,
            )
            sys.exit(1)
        _served_sprint_status(config, sprint_id, new_status, actor, as_json)
        return
    if expected_revision is None:
        raise click.UsageError("Missing option '--expected-revision' for direct sprint status.")
    store, m = _get_store(obj)
    s = m.get_sprint(store, sprint_id)
    if s is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    current = s["status"]
    boundary_event_id = None
    actor = (actor or os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown").strip()
    try:
        if new_status == "closed":
            boundary_event_id = m.close_sprint_with_boundary_event(
                store, sprint_id, actor, expected_revision=expected_revision
            )
        else:
            m.set_sprint_status(
                store, sprint_id, new_status, expected_revision=expected_revision
            )
    except (_db.InvalidTransition, _db.StatusConflict, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if new_status == "active":
        _emit_audit_event(
            "sprint.opened",
            summary=f"Sprint {sprint_id} opened",
            refs=[f"sprint:{sprint_id}"],
            metadata={"sprint_id": sprint_id, "event_type": "sprint-opened"},
        )
    elif new_status == "closed":
        _emit_audit_event(
            "sprint.closed",
            summary=f"Sprint {sprint_id} closed",
            refs=[f"sprint:{sprint_id}"],
            metadata={
                "sprint_id": sprint_id,
                "event_type": "sprint-closed",
                "boundary_event_id": boundary_event_id,
                "boundary_revision": f"event:{boundary_event_id}",
                "actor": actor,
            },
        )
    if as_json:
        payload = {"sprint_id": sprint_id, "previous": current, "status": new_status}
        if boundary_event_id is not None:
            payload["boundary_event_id"] = boundary_event_id
            payload["boundary_revision"] = f"event:{boundary_event_id}"
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Sprint #{sprint_id} status: {current} -> {new_status}")
    if boundary_event_id is not None:
        click.echo(
            f"Sprint-close boundary event #{boundary_event_id} "
            f"(revision event:{boundary_event_id})"
        )


@sprint.command("list")
@click.option("--include-backlog", is_flag=True, default=False, help="Include backlog sprints")
@click.option("--include-archive", is_flag=True, default=False, help="Include archive sprints")
@click.option("--active", "active_only", is_flag=True, default=False, help="Show active active_sprint sprints")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path),
    is_flag=False,
    flag_value=Path("."),
    help="Union backlog repositories from project.toml (a directory resolves upward).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_list(obj, include_backlog, include_archive, active_only, project_path, as_json) -> None:
    """List sprints (active_sprint kind by default; use flags to include others)."""
    config = _served_config_or_none(obj)
    project_unavailable: list[dict] = []
    if config is not None:
        if project_path is not None:
            # ``project_path`` is presence-only in served mode.  Never load
            # the client's project.toml: Vuoro owns the canonical binding and
            # per-member authorization for this aggregate.
            project_result = _run_served(
                "sprint list --project",
                _served.project_sprints,
                config.served_profile,
                include_backlog=include_backlog,
                include_archive=include_archive,
                active_only=active_only,
                resolved_context=_resolved_context(config),
            )
            sprints: list[dict] = project_result["sprints"]
            project_unavailable = [
                entry for entry in project_result["repositories"]
                if entry["status"] == "unavailable"
            ]
        else:
            result = _run_served(
                "sprint list",
                _served.read_sprints,
                config.served_profile,
                repo_id=config.repo_id,
                include_backlog=include_backlog,
                include_archive=include_archive,
                active_only=active_only,
                resolved_context=_resolved_context(config),
            )
            sprints = result["sprints"]
    else:
        if project_path is None:
            scopes = [(None, *_get_store(obj))]
        else:
            _binding, scopes = _get_project_stores(obj, project_path)

        sprints = []
        for repo_id, store, m in scopes:
            if active_only:
                scoped_sprints = m.list_active_sprints(store)
            else:
                scoped_sprints = m.list_sprints(store)
            if repo_id is not None:
                scoped_sprints = [_with_origin(sprint, repo_id) for sprint in scoped_sprints]
            sprints.extend(scoped_sprints)

    if not active_only:
        visible_kinds = {"active_sprint"}
        if include_backlog:
            visible_kinds.add("backlog")
        if include_archive:
            visible_kinds.add("archive")
        sprints = [s for s in sprints if s.get("kind", "active_sprint") in visible_kinds]
    if as_json:
        click.echo(json.dumps(sprints, indent=2))
        return
    if not sprints:
        click.echo("No sprints found.")
        if config is not None:
            click.echo(_render_resolved_context(_resolved_context(config)))
        return
    rows: list[list[str]] = []
    for s in sprints:
        kind = s.get("kind", "active_sprint")
        dates = (
            f"{s['start_date']} to {s['end_date']}"
            if s.get("start_date") and s.get("end_date")
            else "-"
        )
        rows.append(
            [
                f"#{s['id']}",
                *([s["origin_repo"]] if project_path is not None else []),
                _style_status(s["status"]),
                kind,
                s["name"],
                dates,
            ]
        )
    headers = ["ID"]
    if project_path is not None:
        headers.append("ORIGIN_REPO")
    headers.extend(["STATUS", "KIND", "NAME", "DATES"])
    for line in _render_table(headers, rows):
        click.echo(line)
    for entry in project_unavailable:
        click.echo(f"Unavailable {entry['origin_repo']}: {entry['message']}")
    if config is not None:
        click.echo(_render_resolved_context(_resolved_context(config)))


@sprint.command("kind")
@click.option("--id", "sprint_id", type=str, required=True, help="Sprint ID or repo#id")
@click.option(
    "--kind",
    required=True,
    type=click.Choice(["active_sprint", "backlog", "archive"]),
    help="New kind",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_kind_cmd(obj, sprint_id, kind, as_json) -> None:
    """Set the kind classification of a sprint."""
    sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    store, m = _get_store(obj)
    try:
        m.set_sprint_kind(store, sprint_id, kind)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps({"sprint_id": sprint_id, "kind": kind}, indent=2))
        return
    click.echo(f"Sprint #{sprint_id} kind set to: {kind}")


@sprint.command("backlog-seed")
@click.option("--from-sprint-id", "source_sprint_id", type=str, required=True,
              help="Sprint ID or repo#id to read knowledge candidates from")
@click.option("--to-sprint-id", "target_sprint_id", type=str, required=True,
              help="Sprint ID or repo#id (backlog) to seed items into")
@click.option("--actor", default="system", help="Actor name (default: system)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output seeded items as JSON")
@click.pass_obj
def sprint_backlog_seed(obj, source_sprint_id, target_sprint_id, actor, as_json) -> None:
    """Seed backlog items from knowledge candidate events in another sprint."""
    source_sprint_id = _apply_scoped_id(obj, source_sprint_id, field="sprint")
    target_sprint_id = _apply_scoped_id(obj, target_sprint_id, field="sprint")
    store, m = _get_store(obj)
    try:
        seeded = m.backlog_seed_from_candidates(store, source_sprint_id, target_sprint_id, actor=actor)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(seeded, indent=2))
        return
    if not seeded:
        click.echo(f"No new items seeded (0 candidates or all already seeded).")
        return
    click.echo(f"Seeded {len(seeded)} item(s) into sprint #{target_sprint_id}:")
    for it in seeded:
        click.echo(f"  #{it['id']}  {it['title']}")


# ---------------------------------------------------------------------------
# item
# ---------------------------------------------------------------------------

@click.group()
def item() -> None:
    """Manage work items."""


@item.command("add")
@click.option("--sprint-id", type=str, required=True, help="Sprint ID or repo#id")
@click.option("--track", "track_name", required=True, help="Track name (created if absent)")
@click.option("--title", required=True, help="Item title")
@click.option("--description", default=None, help="Non-empty implementation scope or objective")
@click.option("--assignee", default=None, help="Assignee name")
@click.option(
    "--priority", type=int, default=None,
    help="Priority 1-9 (1 = highest; omit for unprioritized)",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output created item as JSON")
@click.pass_obj
def item_add(obj, sprint_id: str, track_name, title, description, assignee, priority, as_json) -> None:
    """Add a work item to a sprint track."""
    sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    if description is not None:
        try:
            _db.validate_work_item_description(description)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--description") from exc
    if priority is not None:
        try:
            _db.validate_priority(priority)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--priority") from exc
    config = _served_config_or_none(obj)
    if config is not None:
        context = _resolved_context(config)
        result = _run_served(
            "item add", _served.item_create, config.served_profile,
            repo_id=config.repo_id, sprint_id=sprint_id, track_name=track_name,
            title=title, description=description, assignee=assignee, priority=priority,
            resolved_context=context,
        )
        created = {**result["item"], "track_name": result["track_name"]}
        if as_json:
            click.echo(json.dumps(created, indent=2))
            return
        click.echo(f"Added item #{created['id']}: {created['title']}  [track: {created['track_name']}]")
        click.echo(_render_resolved_context(context))
        return
    store, m = _get_store(obj)
    s = m.get_sprint(store, sprint_id)
    if s is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    track_id = m.get_or_create_track(store, sprint_id, track_name)
    item_id = m.create_work_item(
        store,
        sprint_id,
        track_id,
        title,
        description=description or "",
        assignee=assignee,
        priority=priority,
    )
    if as_json:
        item = m.get_work_item(store, item_id)
        assert item is not None
        payload = {**item, "track_name": track_name}
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Added item #{item_id}: {title}  [track: {track_name}]")


@item.command("edit")
@click.option("--id", "item_id", type=str, required=True, help="Item ID or repo#id")
@click.option("--description", required=True, help="Non-empty implementation scope or objective")
@click.option("--actor", default=None, help="Actor name (default: actor)")
@click.option(
    "--expected-revision",
    default=None,
    help="Expected description revision (defaults to a fresh item read)",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output updated item as JSON")
@click.pass_obj
def item_edit(obj, item_id: str, description, actor, expected_revision, as_json) -> None:
    """Replace a work item's description with revision protection."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    try:
        _db.validate_work_item_description(description)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--description") from exc

    config = _served_config_or_none(obj)
    context = _resolved_context(obj["backend_config"])
    if config is not None:
        if not expected_revision:
            current = _run_served(
                "item show",
                _served.read_item,
                config.served_profile,
                repo_id=config.repo_id,
                item_id=item_id,
                resolved_context=context,
            )
            expected_revision = current["item"]["edit_revision"]
        result = _run_served(
            "item edit",
            _served.item_edit,
            config.served_profile,
            repo_id=config.repo_id,
            item_id=item_id,
            description=description,
            expected_revision=expected_revision,
            resolved_context=context,
        )
        updated = {**result["item"], "edit_revision": result["revision"]}
        if as_json:
            click.echo(json.dumps(updated, indent=2))
            return
        click.echo(_item_edit_success_message(item_id, result))
        click.echo(_render_resolved_context(context))
        return

    store, m = _get_store(obj)
    current = m.get_work_item_with_edit_revision(store, item_id)
    if current is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    _existing, current_revision = current
    try:
        result = m.update_work_item_description(
            store,
            item_id,
            description,
            expected_revision=expected_revision or current_revision,
            actor=actor or "actor",
        )
    except _db.EditConflict as exc:
        click.echo(f"Error: item-edit-conflict: {exc}", err=True)
        sys.exit(1)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    updated = {**result["item"], "edit_revision": result["revision"]}
    if as_json:
        click.echo(json.dumps(updated, indent=2))
        return
    click.echo(_item_edit_success_message(item_id, result))


def _item_edit_success_message(item_id: int, result: dict) -> str:
    """Render the backend-independent successful edit summary."""
    return (
        f"Updated item #{item_id} description "
        f"({result['previous_revision']} -> {result['revision']})."
    )


@item.command("priority")
@click.option("--id", "item_id", type=str, required=True, help="Item ID or repo#id")
@click.option("--set", "priority", type=int, default=None, help="Priority 1-9 (1 = highest)")
@click.option("--clear", is_flag=True, default=False, help="Clear the priority (unprioritized)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output updated item as JSON")
@click.pass_obj
def item_priority(obj, item_id: str, priority, clear, as_json) -> None:
    """Set or clear a work item's native priority.

    Priority orders next-work suggestions (1 first, unprioritized last) and
    replaces the legacy [pN] title-prefix convention, which remains recognized
    as a fallback when no native priority is set.
    """
    item_id = _apply_scoped_id(obj, item_id, field="item")
    if (priority is None) == (not clear):
        click.echo("Error: pass exactly one of --set N or --clear.", err=True)
        sys.exit(1)
    if priority is not None:
        try:
            _db.validate_priority(priority)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--set") from exc

    store, m = _get_store(obj)
    try:
        m.set_work_item_priority(store, item_id, None if clear else priority)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    updated = m.get_work_item(store, item_id)
    assert updated is not None
    if as_json:
        click.echo(json.dumps(updated, indent=2))
        return
    if clear:
        click.echo(f"Cleared priority on item #{item_id}.")
    else:
        click.echo(f"Set item #{item_id} priority to p{priority}.")


# ---------------------------------------------------------------------------
# guarded projection-backed reads
#
# Feature-flagged read path: when enabled per repository, some CLI read
# surfaces are served from the cached projection populated by the normal sync
# path (sprintctl/sync.py) instead of hitting
# backend (SQLite/PostgreSQL) directly.  A surface only actually reads from
# the projection when (a) the flag is enabled, (b) the cache is healthy
# (matching schema version, synchronized at least once, not stale), and
# (c) that specific surface has a projection-backed implementation.  Any
# other case falls back to backend mode explicitly and says so in both
# --json and text output, never silently.
#
# Only sprintctl/projection.py's existing cached ingest records are used as
# the data source; this module builds no new authoritative state and cannot
# write anything.  Rollback is always available per repository:
#   sprintctl projection-reads disable
# or by unsetting SPRINTCTL_PROJECTION_READS -- either returns every read
# surface below to its current backend-only behavior.
# ---------------------------------------------------------------------------

_PROJECTION_STALE_SECONDS_ENV = "SPRINTCTL_PROJECTION_STALE_SECONDS"


def _projection_stale_after_seconds() -> int:
    raw = os.environ.get(_PROJECTION_STALE_SECONDS_ENV)
    if raw is None:
        return _projection.DEFAULT_STALE_AFTER_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _projection.DEFAULT_STALE_AFTER_SECONDS
    return value if value > 0 else _projection.DEFAULT_STALE_AFTER_SECONDS


def _projection_health(*, cwd: Path | None = None) -> dict:
    """Resolve whether projection reads are enabled and, if so, the cached
    projection's freshness -- independent of any particular read surface.

    Returned ``health`` is one of: "disabled", "missing",
    "schema-upgrade-required", "never-synchronized", "stale", "healthy".
    """
    cwd = cwd or Path.cwd()
    base = {
        "enabled": False,
        "health": "disabled",
        "watermark_offset": None,
        "watermark_age_seconds": None,
        "schema_version": None,
        "stale_after_seconds": _projection_stale_after_seconds(),
        "projection_path": None,
    }
    try:
        reads_status = _projection_reads.projection_reads_status(cwd=cwd)
    except _projection_reads.ProjectionReadsConfigError:
        return base
    if not reads_status.enabled:
        return base
    base["enabled"] = True
    try:
        paths = _sync.repository_sync_paths(cwd=cwd)
    except ValueError:
        base["health"] = "missing"
        return base
    path = paths.projection_path
    base["projection_path"] = str(path)
    if not path.exists():
        base["health"] = "missing"
        return base
    conn = _projection.open_cached_projection(path)
    try:
        freshness = _projection.assess_freshness(conn, stale_after_seconds=base["stale_after_seconds"])
    finally:
        conn.close()
    base["watermark_offset"] = freshness.watermark.ingest_offset
    base["watermark_age_seconds"] = freshness.age_seconds
    base["schema_version"] = freshness.schema_version
    base["health"] = "healthy" if freshness.healthy else freshness.fallback_reason
    return base


def _projection_surface_status(health: dict, *, supported: bool) -> dict:
    """Combine cache health with whether this read surface is wired to it.

    ``source`` is "projection" only when the flag is on, the cache is
    healthy, and this surface has projection-backed content implemented.
    Every other combination reports "backend" plus an explicit
    ``fallback_reason`` -- never a silent fallback.
    """
    status = {
        "enabled": health["enabled"],
        "source": "backend",
        "fallback_reason": None,
        "watermark_offset": health["watermark_offset"],
        "watermark_age_seconds": health["watermark_age_seconds"],
        "schema_version": health["schema_version"],
    }
    if not health["enabled"]:
        return status
    if health["health"] != "healthy":
        status["fallback_reason"] = health["health"]
        return status
    if not supported:
        status["fallback_reason"] = "unsupported-read-surface"
        return status
    status["source"] = "projection"
    return status


def _projection_status_line(status: dict) -> str | None:
    """One human-readable disclosure line, or None if the flag is off."""
    if not status["enabled"]:
        return None
    if status["source"] == "projection":
        age = status["watermark_age_seconds"]
        age_text = f"{age:.0f}s" if age is not None else "unknown"
        return (
            f"Projection: source=projection watermark_offset={status['watermark_offset']} "
            f"age={age_text}"
        )
    return f"Projection: source=backend fallback={status['fallback_reason']}"


def _projection_item_events(projection_path: Path, item_id: int) -> list[dict]:
    """Reconstruct one item's observation-event history from the cache.

    Only observation-classified events appended to normal synchronization
    are present here; authority-changing
    fields on the item itself (status, title, assignee, ...) are never
    mirrored and are never reconstructed by this function.  Ordering and
    field shape match ``db.list_events`` / ``pg.list_events`` filtered to one
    item, so a healthy cache produces the same events a backend read would.
    """
    conn = _projection.open_cached_projection(projection_path)
    try:
        cached_records = _projection.list_cached_records(conn)
    finally:
        conn.close()
    events: list[dict] = []
    for cached in cached_records:
        envelope = cached.record.get("payload")
        if not isinstance(envelope, dict):
            continue
        refs = envelope.get("refs") or {}
        if refs.get("work_item_id") != item_id:
            continue
        inner_payload = envelope.get("payload") or {}
        events.append({
            "id": refs.get("authority_event_id"),
            "sprint_id": refs.get("sprint_id"),
            "work_item_id": refs.get("work_item_id"),
            "source_type": inner_payload.get("source_type"),
            "actor": envelope.get("actor"),
            "event_type": envelope.get("record_type"),
            "payload": inner_payload.get("event_payload"),
            "created_at": envelope.get("authored_at"),
        })
    events.sort(key=lambda e: (e["created_at"] or "", e["id"] or 0))
    return events


@item.command("show")
@click.option("--id", "item_id", type=str, required=True, help="Item ID or repo#id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_show(obj, item_id: str, as_json) -> None:
    """Show a single work item with its recent events and active reservations."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    context = _resolved_context(obj["backend_config"])
    projection_status = None
    if config is not None:
        result = _run_served(
            "item show",
            _served.read_item,
            config.served_profile,
            repo_id=config.repo_id,
            item_id=item_id,
            resolved_context=context,
        )
        it = result["item"]
        item_events = result["events"]
        reservations = result["active_reservations"]
        refs = result["refs"]
        blocking = result["deps"]["blocked_by"]
        blocked_by_me = result["deps"]["blocks"]
    else:
        store, m = _get_store(obj)
        current = m.get_work_item_with_edit_revision(store, item_id)
        if current is None:
            click.echo(
                f"Item #{item_id} not found.\n{_render_resolved_context(context)}",
                err=True,
            )
            sys.exit(1)
        it, edit_revision = current
        it = {
            **it,
            "edit_revision": edit_revision,
            "status_revision": m.item_status_revision(it),
        }

        # Item core fields (status, title, assignee, ...) only ever change via
        # authority commands, which the shadow pilot never mirrors -- so they
        # always come from backend regardless of the flag. The event/notes
        # history below is the one sub-section the cached projection can
        # honestly reconstruct, because item note/event observations are what
        # gets mirrored.
        projection_health = _projection_health()
        projection_status = _projection_surface_status(projection_health, supported=True)
        if projection_status["source"] == "projection":
            item_events = _projection_item_events(Path(projection_health["projection_path"]), item_id)
        else:
            events = m.list_events(store, it["sprint_id"])
            item_events = [e for e in events if e.get("work_item_id") == item_id]

        reservations = m.list_reservations(store, item_id, active_only=True)
        refs = m.list_refs(store, item_id)
        blocking = m.list_deps_blocking(store, item_id)
        blocked_by_me = m.list_deps_blocked_by(store, item_id)

    if as_json:
        payload = {
            "item": dict(it),
            "events": item_events,
            "active_reservations": reservations,
            "refs": refs,
            "deps": {"blocked_by": blocking, "blocks": blocked_by_me},
            "resolved_context": context,
        }
        if projection_status is not None:
            payload["projection"] = projection_status
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"{context['repo_id']}#{it['id']}  [{it['status']}]  {it['title']}")
    click.echo(_render_resolved_context(context))
    if projection_status is not None:
        status_line = _projection_status_line(projection_status)
        if status_line:
            click.echo(f"  {status_line}")
    click.echo(f"  Sprint:   #{it['sprint_id']}")
    track_name = it.get("track_name", "")
    if track_name:
        click.echo(f"  Track:    {track_name}")
    assignee = it.get("assignee") or "-"
    click.echo(f"  Assignee: {assignee}")
    description = it.get("description") or "-"
    click.echo(f"  Description: {description}")
    click.echo(f"  Updated:  {it['updated_at']}")

    if refs:
        click.echo("\nRefs:")
        for r in refs:
            label = f"  {r['label']}" if r["label"] else ""
            click.echo(f"  #{r['id']}  [{r['ref_type']}]  {r['url']}{label}")
    else:
        click.echo(
            f"\nRefs: (none — attach the spec/plan doc with "
            f"'sprintctl item ref add --id {item_id} --type doc --url docs/<path>')"
        )

    if blocking:
        click.echo("\nBlocked by:")
        for d in blocking:
            click.echo(f"  #{d['item_id']}  [{d['blocker_status']}]  {d['blocker_title']}")
    if blocked_by_me:
        click.echo("\nBlocks:")
        for d in blocked_by_me:
            click.echo(f"  #{d['blocked_item_id']}  [{d['waiting_status']}]  {d['waiting_title']}")

    if reservations:
        click.echo("\nActive reservations:")
        for reservation in reservations:
            parts = [
                f"  #{reservation['id']}  {reservation['actor']}  "
                f"[{reservation['role']}]  session={reservation['session_id']}"
            ]
            if reservation.get("correlation_ref"):
                parts.append(f"  correlation={reservation['correlation_ref']}")
            click.echo("".join(parts))

    if item_events:
        click.echo("\nEvents:")
        for e in item_events[-10:]:
            click.echo(f"  #{e['id']}  [{e['event_type']}]  {e['actor']}  {e['created_at']}")
    else:
        click.echo("\nEvents: (none)")


@item.command("list")
@click.option("--sprint-id", type=str, default=None, help="Filter by sprint ID or repo#id")
@click.option("--track", "track_name", default=None, help="Filter by track name")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "active", "done", "blocked"]),
    help="Filter by status",
)
@click.option(
    "--fzf",
    "as_fzf",
    is_flag=True,
    default=False,
    help="Output one tab-separated item per line for fzf/pipe workflows",
)
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path),
    is_flag=False,
    flag_value=Path("."),
    help="Union backlog repositories from project.toml (a directory resolves upward).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_list(obj, sprint_id, track_name, status, as_fzf, project_path, as_json) -> None:
    """List work items."""
    if sprint_id is not None:
        sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    if as_json and as_fzf:
        click.echo("Error: --fzf cannot be combined with --json.", err=True)
        sys.exit(1)
    config = _served_config_or_none(obj)
    if config is not None:
        if as_fzf:
            _served_operation_unavailable(
                "item list --fzf",
                replacement="Use --json or the table output in served mode.",
            )
        if project_path is None:
            result = _run_served(
                "item list",
                _served.read_items,
                config.served_profile,
                repo_id=config.repo_id,
                sprint_id=sprint_id,
                track_name=track_name,
                status=status,
                resolved_context=_resolved_context(config),
            )
        else:
            result = _run_served(
                "project item list",
                _served.project_items,
                config.served_profile,
                sprint_id=sprint_id,
                track_name=track_name,
                status=status,
                resolved_context=_resolved_context(config),
            )
        items = result["items"]
        if as_json:
            click.echo(json.dumps(items, indent=2))
            return
        if not items:
            click.echo("No items found.")
            click.echo(_render_resolved_context(_resolved_context(config)))
            return
        rows = [[f"#{it['id']}", _style_status(it["status"]), _format_priority(it), it["track_name"], it.get("assignee") or "-", it["title"]] for it in items]
        for line in _render_table(["ID", "STATUS", "PRI", "TRACK", "ASSIGNEE", "TITLE"], rows):
            click.echo(line)
        click.echo(_render_resolved_context(_resolved_context(config)))
        return

    if project_path is None:
        scopes = [(None, *_get_store(obj))]
    else:
        _binding, scopes = _get_project_stores(obj, project_path)
    items: list[dict] = []
    for repo_id, store, m in scopes:
        scoped_items = m.list_work_items(
            store, sprint_id=sprint_id, track_name=track_name, status=status
        )
        if repo_id is not None:
            scoped_items = [_with_origin(item, repo_id) for item in scoped_items]
        items.extend(scoped_items)
    if as_json:
        # NOTE: this endpoint's JSON shape is a bare array and is relied on
        # by existing consumers (fzf pipelines, other tooling). Item listings
        # are not projection-backed (see module note above item_show) and
        # adding a "projection" key would change this into an incompatible
        # object shape, so freshness is intentionally not surfaced here.
        # Use `sprintctl projection-reads status --json` to check freshness
        # instead.
        click.echo(json.dumps(items, indent=2))
        return
    if as_fzf:
        for it in items:
            assignee = it.get("assignee") or "-"
            priority = _format_priority(it)
            origin = f"{_escape_fzf_field(it['origin_repo'])}\t" if project_path is not None else ""
            click.echo(
                f"{origin}#{it['id']}\t"
                f"{_escape_fzf_field(it['status'])}\t"
                f"{_escape_fzf_field(it['track_name'])}\t"
                f"{_escape_fzf_field(assignee)}\t"
                f"{_escape_fzf_field(it['title'])}\t"
                f"{_escape_fzf_field(priority)}"
            )
        return
    if project_path is None:
        status_line = _projection_status_line(
            _projection_surface_status(_projection_health(), supported=False)
        )
        if status_line:
            click.echo(status_line)
    if not items:
        click.echo("No items found.")
        return
    rows: list[list[str]] = []
    for it in items:
        assignee = it.get("assignee") or "-"
        rows.append(
            [
                f"#{it['id']}",
                *([it["origin_repo"]] if project_path is not None else []),
                _style_status(it["status"]),
                _format_priority(it),
                it["track_name"],
                assignee,
                it["title"],
            ]
        )
    headers = ["ID"]
    if project_path is not None:
        headers.append("ORIGIN_REPO")
    headers.extend(["STATUS", "PRI", "TRACK", "ASSIGNEE", "TITLE"])
    for line in _render_table(headers, rows):
        click.echo(line)


def _served_item_note(
    config, item_id, note_type, summary, detail, tags, actor,
    evidence_item_id, evidence_event_id, git_branch, git_sha, git_worktree,
) -> None:
    """Served-mode ``item note``: routes to ``work.item.note``.

    Unlike ``item status``/``sprint status``, this is not an authority
    command -- no outbox record is minted, and there is no basis-revision or
    idempotency-key concept to send. The recording actor is always the
    authenticated identity the server resolves from the credential; a
    caller-supplied ``--actor`` is accepted for parity with local mode but
    silently ignored server-side, exactly like ``claim start``'s actor.
    """

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    context = _resolved_context(config)
    result = _run_served(
        "item note",
        _served.item_note,
        config.served_profile,
        repo_id=config.repo_id,
        item_id=item_id,
        note_type=note_type,
        summary=summary,
        detail=detail,
        tags=tag_list,
        evidence_item_id=evidence_item_id,
        evidence_event_id=evidence_event_id,
        git_branch=git_branch,
        git_sha=git_sha,
        git_worktree=git_worktree,
        resolved_context=context,
    )
    click.echo(
        f"Recorded note #{result['event_id']} ({result['note_type']}) "
        f"on item #{result['item_id']}: {result['summary']}"
    )
    click.echo(_render_resolved_context(context))


@item.command("note")
@click.option("--id", "item_id", type=str, required=True, help="Work item ID or repo#id")
@click.option("--type", "note_type", required=True, help="Note type (e.g. decision, blocker, update)")
@click.option("--summary", required=True, help="Short summary")
@click.option("--detail", default=None, help="Extended detail")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option("--actor", default="actor", help="Actor name (default: actor)")
@click.option("--evidence-item-id", type=str, default=None, help="Work item ID or repo#id this knowledge came from")
@click.option("--evidence-event-id", type=int, default=None, help="Event ID this knowledge came from")
@click.option("--git-branch", default=None, help="Git branch name at time of note")
@click.option("--git-sha", default=None, help="Git commit SHA at time of note")
@click.option("--git-worktree", default=None, help="Git worktree path at time of note")
@click.pass_obj
def item_note(
    obj, item_id: str, note_type, summary, detail, tags, actor,
    evidence_item_id, evidence_event_id,
    git_branch, git_sha, git_worktree,
) -> None:
    """Record a structured note event on a work item."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    if evidence_item_id is not None:
        evidence_item_id = _apply_scoped_id(obj, evidence_item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        _served_item_note(
            config, item_id, note_type, summary, detail, tags, actor,
            evidence_item_id, evidence_event_id, git_branch, git_sha, git_worktree,
        )
        return
    store, m = _get_store(obj)
    it = m.get_work_item(store, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    payload: dict = {"summary": summary}
    if detail:
        payload["detail"] = detail
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if evidence_item_id is not None:
        payload["evidence_item_id"] = evidence_item_id
    if evidence_event_id is not None:
        payload["evidence_event_id"] = evidence_event_id
    if git_branch is not None:
        payload["git_branch"] = git_branch
    if git_sha is not None:
        payload["git_sha"] = git_sha
    if git_worktree is not None:
        payload["git_worktree"] = git_worktree
    eid = m.create_event(
        store,
        it["sprint_id"],
        actor=actor,
        event_type=note_type,
        source_type="actor",
        work_item_id=item_id,
        payload=payload,
    )
    if note_type in _db.KNOWLEDGE_EVENT_TYPES:
        _emit_audit_event(
            "knowledge.landed",
            summary=f"Knowledge event #{eid} ({note_type}) on item #{item_id}",
            refs=[f"sprint:{it['sprint_id']}", f"ka:{eid}"],
            metadata={
                "sprint_id": it["sprint_id"],
                "event_type": "knowledge-landed",
                "knowledge_event_id": eid,
                "note_type": note_type,
            },
        )
    click.echo(f"Recorded note #{eid} ({note_type}) on item #{item_id}: {summary}")


def _served_item_status(config, item_id, new_status, actor, claim_id, claim_token, as_json) -> None:
    """Run one immutable served item transition, with proof kept transient."""
    context = _resolved_context(config)
    if (claim_id is None) != (claim_token is None):
        click.echo("Error: --claim-id and --claim-token must be supplied together.", err=True)
        sys.exit(1)

    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    record_type = "item.done" if new_status == "done" else "item.transition"
    durable = _find_pending_served_item_status_record(
        rollout_paths.outbox_path, record_type=record_type,
        item_id=item_id, to_status=new_status,
    )
    credentials: dict[str, str] = {}
    if durable is not None:
        command = _contracts.record_from_dict(durable.payload)
        assert isinstance(command, _contracts.AuthorityCommand)
        expected_id = command.payload.get("claim_id")
        expected_ref = command.payload.get("credential_ref")
        supplied_ref = _authority.credential_ref(claim_token) if claim_token is not None else None
        if expected_id != claim_id or expected_ref != supplied_ref:
            click.echo(
                f"Error: durable item status request {durable.event_id} requires "
                "the original claim proof; do not mint a new request.", err=True,
            )
            sys.exit(1)
        if expected_ref is not None:
            assert claim_token is not None
            credentials[expected_ref] = claim_token
        current = command.basis_revision.rsplit("@status:", 1)[-1]
    else:
        read_result = _run_served(
            "item status", _served.read_item, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, resolved_context=context,
        )
        it = read_result["item"]
        current = it["status"]

    identity = _run_served(
        "item status", _served.identity_current, config.served_profile,
        repo_id=config.repo_id, resolved_context=context,
    )
    actor_value = identity["actor"]
    if actor is not None and actor != actor_value:
        click.echo(
            f"Note: served mode records the authenticated identity "
            f"({actor_value}); --actor {actor!r} was not sent and is ignored.", err=True,
        )

    if durable is None:
        payload: dict[str, object] = {"to_status": new_status}
        if claim_id is not None:
            assert claim_token is not None
            ref = _authority.credential_ref(claim_token)
            payload.update({"claim_id": claim_id, "credential_ref": ref})
            credentials[ref] = claim_token
        try:
            durable = _mint_authority_command_record(
                record_type=record_type, actor=actor_value,
                refs={
                    "repo_id": _authority_repo_uuid(rollout_paths.repo_root),
                    "aggregate_type": "item", "aggregate_uuid": it["aggregate_uuid"],
                    "aggregate_id": item_id,
                },
                payload=payload, basis_revision=_authority.item_revision(it),
                outbox_path=rollout_paths.outbox_path,
            )
        except (TypeError, ValueError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
    try:
        decision = _served.lifecycle_arbitrate(
            config.served_profile, repo_id=config.repo_id,
            record=_served_record_argument(durable),
            **({"transient_credentials": credentials} if credentials else {}),
        )
    except Exception as exc:
        click.echo(
            "Error: served item status failed after preserving durable authority request "
            f"{durable.event_id} (origin stream {durable.origin_stream_id}, "
            f"sequence {durable.origin_seq}): {exc}. Retry this exact command with "
            "the original claim proof; do not mint a new request.", err=True,
        )
        sys.exit(1)
    _authority_config.mark_terminal_authority_decision(
        rollout_paths, event_id=durable.event_id, outcome=decision["outcome"]
    )
    if decision["outcome"] != "accepted":
        click.echo(
            f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}\n"
            f"{_render_resolved_context(context)}", err=True,
        )
        sys.exit(1)
    final_status = decision["effect"].get("status", new_status)
    if as_json:
        click.echo(json.dumps({"item_id": item_id, "previous": current, "status": final_status}, indent=2))
        return
    click.echo(f"Item #{item_id} status: {current} -> {final_status}")
    click.echo(_render_resolved_context(context))


@item.command("status")
@click.option("--id", "item_id", type=str, required=True, help="Item ID or repo#id")
@click.option(
    "--status",
    "new_status",
    required=True,
    type=click.Choice(["pending", "active", "done", "blocked"]),
    help="New status",
)
@click.option("--actor", default=None, help="Actor name")
@click.option("--claim-id", type=int, default=None, help="Claim ID to prove ownership of an active exclusive claim")
@click.option("--claim-token", default=None, help="Claim token proving ownership of an active exclusive claim")
@click.option(
    "--expected-revision",
    default=None,
    help="Required expected item status revision for direct local transitions",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output status transition as JSON")
@click.pass_obj
def item_status(
    obj, item_id: str, new_status, actor, claim_id, claim_token, expected_revision, as_json
) -> None:
    """Update an item's status (enforces transitions, claims, and dependency safety)."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        if expected_revision is not None:
            click.echo(
                "Error: --expected-revision is a direct-backend CAS option; "
                "served lifecycle commands already carry their immutable basis revision.",
                err=True,
            )
            sys.exit(1)
        _served_item_status(config, item_id, new_status, actor, claim_id, claim_token, as_json)
        return
    if expected_revision is None:
        raise click.UsageError("Missing option '--expected-revision' for direct item status.")
    store, m = _get_store(obj)
    it = m.get_work_item(store, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    current = it["status"]
    try:
        m.set_work_item_status(
            store,
            item_id,
            new_status,
            actor=actor,
            claim_id=claim_id,
            claim_token=claim_token,
            expected_revision=expected_revision,
        )
    except (_db.InvalidTransition, _db.ClaimConflict, _db.StatusConflict, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps({"item_id": item_id, "previous": current, "status": new_status}, indent=2))
        return
    click.echo(f"Item #{item_id} status: {current} -> {new_status}")


def _served_item_done_from_claim(config, item_id, claim_id, claim_token, actor, keep_claim, as_json) -> None:
    """Finish an execute claim through one durable lifecycle arbitration.

    The preliminary reads only obtain non-secret immutable context; the state
    change is a single ``work.lifecycle.arbitrate`` call carrying one durable
    command and its transient proof.  It must never be replaced by status and
    release catalog calls, which have an observable split-brain failure mode.
    """
    resolved_context = _resolved_context(config)
    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    pending = _find_pending_served_done_from_claim_record(
        rollout_paths.outbox_path, claim_id=claim_id, item_id=item_id,
        keep_claim=keep_claim,
    )
    if pending is not None:
        # Replay the original event *before* inspecting the claim.  In the
        # response-lost success case that claim has already been deleted.
        command = _contracts.record_from_dict(pending.payload)
        assert isinstance(command, _contracts.AuthorityCommand)
        expected_ref = command.payload["credential_ref"]
        supplied_ref = _authority.credential_ref(claim_token)
        if supplied_ref != expected_ref:
            click.echo(
                f"Error: durable item done-from-claim request {pending.event_id} "
                "requires the original claim proof; do not mint a new request.",
                err=True,
            )
            sys.exit(1)
        try:
            proof = _authority_config.load_pending_authority_credential(
                rollout_paths, event_id=pending.event_id,
            )
            # A crash between append and sidecar persistence is recoverable
            # while the caller still possesses the exact proof.  Restore the
            # sidecar under the original event id, never mint a later record.
            if proof is None:
                _authority_config.store_pending_authority_credentials(
                    rollout_paths, event_id=pending.event_id,
                    credentials={expected_ref: claim_token},
                )
                proof = _authority_config.load_pending_authority_credential(
                    rollout_paths, event_id=pending.event_id,
                )
            assert proof is not None
        except _authority_config.AuthorityCommandConfigError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        decision = _run_served(
            "item done-from-claim", _served.lifecycle_arbitrate, config.served_profile,
            repo_id=config.repo_id, record=_served_record_argument(pending),
            transient_credentials=dict(proof.credentials), resolved_context=resolved_context,
        )
        _authority_config.mark_terminal_authority_decision(
            rollout_paths, event_id=pending.event_id, outcome=decision["outcome"]
        )
        _authority_config.remove_pending_authority_credential(
            rollout_paths, event_id=pending.event_id
        )
        _render_served_done_from_claim_decision(
            decision, item_id=item_id or int(command.refs["aggregate_id"]), claim_id=claim_id,
            keep_claim=keep_claim, as_json=as_json, resolved_context=resolved_context,
        )
        return

    claim_context = _run_served(
        "item done-from-claim", _served.claim_context, config.served_profile,
        repo_id=config.repo_id, claim_id=claim_id, resolved_context=resolved_context,
    )
    claim = claim_context["claim"]
    inferred_item_id = int(claim["work_item_id"])
    if item_id is None:
        item_id = inferred_item_id
    if item_id != inferred_item_id:
        click.echo(f"Error: claim #{claim_id} belongs to item #{inferred_item_id}, not item #{item_id}.", err=True)
        sys.exit(1)
    item_result = _run_served(
        "item done-from-claim", _served.read_item, config.served_profile,
        repo_id=config.repo_id, item_id=item_id, resolved_context=resolved_context,
    )
    item_value = item_result["item"]
    authenticated_actor = claim_context["actor"]
    if actor is not None and actor != authenticated_actor:
        click.echo(f"Note: served mode claims as the authenticated identity ({authenticated_actor}); --actor {actor!r} was not sent and is ignored.", err=True)
    ref = _authority.credential_ref(claim_token)
    credentials = {ref: claim_token}
    try:
        durable = _mint_authority_command_record(
            record_type="item.done-from-claim", actor=authenticated_actor,
            refs={
                "repo_id": _served_claim_authority_repo_uuid(claim_context, rollout_paths.repo_root),
                "aggregate_type": "item", "aggregate_uuid": item_value["aggregate_uuid"],
                "aggregate_id": item_id,
            },
            payload={"claim_id": claim_id, "credential_ref": ref, "keep_claim": keep_claim},
            basis_revision=_authority.item_revision(item_value), outbox_path=rollout_paths.outbox_path,
        )
        _authority_config.store_pending_authority_credentials(
            rollout_paths, event_id=durable.event_id, credentials=credentials,
        )
    except (TypeError, ValueError, _authority_config.AuthorityCommandConfigError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    decision = _run_served(
        "item done-from-claim", _served.lifecycle_arbitrate, config.served_profile,
        repo_id=config.repo_id, record=_served_record_argument(durable),
        transient_credentials=credentials, resolved_context=resolved_context,
    )
    _authority_config.mark_terminal_authority_decision(
        rollout_paths, event_id=durable.event_id, outcome=decision["outcome"]
    )
    _authority_config.remove_pending_authority_credential(rollout_paths, event_id=durable.event_id)
    _render_served_done_from_claim_decision(
        decision, item_id=item_id, claim_id=claim_id, keep_claim=keep_claim,
        as_json=as_json, resolved_context=resolved_context,
    )


def _render_served_done_from_claim_decision(
    decision, *, item_id, claim_id, keep_claim, as_json, resolved_context,
) -> None:
    if decision["outcome"] != "accepted":
        click.echo(f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}\n{_render_resolved_context(resolved_context)}", err=True)
        sys.exit(1)
    effect = decision["effect"]
    payload = {
        "operation": "item_done_from_claim", "item_id": effect["item_id"],
        "item_status_before": effect["previous_status"], "item_status_after": effect["status"],
        "claim_id": claim_id, "claim_released": effect["claim_released"],
        "claim_still_present": effect["claim_still_present"], "keep_claim": effect["keep_claim"],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Item #{item_id} status: {payload['item_status_before']} -> {payload['item_status_after']}")
    click.echo(_render_resolved_context(resolved_context))


@item.command("done-from-claim")
@click.option("--id", "item_id", type=str, default=None, help="Item ID or repo#id (defaults to the claim's item)")
@click.option("--claim-id", type=int, required=True, help="Claim ID proving ownership")
@click.option("--claim-token", required=True, help="Claim token proving ownership")
@click.option("--actor", default=None, help="Actor name")
@click.option(
    "--keep-claim",
    is_flag=True,
    default=False,
    help="Do not release the claim after marking the item done",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output operation result as JSON")
@click.pass_obj
def item_done_from_claim(obj, item_id, claim_id, claim_token, actor, keep_claim, as_json) -> None:
    """Mark an active item done using claim proof, then optionally release the claim."""
    if item_id is not None:
        item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        _served_item_done_from_claim(
            config, item_id, claim_id, claim_token, actor, keep_claim, as_json
        )
        return
    store, m = _get_store(obj)
    claim = m.get_claim(store, claim_id)
    if claim is None:
        click.echo(f"Claim #{claim_id} not found.", err=True)
        sys.exit(1)
    if item_id is None:
        item_id = claim["work_item_id"]
    if claim["work_item_id"] != item_id:
        click.echo(
            f"Error: claim #{claim_id} belongs to item #{claim['work_item_id']}, not item #{item_id}.",
            err=True,
        )
        sys.exit(1)
    it = m.get_work_item(store, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    if claim["claim_type"] != "execute" or not bool(claim["exclusive"]):
        click.echo(
            "Error: done-from-claim requires an active exclusive execute claim.",
            err=True,
        )
        sys.exit(1)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if claim["expires_at"] <= now_utc:
        click.echo(
            f"Error: claim #{claim_id} is expired ({claim['expires_at']}). Refresh or re-claim first.",
            err=True,
        )
        sys.exit(1)

    previous_status = it["status"]
    try:
        m.set_work_item_status(
            store,
            item_id,
            "done",
            actor=actor,
            claim_id=claim_id,
            claim_token=claim_token,
        )
    except (_db.InvalidTransition, _db.ClaimConflict, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    claim_released = False
    release_error = None
    if not keep_claim:
        try:
            m.release_claim(store, claim_id, claim_token, actor=actor)
            _remove_claim_recovery_record(claim_id)
            claim_released = True
        except ValueError as e:
            release_error = str(e)

    updated_item = m.get_work_item(store, item_id)
    assert updated_item is not None
    claim_still_present = m.get_claim(store, claim_id) is not None

    if as_json:
        payload = {
            "operation": "item_done_from_claim",
            "item_id": item_id,
            "item_status_before": previous_status,
            "item_status_after": updated_item["status"],
            "claim_id": claim_id,
            "claim_released": claim_released,
            "claim_still_present": claim_still_present,
            "keep_claim": keep_claim,
        }
        if release_error is not None:
            payload["release_error"] = release_error
        click.echo(json.dumps(payload, indent=2))
        if release_error is not None:
            sys.exit(1)
        return

    click.echo(f"Item #{item_id} status: {previous_status} -> {updated_item['status']}")
    if claim_released:
        click.echo(f"Claim #{claim_id} released.")
    elif keep_claim:
        click.echo(f"Claim #{claim_id} retained (--keep-claim).")
    if release_error is not None:
        click.echo(
            f"Error: item moved to done but claim release failed: {release_error}",
            err=True,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# item ref
# ---------------------------------------------------------------------------

@item.group("ref")
def item_ref() -> None:
    """Manage external references on a work item."""


@item_ref.command("add")
@click.option("--id", "item_id", type=str, required=True, help="Work item ID or repo#id")
@click.option(
    "--type", "ref_type",
    required=True,
    type=click.Choice(["pr", "issue", "doc", "other", "file", "glob", "manifest", "command"]),
    help="Reference type",
)
@click.option(
    "--url",
    required=True,
    help=("Reference target. Doc refs accept URLs or repo-relative paths; "
          "file, glob, and manifest refs require repo-relative POSIX paths; "
          "command refs hold a non-empty runnable shell command."),
)
@click.option("--label", default="", help="Short human-readable label")
@click.pass_obj
def item_ref_add(obj, item_id: str, ref_type, url, label) -> None:
    """Attach an external reference to a work item."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        result = _run_served("item ref add", _served.item_ref_add, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, ref_type=ref_type, url=url, label=label,
            resolved_context=_resolved_context(config))
        click.echo(f"Ref #{result['ref_id']} added to item #{item_id}: [{ref_type}] {url}")
        return
    store, m = _get_store(obj)
    try:
        ref_id = m.add_ref(store, item_id, ref_type, url, label)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Ref #{ref_id} added to item #{item_id}: [{ref_type}] {url}")


@item_ref.command("list")
@click.option("--id", "item_id", type=str, required=True, help="Work item ID or repo#id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_ref_list(obj, item_id: str, as_json) -> None:
    """List external references on a work item."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        result = _run_served("item ref list", _served.read_item, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, resolved_context=_resolved_context(config))
        refs = result["refs"]
        if as_json: click.echo(json.dumps(refs, indent=2))
        elif not refs: click.echo(f"No refs on item #{item_id}.")
        else:
            for r in refs: click.echo(f"  #{r['id']}  [{r['ref_type']}]  {r['url']}{'  ' + r['label'] if r['label'] else ''}")
        return
    store, m = _get_store(obj)
    if m.get_work_item(store, item_id) is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    refs = m.list_refs(store, item_id)
    if as_json:
        click.echo(json.dumps(refs, indent=2))
        return
    if not refs:
        click.echo(f"No refs on item #{item_id}.")
        return
    for r in refs:
        label = f"  {r['label']}" if r["label"] else ""
        click.echo(f"  #{r['id']}  [{r['ref_type']}]  {r['url']}{label}")


@item_ref.command("remove")
@click.option("--id", "item_id", type=str, required=True, help="Work item ID or repo#id")
@click.option("--ref-id", type=int, required=True, help="Ref ID to remove")
@click.pass_obj
def item_ref_remove(obj, item_id: str, ref_id) -> None:
    """Remove an external reference from a work item."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        _run_served("item ref remove", _served.item_ref_remove, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, ref_id=ref_id, resolved_context=_resolved_context(config))
        click.echo(f"Ref #{ref_id} removed from item #{item_id}.")
        return
    store, m = _get_store(obj)
    try:
        m.remove_ref(store, ref_id, item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Ref #{ref_id} removed from item #{item_id}.")


# ---------------------------------------------------------------------------
# item dep
# ---------------------------------------------------------------------------

@item.group("dep")
def item_dep() -> None:
    """Manage dependencies between work items."""


@item_dep.command("add")
@click.option("--id", "item_id", type=str, required=True, help="Blocker item ID or repo#id (must complete first)")
@click.option("--blocks-item-id", type=str, required=True, help="ID or repo#id of the item being blocked")
@click.pass_obj
def item_dep_add(obj, item_id: str, blocks_item_id: str) -> None:
    """Record that item --id must complete before --blocks-item-id can start."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    blocks_item_id = _apply_scoped_id(obj, blocks_item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        result = _run_served("item dep add", _served.item_dep_add, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, blocked_item_id=blocks_item_id,
            resolved_context=_resolved_context(config))
        click.echo(f"Dep #{result['dep_id']}: item #{item_id} blocks item #{blocks_item_id}")
        return
    store, m = _get_store(obj)
    try:
        dep_id = m.add_dep(store, item_id, blocks_item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Dep #{dep_id}: item #{item_id} blocks item #{blocks_item_id}")


@item_dep.command("list")
@click.option("--id", "item_id", type=str, required=True, help="Work item ID or repo#id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_dep_list(obj, item_id: str, as_json) -> None:
    """List dependencies for a work item (what blocks it and what it blocks)."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        result = _run_served("item dep list", _served.read_item, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, resolved_context=_resolved_context(config))
        blocking, blocked_by_me = result["deps"]["blocked_by"], result["deps"]["blocks"]
        if as_json: click.echo(json.dumps({"blocked_by": blocking, "blocks": blocked_by_me}, indent=2))
        elif not blocking and not blocked_by_me: click.echo(f"No dependencies on item #{item_id}.")
        else:
            for d in blocking: click.echo(f"Item #{item_id} is blocked by: #{d['item_id']}  [{d['blocker_status']}]  {d['blocker_title']}  (dep #{d['id']})")
            for d in blocked_by_me: click.echo(f"Item #{item_id} blocks: #{d['blocked_item_id']}  [{d['waiting_status']}]  {d['waiting_title']}  (dep #{d['id']})")
        return
    store, m = _get_store(obj)
    if m.get_work_item(store, item_id) is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    blocking = m.list_deps_blocking(store, item_id)
    blocked_by_me = m.list_deps_blocked_by(store, item_id)
    if as_json:
        click.echo(json.dumps({"blocked_by": blocking, "blocks": blocked_by_me}, indent=2))
        return
    if not blocking and not blocked_by_me:
        click.echo(f"No dependencies on item #{item_id}.")
        return
    if blocking:
        click.echo(f"Item #{item_id} is blocked by:")
        for d in blocking:
            click.echo(f"  #{d['item_id']}  [{d['blocker_status']}]  {d['blocker_title']}  (dep #{d['id']})")
    if blocked_by_me:
        click.echo(f"Item #{item_id} blocks:")
        for d in blocked_by_me:
            click.echo(f"  #{d['blocked_item_id']}  [{d['waiting_status']}]  {d['waiting_title']}  (dep #{d['id']})")


@item_dep.command("remove")
@click.option("--id", "item_id", type=str, required=True, help="Work item ID or repo#id (either side of the dep)")
@click.option("--dep-id", type=int, required=True, help="Dep ID to remove")
@click.pass_obj
def item_dep_remove(obj, item_id: str, dep_id) -> None:
    """Remove a dependency."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        _run_served("item dep remove", _served.item_dep_remove, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, dep_id=dep_id, resolved_context=_resolved_context(config))
        click.echo(f"Dep #{dep_id} removed.")
        return
    store, m = _get_store(obj)
    try:
        m.remove_dep(store, dep_id, item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Dep #{dep_id} removed.")


# ---------------------------------------------------------------------------
# event
# ---------------------------------------------------------------------------

def _shadow_observation_envelope(event: dict, repo_id: str) -> _contracts.RecordEnvelope | None:
    """Translate one persisted authority event into a pilot observation.

    The current event table remains authoritative.  The pilot therefore uses a
    deterministic UUID derived from its stable repository identity and the
    backend event ID, rather than introducing another identifier allocation
    path.  Only record types classified as observations are eligible.
    """
    event_type = event["event_type"]
    try:
        if _contracts.record_class_for_type(event_type) is not _contracts.RecordClass.OBSERVATION:
            return None
    except ValueError:
        return None
    raw_payload = event.get("payload")
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload or {})
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl:{repo_id}:event:{event['id']}"))
    return _contracts.Observation(
        event_id=event_id,
        record_type=event_type,
        schema_version="1",
        actor=event["actor"],
        authored_at=event["created_at"],
        refs={
            "repo_id": repo_id,
            "sprint_id": event["sprint_id"],
            "work_item_id": event.get("work_item_id"),
            "authority_event_id": event["id"],
        },
        payload={"source_type": event["source_type"], "event_payload": payload},
    )


def _shadow_source(envelope: _contracts.RecordEnvelope) -> dict:
    """Return the outbox-shaped record used by parity comparison."""
    return {
        "record_class": envelope.record_class.value,
        "event_id": envelope.event_id,
        "event_type": envelope.record_type,
        "actor": envelope.actor,
        "occurred_at": envelope.authored_at,
        "payload": envelope.to_dict(),
        "runtime_session_id": None,
        "basis_revision": envelope.basis_revision,
        "correlation_id": envelope.correlation_id,
        "causation_id": envelope.causation_id,
    }


def _mirror_shadow_event(event: dict, *, repo_id: str) -> dict:
    """Best-effort, post-commit observation mirror for the opt-in pilot.

    A mirror failure never rolls back or hides the already committed authority
    event.  The structured outcome is instead returned to the operator so a
    pilot defect is observable and retryable without changing normal writes.
    """
    try:
        status = _pilot.shadow_pilot_status(cwd=Path.cwd())
    except _pilot.ShadowPilotConfigError as exc:
        return {"status": "unavailable", "detail": str(exc)}
    if not status.enabled:
        return {"status": "disabled"}
    envelope = _shadow_observation_envelope(event, repo_id)
    if envelope is None:
        return {"status": "unsupported", "event_type": event["event_type"]}
    producer = _outbox.open_outbox(status.paths.outbox_path)
    try:
        result = _dualwrite.mirror_event(
            producer,
            envelope,
        )
    except Exception as exc:  # Authority write already committed; surface, do not undo it.
        return {"status": "error", "detail": str(exc)}
    finally:
        producer.close()
    return {
        "status": result.disposition.value,
        "event_id": result.event_id,
        "event_type": result.record_type,
    }


def _pilot_status_payload() -> dict:
    """Collect non-mutating operator status and optional local cache facts."""
    status = _pilot.shadow_pilot_status(cwd=Path.cwd())
    result = status.to_dict()
    result["outbox_records"] = None
    result["watermark"] = None
    if status.paths.outbox_path.exists():
        producer = _outbox.open_outbox(status.paths.outbox_path)
        try:
            result["outbox_records"] = len(_outbox.list_records(producer))
        finally:
            producer.close()
    if status.paths.projection_path.exists():
        cache = _projection.open_cached_projection(status.paths.projection_path)
        try:
            watermark = _projection.get_watermark(cache)
            result["watermark"] = {
                "ingest_offset": watermark.ingest_offset,
                "advanced_at": watermark.advanced_at,
            }
        finally:
            cache.close()
    return result




_RUNTIME = {}
__runtime_source: dict[str, object] | None = None


def _sync_runtime() -> None:
    source = __runtime_source if __runtime_source is not None else _RUNTIME
    globals().update({key: value for key, value in source.items() if not key.startswith("__")})


def _wrap_runtime_callbacks(command: click.Command) -> None:
    if isinstance(command, click.Group):
        for child in command.commands.values():
            _wrap_runtime_callbacks(child)
        return
    callback = command.callback
    assert callback is not None

    @wraps(callback)
    def runtime_callback(*args, __callback=callback, **kwargs):
        _sync_runtime()
        return __callback(*args, **kwargs)

    command.callback = runtime_callback


def register(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach work-related command groups and keep runtime seams live."""
    global __runtime_source
    __runtime_source = runtime
    _RUNTIME.clear()
    _RUNTIME.update({name: value for name, value in runtime.items() if not name.startswith("__")})
    _sync_runtime()
    for command in (sprint, item):
        root.add_command(command)
        _wrap_runtime_callbacks(command)
