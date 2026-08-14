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

@click.command("handoff")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--output", "output_path", default=None, help="Output file path (default: handoff-N.json or handoff-N.txt)")
@click.option("--events", "events_limit", type=int, default=50, help="Recent events to include (default: 50)")
@click.option(
    "--format", "fmt",
    default="json",
    type=click.Choice(["json", "text"]),
    help="Output format: json (default) or text (human-readable summary)",
)
@click.pass_obj
def handoff_cmd(obj, sprint_id, output_path, events_limit, fmt) -> None:
    """Produce a working-memory handoff bundle for session resumption.

    Use --format text for a human-readable summary suitable for LLM context injection.
    Use --format json (default) for a machine-parseable bundle.
    Pass --output - to write to stdout regardless of format.
    """
    config = _served_config_or_none(obj)
    if config is not None:
        bundle = _run_served("handoff", _served.read_handoff, config.served_profile,
            repo_id=config.repo_id, sprint_id=sprint_id, events_limit=events_limit,
            git_context=_detect_git_context(), resolved_context=_resolved_context(config))
        sid = bundle["sprint"]["id"]
        content = _render_handoff_text(bundle) if fmt == "text" else json.dumps(bundle, indent=2)
        ext = ".txt" if fmt == "text" else ".json"
        dest = output_path or f"handoff-{sid}{ext}"
        if dest == "-":
            click.echo(content)
        else:
            with open(dest, "w") as fh:
                fh.write(content)
                if not content.endswith("\n"):
                    fh.write("\n")
        try:
            _served.handoff_record(config.served_profile, repo_id=config.repo_id,
                sprint_id=sid, bundle=bundle)
        except Exception as error:
            click.echo(f"Handoff bundle written, but served recording is unconfirmed: {error}", err=True)
            raise click.exceptions.Exit(1) from error
        if dest != "-":
            click.echo(f"Handoff bundle for sprint #{sid} written to {dest}")
        return
    store, m = _get_store(obj)
    if sprint_id is not None:
        s = m.get_sprint(store, sprint_id)
    else:
        s = _resolve_implicit_sprint(store, m=m)
    if s is None:
        click.echo("No sprint found. Use --sprint-id to specify one.", err=True)
        sys.exit(1)
    sid = s["id"]
    bundle = _build_handoff_bundle(store, s, events_limit, m=m)

    if fmt == "text":
        content = _render_handoff_text(bundle)
        ext = ".txt"
    else:
        content = json.dumps(bundle, indent=2)
        ext = ".json"

    dest = output_path or f"handoff-{sid}{ext}"
    if dest == "-":
        click.echo(content)
        _record_handoff_generated(store, sid, bundle, m=m)
        return
    with open(dest, "w") as fh:
        fh.write(content)
        if not content.endswith("\n"):
            fh.write("\n")
    _record_handoff_generated(store, sid, bundle, m=m)
    click.echo(f"Handoff bundle for sprint #{sid} written to {dest}")


@click.command("agent-protocol")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def agent_protocol_cmd(as_json) -> None:
    """Print the credential-free reservation protocol for agent consumption."""
    protocol = {
        "sprintctl_agent_protocol_version": "3",
        "reservation_model": {"ownership_proof": None, "stale_after_hours": 4,
            "maintenance_interrupt_after_days": 7,
            "roles": ["inspect", "execute", "review", "coordinate"]},
        "takeup_model": {
            "description": (
                "Sprint-level takeup is an append-only visibility signal, not ownership proof. "
                "Use it to mark which actors are actively looking at or operating on a sprint."
            ),
            "event_types": ["sprint-taken-up", "sprint-released"],
            "commands": {
                "take": (
                    "sprintctl takeup take --sprint-id <id> --actor <name> "
                    "[--instance-id <uuid>] [--context TEXT] [--force] [--json]"
                ),
                "release": (
                    "sprintctl takeup release --sprint-id <id> --actor <name> "
                    "[--instance-id <uuid>] [--reason TEXT] [--json]"
                ),
                "inspect": "sprintctl takeup list [--sprint-id <id>] [--all-history] [--json]",
            },
            "proof_note": "Takeup and reservations are advisory coordination signals; neither authorizes mutation.",
        },
        "lifecycle": {
            "1_startup": {
                "description": "Reserve the item before beginning work.",
                "command": (
                    "sprintctl reservation reserve --item-id <id> --actor <name> "
                    "[--session-id <env-session-id>] [--correlation-ref <actionq-ref>] --json"
                ),
                "store": "Save reservation_id only; no token or recovery secret exists.",
            },
            "2_activity": {
                "description": "Touch activity when useful; there is no lease or heartbeat requirement.",
                "command": "sprintctl reservation touch --id <reservation_id> [--session-id <id>]",
            },
            "3_status_transition": {
                "description": "Transition item status using the current revision CAS basis.",
                "command": (
                    "sprintctl item status --id <item_id> --status active|done|blocked "
                    "--actor <name> --expected-revision <revision>"
                ),
            },
            "4_handoff": {
                "description": "Reassign the advisory reservation to an incoming session when work continues.",
                "command": (
                    "sprintctl reservation reassign --id <reservation_id> --actor <next-agent-name> "
                    "--session-id <next-session-id> --json"
                ),
            },
            "5_release": {
                "description": "Release the reservation when work is complete and no reassignment is needed.",
                "command": "sprintctl reservation release --id <reservation_id> --actor <name>",
            },
        },
        "session_resumption": {
            "description": "If context is lost, list reservations and reassign or reserve as appropriate.",
            "command": "sprintctl reservation list --all --json",
            "recovery": "Reservations contain no recoverable credential.",
        },
        "shutdown_checklist": [
            "For each active reservation: reassign to the next session OR release.",
            "Run 'sprintctl handoff' to write a bundle for the incoming session.",
        ],
        "environment_hints": {
            "SPRINTCTL_RUNTIME_SESSION_ID": "Set to your runtime session ID (auto-detected from CODEX_THREAD_ID).",
            "SPRINTCTL_INSTANCE_ID": "Optional session metadata only; never a credential.",
            "SPRINTCTL_DB": "Override the database path (default: ~/.sprintctl/sprintctl.db).",
        },
    }
    if as_json:
        click.echo(json.dumps(protocol, indent=2))
        return

    click.echo("=== sprintctl Agent Reservation Protocol ===\n")
    click.echo("Ownership proof: none (reservations are advisory)\n")
    click.echo("Sprint takeup:")
    click.echo(f"  {protocol['takeup_model']['description']}")
    click.echo(f"  $ {protocol['takeup_model']['commands']['take']}")
    click.echo(f"  $ {protocol['takeup_model']['commands']['release']}")
    click.echo("")
    click.echo("Lifecycle steps:")
    for step, info in protocol["lifecycle"].items():
        click.echo(f"\n  {step}: {info['description']}")
        click.echo(f"    $ {info['command']}")
        for key in ("store", "frequency", "note", "coordinator_note"):
            if key in info:
                click.echo(f"    [{key}] {info[key]}")
    click.echo("\nSession resumption:")
    click.echo(f"  $ {protocol['session_resumption']['command']}")
    click.echo(f"  {protocol['session_resumption']['recovery']}")
    click.echo("\nShutdown checklist:")
    for item in protocol["shutdown_checklist"]:
        click.echo(f"  - {item}")
    click.echo("\nEnvironment variables:")
    for var, desc in protocol["environment_hints"].items():
        click.echo(f"  {var}: {desc}")


@click.command("next-work")
@click.option("--sprint-id", type=str, default=None, help="Sprint ID or repo#id (defaults to active)")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path),
    is_flag=False,
    flag_value=Path("."),
    help="Union backlog repositories from project.toml (a directory resolves upward).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.option(
    "--explain",
    is_flag=True,
    default=False,
    help="Include exclusion reasons, conflicts, and next_action (detailed in --json mode).",
)
@click.pass_obj
def next_work_cmd(obj, sprint_id, project_path, as_json, explain) -> None:
    """Suggest pending items that are ready to start (no unresolved blocking deps).

    Items are listed in creation order. Items blocked by incomplete predecessors
    are excluded from the suggestion.
    """
    if sprint_id is not None:
        sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    config = _served_config_or_none(obj)
    if config is not None:
        context = _resolved_context(config)
        if explain:
            if project_path is not None:
                _served_operation_unavailable(
                    "project next-work --explain",
                    replacement="The project explain aggregate is not yet served.",
                )
            payload = _run_served(
                "next-work --explain",
                _served.read_next_work_explain,
                config.served_profile,
                repo_id=config.repo_id,
                sprint_id=sprint_id,
                resolved_context=context,
            )
            if as_json:
                click.echo(json.dumps(payload, indent=2))
            else:
                click.echo(_render_next_work_explained_text(payload))
                click.echo(_render_resolved_context(context))
            return
        if project_path is None:
            result = _run_served(
                "next-work",
                _served.read_next_work,
                config.served_profile,
                repo_id=config.repo_id,
                sprint_id=sprint_id,
                resolved_context=context,
            )
            s = result["sprint"]
            ready = result["ready_items"]
            if as_json:
                click.echo(json.dumps(ready, indent=2))
                return
            if not ready:
                click.echo(f"No pending items ready to start in sprint #{s['id']} ({s['name']}).")
                click.echo(_render_resolved_context(context))
                return
            click.echo(f"Ready to start in sprint #{s['id']} ({s['name']}):")
            rows: list[list[str]] = []
            for it in ready:
                assignee = it.get("assignee") or "-"
                rows.append(
                    [f"#{it['id']}", _format_priority(it), it["track_name"], assignee, it["title"]]
                )
            for line in _render_table(["ID", "PRI", "TRACK", "ASSIGNEE", "TITLE"], rows):
                click.echo(f"  {line}")
            click.echo(_render_resolved_context(context))
            return

        result = _run_served(
            "project next-work",
            _served.project_next_work,
            config.served_profile,
            sprint_id=sprint_id,
            resolved_context=context,
        )
        ready_items = result["ready_items"]
        repositories = result["repositories"]
        if as_json:
            click.echo(json.dumps(ready_items, indent=2))
            return
        click.echo(f"Project {result['project_id']}")
        for entry in repositories:
            repo_id = entry["origin_repo"]
            click.echo(f"\n=== {repo_id} ===")
            sprint_row = entry["sprint"]
            tagged_ready = entry["ready_items"]
            if not tagged_ready:
                click.echo(
                    f"No pending items ready to start in sprint #{sprint_row['id']} "
                    f"({sprint_row['name']})."
                )
                continue
            rows = []
            for item_row in tagged_ready:
                rows.append(
                    [
                        f"#{item_row['id']}",
                        _format_priority(item_row),
                        item_row["track_name"],
                        item_row.get("assignee") or "-",
                        item_row["title"],
                    ]
                )
            for line in _render_table(["ID", "PRI", "TRACK", "ASSIGNEE", "TITLE"], rows):
                click.echo(f"  {line}")
        click.echo(_render_resolved_context(context))
        return

    if project_path is None:
        store, m = _get_store(obj)
        if sprint_id is not None:
            s = m.get_sprint(store, sprint_id)
            if s is None:
                click.echo(f"Sprint #{sprint_id} not found.", err=True)
                sys.exit(1)
        else:
            s = _resolve_implicit_sprint(store, m=m)
            if s is None:
                click.echo("No active sprint found. Use --sprint-id to specify one.", err=True)
                sys.exit(1)
        ready = m.get_ready_items(store, s["id"])
        payload = None
        # next-work suggestions require current item/dependency state, which
        # the cached projection never materializes (only observation events
        # are mirrored) -- always backend-sourced; only freshness disclosure
        # is flag-gated here, same rationale as item_list.
        projection_status = _projection_surface_status(_projection_health(), supported=False)
        if explain:
            payload = _collect_next_work_explained_payload(
                conn=store,
                sprint=s,
                ready_items=ready,
                now=datetime.now(timezone.utc),
                m=m,
                repo_id=(
                    obj["backend_config"].repo_id
                    if obj["backend_config"].mode == "remote"
                    else None
                ),
            )
            payload["projection"] = projection_status
        if as_json:
            if explain:
                click.echo(json.dumps(payload, indent=2))
                return
            # NOTE: bare-array JSON shape preserved for compatibility, same as
            # item_list --json; use `projection-reads status --json` instead.
            click.echo(json.dumps(ready, indent=2))
            return
        status_line = _projection_status_line(projection_status)
        if explain:
            if status_line:
                click.echo(status_line)
            click.echo(_render_next_work_explained_text(payload))
            return
        if status_line:
            click.echo(status_line)
        if not ready:
            click.echo(f"No pending items ready to start in sprint #{s['id']} ({s['name']}).")
            return
        click.echo(f"Ready to start in sprint #{s['id']} ({s['name']}):")
        rows: list[list[str]] = []
        for it in ready:
            assignee = it.get("assignee") or "-"
            rows.append(
                [f"#{it['id']}", _format_priority(it), it["track_name"], assignee, it["title"]]
            )
        for line in _render_table(["ID", "PRI", "TRACK", "ASSIGNEE", "TITLE"], rows):
            click.echo(f"  {line}")
        return

    project, scopes = _get_project_stores(obj, project_path)
    resolved, unavailable = _project_sprints(scopes, sprint_id)
    now = datetime.now(timezone.utc)
    ready_items: list[dict] = []
    repositories: list[dict] = []
    for repo_id, store, m, sprint_row in resolved:
        ready = m.get_ready_items(store, sprint_row["id"])
        tagged_ready = [_with_origin(item, repo_id) for item in ready]
        ready_items.extend(tagged_ready)
        entry: dict = {
            "origin_repo": repo_id,
            "sprint": _with_origin(
                {
                    "id": sprint_row["id"],
                    "name": sprint_row["name"],
                    "status": sprint_row["status"],
                },
                repo_id,
            ),
            "ready_items": tagged_ready,
        }
        if explain:
            detailed = _collect_next_work_explained_payload(
                conn=store,
                sprint=sprint_row,
                ready_items=ready,
                now=now,
                m=m,
                repo_id=repo_id,
            )
            entry["next_work"] = _tag_next_work_payload(detailed, repo_id)
        repositories.append(entry)
    repositories.extend({**entry, "status": "unavailable"} for entry in unavailable)

    if as_json and not explain:
        click.echo(json.dumps(ready_items, indent=2))
        return
    if as_json:
        union_payload = {
            "contract_version": "project-1",
            "project": project.summary(),
            "summary": {
                "repositories": len(scopes),
                "repositories_with_sprints": len(resolved),
                "ready": len(ready_items),
            },
            "ready_items": ready_items,
            "repositories": repositories,
        }
        click.echo(json.dumps(union_payload, indent=2))
        return

    click.echo(f"Project {project.display_name} ({project.project_id})")
    for entry in repositories:
        repo_id = entry["origin_repo"]
        click.echo(f"\n=== {repo_id} ===")
        if entry.get("status") == "unavailable":
            click.echo(f"  Unavailable: {entry['message']}")
            continue
        if explain:
            click.echo(_render_next_work_explained_text(entry["next_work"]))
            continue
        tagged_ready = entry["ready_items"]
        sprint_row = entry["sprint"]
        if not tagged_ready:
            click.echo(
                f"No pending items ready to start in sprint #{sprint_row['id']} "
                f"({sprint_row['name']})."
            )
            continue
        rows = []
        for item_row in tagged_ready:
            rows.append(
                [
                    f"#{item_row['id']}",
                    _format_priority(item_row),
                    item_row["track_name"],
                    item_row.get("assignee") or "-",
                    item_row["title"],
                ]
            )
        for line in _render_table(["ID", "PRI", "TRACK", "ASSIGNEE", "TITLE"], rows):
            click.echo(f"  {line}")


@click.command("context-candidates")
@click.option("--sprint-id", type=str, default=None, help="Sprint ID or repo#id (defaults to active)")
@click.option(
    "--item-id",
    "explicit_item_id",
    type=str,
    default=None,
    help="Explicit item ID or repo#id (rank 1). Only this rank is ever claim_eligible.",
)
@click.option(
    "--path",
    "target_paths",
    multiple=True,
    help=(
        "Repo-relative path to match against item file/manifest/glob/doc scope "
        "refs (rank 2). Repeatable."
    ),
)
@click.option(
    "--query",
    default=None,
    help="Free text tokenized for deterministic lexical fallback matching (rank 4).",
)
@click.option(
    "--limit",
    type=int,
    default=_context_candidates.DEFAULT_CANDIDATE_LIMIT,
    show_default=True,
    help=f"Bound the packet to at most this many candidates (capped at {_context_candidates.MAX_CANDIDATE_LIMIT}).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def context_candidates_cmd(obj, sprint_id, explicit_item_id, target_paths, query, limit, as_json) -> None:
    """Emit a bounded, deterministically ranked Tier-1 context-candidate packet.

    Ranks, in preference order: an explicit --item-id target, path/manifest/doc
    scope overlap (--path, repeatable), items carrying other linked
    documentation, deterministic lexical overlap (--query), then remaining
    repo-level candidates -- see docs/ops-upgrade-plan.md Tier 1. Only the
    explicit target (rank 1) is ever marked claim_eligible; inferred candidates
    (ranks 2-5) are advisory context only. This command never claims anything
    itself. Includes the cached projection watermark and its age so a
    consumer knows how stale its view is.
    """
    if limit <= 0:
        click.echo("Error: --limit must be a positive integer.", err=True)
        sys.exit(1)
    if sprint_id is not None:
        sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    if explicit_item_id is not None:
        explicit_item_id = _apply_scoped_id(obj, explicit_item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        payload = _run_served(
            "context-candidates",
            _served.context_candidates,
            config.served_profile,
            repo_id=config.repo_id,
            sprint_id=sprint_id,
            item_id=explicit_item_id,
            target_paths=list(target_paths),
            query=query,
            limit=limit,
        )
    else:
        store, m = _get_store(obj)
        if sprint_id is not None:
            s = m.get_sprint(store, sprint_id)
            if s is None:
                click.echo(f"Sprint #{sprint_id} not found.", err=True)
                sys.exit(1)
        else:
            s = _resolve_implicit_sprint(store, m=m)
            if s is None:
                click.echo("No active sprint found. Use --sprint-id to specify one.", err=True)
                sys.exit(1)
        ready_items = m.get_ready_items(store, s["id"])
        refs_by_item = m.list_refs_for_items(store, [item["id"] for item in ready_items])
        explicit_item = m.get_work_item(store, explicit_item_id) if explicit_item_id is not None else None
        projection_status = _projection_surface_status(_projection_health(), supported=False)
        watermark = None
        if projection_status["watermark_offset"] is not None:
            watermark = {
                "ingest_offset": projection_status["watermark_offset"],
                "age_seconds": projection_status["watermark_age_seconds"],
            }
        try:
            payload = _context_candidates.build_context_candidates(
                ready_items=ready_items,
                refs_by_item=refs_by_item,
                explicit_item_id=explicit_item_id,
                explicit_item=explicit_item,
                target_paths=target_paths,
                query=query,
                limit=limit,
                watermark=watermark,
            )
        except _context_candidates.ContextCandidatesError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        payload["sprint"] = {"id": s["id"], "name": s["name"]}
        payload["projection"] = projection_status

    s = payload["sprint"]
    projection_status = payload["projection"]
    watermark = payload["watermark"]

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Context candidates for sprint #{s['id']} ({s['name']}):")
    status_line = _projection_status_line(projection_status)
    if status_line:
        click.echo(status_line)
    if watermark is not None:
        age = watermark["age_seconds"]
        age_text = f"{age:.0f}s" if age is not None else "unknown"
        click.echo(f"Watermark: offset={watermark['ingest_offset']} age={age_text}")
    explicit_target = payload["explicit_target"]
    if explicit_target is not None and not explicit_target["found"]:
        click.echo(f"Explicit target #{explicit_item_id} not found.")
    if not payload["candidates"]:
        click.echo("No candidates.")
        return
    rows = []
    for candidate in payload["candidates"]:
        rows.append(
            [
                f"#{candidate['item_id']}",
                str(candidate["rank"]),
                candidate["rank_reason"],
                "yes" if candidate["claim_eligible"] else "no",
                candidate["title"] or "",
            ]
        )
    for line in _render_table(["ID", "RANK", "REASON", "CLAIM-OK", "TITLE"], rows):
        click.echo(f"  {line}")
    if payload["truncated"]:
        click.echo(f"(truncated to {payload['bound']} candidates)")


@click.group()
def session() -> None:
    """Session lifecycle helpers."""


@session.command("resume")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def session_resume_cmd(obj, sprint_id, as_json) -> None:
    """Show a combined resume surface (context, next-work explain, and git context)."""
    if _served_config_or_none(obj) is not None:
        _served_operation_unavailable(
            "session resume",
            replacement="The combined session-resume contract is not yet served.",
        )
    store, m = _get_store(obj)
    sprint = _resolve_sprint(store, sprint_id, m=m)
    payload = _collect_session_resume_payload(
        conn=store,
        sprint=sprint,
        now=datetime.now(timezone.utc),
        m=m,
    )
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(_render_session_resume_text(payload))


@click.command("usage")
@click.option(
    "--context",
    "as_context",
    is_flag=True,
    default=False,
    help="Emit current sprint context (active claims, stale/blocked items, ready work, recent decisions)",
)
@click.option("--sprint-id", type=int, default=None, help="Sprint ID for --context (defaults to active)")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path),
    is_flag=False,
    flag_value=Path("."),
    help="Union backlog repositories from project.toml for --context.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output --context as JSON")
@click.pass_obj
def usage_cmd(obj, as_context, sprint_id, project_path, as_json) -> None:
    """Print a compact command reference, or current sprint context with --context."""
    if project_path is not None and not as_context:
        raise click.ClickException("--project requires --context")
    if as_context:
        if _served_config_or_none(obj) is not None:
            if project_path is not None:
                config = obj["backend_config"]
                project_snapshot = _run_served(
                    "usage --context --project",
                    _served.project_context,
                    config.served_profile,
                    sprint_id=sprint_id,
                    resolved_context=_resolved_context(config),
                )
                if as_json:
                    click.echo(json.dumps(project_snapshot, indent=2))
                    return
                project = project_snapshot["project"]
                click.echo(
                    f"Project {project.get('display_name', project['project_id'])} "
                    f"({project['project_id']})"
                )
                for entry in project_snapshot["repositories"]:
                    click.echo(f"\n=== {entry['origin_repo']} ===")
                    if entry["status"] == "unavailable":
                        click.echo(f"  Unavailable: {entry['message']}")
                    else:
                        click.echo(_render_context_text(entry["context"]))
                return
            config = obj["backend_config"]
            context = _resolved_context(config)
            snapshot = _run_served(
                "usage --context", _served.read_context, config.served_profile,
                repo_id=config.repo_id, sprint_id=sprint_id, resolved_context=context,
            )
            if as_json:
                click.echo(json.dumps(snapshot, indent=2))
            else:
                click.echo(_render_context_text(snapshot))
            return
        if project_path is not None:
            project, scopes = _get_project_stores(obj, project_path)
            resolved, unavailable = _project_sprints(scopes, sprint_id)
            now = datetime.now(timezone.utc)
            repositories: list[dict] = []
            snapshots: list[dict] = []
            for repo_id, store, m, sprint_row in resolved:
                snapshot = _tag_context_payload(
                    _collect_context_contract(store, sprint_row, now, m=m), repo_id
                )
                snapshots.append(snapshot)
                repositories.append(
                    {
                        "origin_repo": repo_id,
                        "status": "ok",
                        "context": snapshot,
                    }
                )
            repositories.extend({**entry, "status": "unavailable"} for entry in unavailable)
            summary_keys = (
                "total",
                "done",
                "active",
                "pending",
                "blocked",
                "stale",
                "ready",
                "waiting_on_dependencies",
                "active_reservations",
                "active_unreserved",
            )
            union_payload = {
                "contract_version": "project-1",
                "project": project.summary(),
                "summary": {
                    key: sum(snapshot["summary"][key] for snapshot in snapshots)
                    for key in summary_keys
                },
                "sprints": [snapshot["sprint"] for snapshot in snapshots],
                "active_reservations": [
                    value for snapshot in snapshots for value in snapshot["active_reservations"]
                ],
                "active_unreserved_items": [
                    value
                    for snapshot in snapshots
                    for value in snapshot["active_unreserved_items"]
                ],
                "conflicts": [
                    value for snapshot in snapshots for value in snapshot["conflicts"]
                ],
                "ready_items": [
                    value for snapshot in snapshots for value in snapshot["ready_items"]
                ],
                "blocked_items": [
                    value for snapshot in snapshots for value in snapshot["blocked_items"]
                ],
                "stale_items": [
                    value for snapshot in snapshots for value in snapshot["stale_items"]
                ],
                "recent_decisions": [
                    value for snapshot in snapshots for value in snapshot["recent_decisions"]
                ],
                "next_actions": [snapshot["next_action"] for snapshot in snapshots],
                "repositories": repositories,
            }
            if as_json:
                click.echo(json.dumps(union_payload, indent=2))
                return
            click.echo(f"Project {project.display_name} ({project.project_id})")
            for entry in repositories:
                click.echo(f"\n=== {entry['origin_repo']} ===")
                if entry["status"] == "unavailable":
                    click.echo(f"  Unavailable: {entry['message']}")
                else:
                    click.echo(_render_context_text(entry["context"]))
            return
        store, m = _get_store(obj)
        s = _resolve_sprint(store, sprint_id, m=m)
        now = datetime.now(timezone.utc)
        snapshot = _collect_context_contract(store, s, now, m=m)
        # usage --context aggregates sprint/claim/item state that the cached
        # projection never materializes (only observation events are
        # mirrored) -- always backend-sourced; only freshness disclosure is
        # flag-gated here, same rationale as item_list/next-work. The
        # "projection" key is added only when the flag is enabled so the
        # default --json shape stays byte-for-byte unchanged.
        projection_status = _projection_surface_status(_projection_health(), supported=False)
        if projection_status["enabled"]:
            snapshot["projection"] = projection_status
        if as_json:
            click.echo(json.dumps(snapshot, indent=2))
            return
        status_line = _projection_status_line(projection_status)
        if status_line:
            click.echo(status_line)
        click.echo(_render_context_text(snapshot))
        return

    lines = [
        f"sprintctl v{__version__} — agent-centric sprint coordination CLI",
        "  doctor         [--json]  # read-only provenance/backend/schema diagnostics",
        "",
        "SPRINT",
        "  sprint create  --name NAME [--goal GOAL] [--start YYYY-MM-DD] [--end YYYY-MM-DD]",
        "                 [--status planned|active|closed] [--kind active_sprint|backlog|archive] [--json]",
        "  sprint show    [--id ID] [--detail] [--watch] [--interval SECONDS] [--json]",
        "  sprint status  --id ID --status planned|active|closed [--actor NAME] [--json]",
        "  sprint list    [--include-backlog] [--include-archive] [--json]",
        "                 [--project PROJECT_TOML]",
        "  sprint kind    --id ID --kind active_sprint|backlog|archive",
        "",
        "ITEM",
        "  item add       --sprint-id ID --track NAME --title TITLE [--description TEXT]",
        "                 [--assignee NAME] [--priority N] [--json]",
        "  item edit      --id ID --description TEXT [--json]",
        "  item priority  --id ID (--set N | --clear) [--json]",
        "  item show      --id ID [--json]",
        "  item list      [--sprint-id ID] [--track NAME] [--status STATUS] [--fzf] [--json]",
        "                 [--project PROJECT_TOML]",
        "  item note      --id ID --type TYPE --summary TEXT [--detail TEXT] [--tags T1,T2]",
        "                 [--actor NAME]",
        "  item status    --id ID --status pending|active|done|blocked [--actor NAME] [--json]",
        "  item ref add   --id ID --type pr|issue|doc|other --url URL [--label TEXT]",
        "  item ref list  --id ID [--json]",
        "  item ref remove --id ID --ref-id N",
        "  item dep add   --id BLOCKER_ID --blocks-item-id BLOCKED_ID",
        "  item dep list  --id ID [--json]",
        "  item dep remove --id ID --dep-id N",
        "",
        "EVENT",
        "  event add      --sprint-id ID --type|--event-type TYPE --actor NAME [--item-id ID]",
        "                 [--source actor|daemon|system] [--payload JSON] [--json]",
        "  event log      Alias for event add",
        "  event list     --sprint-id ID [--item-id ID] [--type TYPE] [--limit N] [--json]",
        "",
        "TAKEUP",
        "  takeup take    --sprint-id ID --actor NAME [--instance-id ID] [--context TEXT]",
        "                 [--force] [--json]",
        "  takeup release --sprint-id ID --actor NAME [--instance-id ID] [--reason TEXT] [--json]",
        "  takeup list    [--sprint-id ID] [--all-history] [--json]",
        "  takeup show    --sprint-id ID [--json]",
        "  takeup sweep   [--sprint-id ID] [--stale-after SECONDS] [--json]",
        "",
        "MAINTAIN",
        "  maintain check    [--sprint-id ID] [--threshold Nh] [--json]",
        "  maintain sweep    [--sprint-id ID] [--threshold Nh] [--auto-close]",
        "  maintain carryover --from-sprint ID --to-sprint ID",
        "  db vacuum         [--json]",
        "  db integrity      [--json]",
        "",
        "TOP-LEVEL",
        "  export         --sprint-id ID [--output PATH]",
        "  import         --file PATH",
        "  handoff        [--sprint-id ID] [--output PATH] [--events N] [--format json|text]",
        "  render         [--sprint-id ID] [--output PATH]",
        "  next-work      [--sprint-id ID] [--json] [--explain]",
        "                 [--project PROJECT_TOML]",
        "  takeup         take|release|list|show|sweep",
        "  session resume [--sprint-id ID] [--json]",
        "  git-context    [--json]",
        "  agent-protocol [--json]",
        "  usage          [--context] [--sprint-id ID] [--json]",
        "                 [--project PROJECT_TOML]",
        "",
        "PROJECTION-READS (guarded projection-backed reads, default off)",
        "  projection-reads status  [--json]",
        "  projection-reads enable  [--json]",
        "  projection-reads disable [--json]   # rollback: returns all reads to backend",
        "",
        "ENV",
        "  SPRINTCTL_DB                    Database path (default: ~/.sprintctl/sprintctl.db)",
        "  SPRINTCTL_STALE_THRESHOLD       Active item staleness in hours (default: 4)",
        "  SPRINTCTL_PENDING_STALE_THRESHOLD  Pending item staleness threshold (default: off)",
        "  SPRINTCTL_RUNTIME_SESSION_ID    Runtime session ID (auto-detected from CODEX_THREAD_ID)",
        "  SPRINTCTL_INSTANCE_ID           Stable per-process instance UUID",
        "  SPRINTCTL_PROJECTION_READS      Override projection-reads enable/disable for one invocation",
        "  SPRINTCTL_PROJECTION_STALE_SECONDS  Projection staleness threshold in seconds (default: 300)",
    ]
    click.echo("\n".join(lines))


# ---------------------------------------------------------------------------
# git-context
# ---------------------------------------------------------------------------


@click.command("git-context")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def git_context_cmd(as_json) -> None:
    """Show the current git branch, commit SHA, and worktree path."""
    context = _detect_git_context()
    if context is None:
        click.echo("Error: not a git repository.", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(context))
        return
    click.echo(f"Branch:   {context['branch']}")
    click.echo(f"SHA:      {context['sha']}")
    click.echo(f"Worktree: {context['worktree']}")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@click.command("render")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--output", "output_path", default=None, help="Write rendered doc to a file instead of stdout")
@click.pass_obj
def render_cmd(obj, sprint_id, output_path) -> None:
    """Render a plain-text sprint document."""
    store, m = _get_store(obj)
    if sprint_id is not None:
        s = m.get_sprint(store, sprint_id)
    else:
        s = _resolve_implicit_sprint(store, m=m)
    if s is None:
        click.echo("No sprint found. Use --sprint-id to specify one.", err=True)
        sys.exit(1)
    tracks = m.list_tracks(store, s["id"])
    all_items = m.list_work_items(store, sprint_id=s["id"])
    items_by_track: dict[int, list[dict]] = {}
    for it in all_items:
        items_by_track.setdefault(it["track_id"], []).append(it)
    refs_by_item: dict[int, list[dict]] = {}
    for it in all_items:
        item_refs = m.list_refs(store, it["id"])
        if item_refs:
            refs_by_item[it["id"]] = item_refs
    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    active_takeups = m.list_active_takeups(store, s["id"])
    doc = render_sprint_doc(
        s,
        tracks,
        items_by_track,
        rendered_at,
        refs_by_item=refs_by_item,
        active_takeups=active_takeups,
    )
    if output_path:
        with open(output_path, "w") as fh:
            fh.write(doc + "\n")
        click.echo(f"Sprint #{s['id']} rendered to {output_path}")
    else:
        click.echo(doc)


# ---------------------------------------------------------------------------
# migrate-to-remote — explicit SQLite-to-PostgreSQL state transfer
# ---------------------------------------------------------------------------

@click.command("migrate-to-remote")
@click.option("--url", "pg_url", default=None, help="Postgres URL (default: $SPRINTCTL_URL)")
@click.option("--db", "db_path_override", default=None, help="Source sqlite path (default: auto-detect)")
@click.option("--repo-root", "repo_root_override", default=None, help="Repo root override")
@click.option("--repo-id", "repo_id_assert", default=None, help="Assert this repo_id (must match path-derived value)")
@click.option("--dry-run", is_flag=True, default=False, help="Validate without importing or freezing")
@click.option("--replace", is_flag=True, default=False, help="Delete existing pg rows for repo_id before import")
@click.option("--remap-ids", "remap_ids", is_flag=True, default=False, help="Let postgres assign new IDs (needed when shared DB already has conflicting global IDs)")
@click.option("--keep-ndjson", "keep_ndjson_path", default=None, help="Write NDJSON to this file for inspection")
@click.option("--yes", "skip_confirm", is_flag=True, default=False, help="Skip confirmation prompt before freezing sqlite")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable summary")
def migrate_to_remote_cmd(
    pg_url,
    db_path_override,
    repo_root_override,
    repo_id_assert,
    dry_run,
    replace,
    remap_ids,
    keep_ndjson_path,
    skip_confirm,
    as_json,
) -> None:
    """Migrate a local sqlite database to remote postgres."""
    import io  # noqa: PLC0415
    from .. import pg as _pg  # noqa: PLC0415

    # 1. Preflight: resolve repo identity
    cwd = Path(repo_root_override) if repo_root_override else Path.cwd()
    try:
        repo_root, repo_id, marker = _backend.resolve_repo_identity(cwd)
    except _backend.BackendConfigError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    if repo_id is None:
        click.echo(
            "Error: cannot resolve repo_id. Run from inside a repository with .sprintctl/backend.json or .git.",
            err=True,
        )
        sys.exit(1)

    if repo_id_assert is not None and repo_id_assert != repo_id:
        click.echo(
            f"Error: --repo-id='{repo_id_assert}' does not match path-derived repo_id='{repo_id}'.",
            err=True,
        )
        sys.exit(1)

    # Resolve pg URL
    url = pg_url or os.environ.get("SPRINTCTL_URL")
    if not url:
        click.echo("Error: Postgres URL required. Pass --url or set SPRINTCTL_URL.", err=True)
        sys.exit(1)

    # Resolve sqlite source path
    if db_path_override:
        sqlite_path = Path(db_path_override)
    elif os.environ.get("SPRINTCTL_DB"):
        sqlite_path = Path(os.environ["SPRINTCTL_DB"])
    elif repo_root:
        sqlite_path = repo_root / ".sprintctl" / "sprintctl.db"
    else:
        sqlite_path = _db.get_db_path()

    if not sqlite_path.exists() or not sqlite_path.is_file():
        click.echo(f"Error: sqlite source not found: {sqlite_path}", err=True)
        sys.exit(1)

    # Open and upgrade sqlite source
    sqlite_conn = _db.get_connection(sqlite_path)
    try:
        _db.init_db(sqlite_conn)
    except Exception as e:
        click.echo(f"Error: local migration failed before export: {e}", err=True)
        sys.exit(1)

    # Connect to pg and init schema
    try:
        pg_store = _pg.get_connection(url)
        _pg.init_db(pg_store)
    except Exception as e:
        click.echo(f"Error: could not connect to postgres from SPRINTCTL_URL: {e}", err=True)
        sys.exit(1)

    # Check for existing pg data
    try:
        with pg_store.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM sprint WHERE repo_id = %s", (repo_id,))
            row = cur.fetchone()
            existing_count = row["cnt"] if row else 0
    except Exception:
        existing_count = 0

    if existing_count > 0 and not replace:
        click.echo(
            f"Error: remote repo_id '{repo_id}' already has data ({existing_count} sprints). "
            "Use --replace to re-import intentionally.",
            err=True,
        )
        sys.exit(1)

    # 2. Export NDJSON
    ndjson_buf = io.StringIO()
    try:
        counts = _pg.export_ndjson(sqlite_conn, repo_id, ndjson_buf)
    except Exception as e:
        click.echo(f"Error: NDJSON export failed: {e}", err=True)
        sys.exit(1)

    ndjson_content = ndjson_buf.getvalue()
    records = [json.loads(line) for line in ndjson_content.splitlines() if line.strip()]

    if keep_ndjson_path:
        try:
            Path(keep_ndjson_path).write_text(ndjson_content)
        except OSError as e:
            click.echo(f"Warning: could not write NDJSON to {keep_ndjson_path}: {e}", err=True)

    if dry_run:
        if as_json:
            click.echo(json.dumps({
                "dry_run": True,
                "repo_id": repo_id,
                "sqlite_path": str(sqlite_path),
                "counts": counts,
            }, indent=2))
        else:
            click.echo(f"Dry run for repo '{repo_id}' from {sqlite_path}")
            for table, count in counts.items():
                click.echo(f"  {table}: {count} rows")
        sqlite_conn.close()
        pg_store.conn.close()
        return

    # Confirm before freeze
    if not skip_confirm:
        total_rows = sum(counts.values())
        click.echo(
            f"About to migrate repo '{repo_id}' ({total_rows} rows) to postgres "
            f"and freeze {sqlite_path}."
        )
        if not click.confirm("Proceed?"):
            click.echo("Aborted.")
            sqlite_conn.close()
            pg_store.conn.close()
            sys.exit(0)

    # 3. Import to pg
    try:
        _pg.import_ndjson(
            pg_store,
            records,
            replace=replace,
            remap_ids=remap_ids,
            trusted_state_transfer=True,
        )
    except Exception as e:
        click.echo(f"Error: import failed: {e}", err=True)
        click.echo("Sqlite has NOT been modified. Fix the error and retry (use --replace if pg now has partial data).")
        sqlite_conn.close()
        pg_store.conn.close()
        sys.exit(1)

    sqlite_conn.close()

    # 4. Freeze local
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    frozen_path = sqlite_path.parent / f".sprintctl.db.frozen-{ts}"
    marker_path = sqlite_path.parent / "backend.json"
    sentinel_path = sqlite_path  # will become a directory

    try:
        sqlite_path.rename(frozen_path)
        marker_path.write_text(json.dumps({
            "backend": "remote",
            "repo_id": repo_id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, indent=2) + "\n")
        sentinel_path.mkdir(exist_ok=False)
    except Exception as e:
        click.echo(f"Error: freeze failed after successful import: {e}", err=True)
        click.echo(
            "The pg import succeeded. To complete the freeze manually:\n"
            f"  mv '{sqlite_path}' '{frozen_path}'\n"
            f"  echo '{{\"backend\":\"remote\",\"repo_id\":\"{repo_id}\"}}' > '{marker_path}'\n"
            f"  mkdir '{sentinel_path}'"
        )
        pg_store.conn.close()
        sys.exit(1)

    pg_store.conn.close()

    if as_json:
        click.echo(json.dumps({
            "repo_id": repo_id,
            "counts": counts,
            "frozen_sqlite": str(frozen_path),
            "backend_marker": str(marker_path),
        }, indent=2))
    else:
        click.echo(f"Migrated repo '{repo_id}' to remote postgres.")
        parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
        click.echo(f"Imported: {', '.join(parts)}.")
        click.echo(f"Frozen sqlite: {frozen_path}")


# ---------------------------------------------------------------------------
# remote-backfill — PostgreSQL-to-PostgreSQL repo state transfer
#
# Generalizes the one-off manual procedure used for sprintctl #1164's own
# served-mode promotion (vuoro #1223 "production promotion record": a
# hand-run psql \copy / COPY FROM STDIN dance, table by table, done once for
# one repo). Every workstation repo still on direct-remote mode has its
# history in a database completely separate from vuoro-shared's -- this
# backfill is the prerequisite for any of them flipping to served mode
# without their sprint/item history going silently invisible.
# ---------------------------------------------------------------------------

@click.command("remote-backfill")
@click.option("--source-url", required=True, help="Source PostgreSQL URL (a separate, already-deployed sprintctl authority)")
@click.option("--url", "dest_url", default=None, help="Destination PostgreSQL URL (default: $SPRINTCTL_URL)")
@click.option("--repo-id", "repo_id", required=True, help="Repository to copy (must be explicit -- this command is not run from inside a repo checkout)")
@click.option("--dry-run", is_flag=True, default=False, help="Report source/destination row counts without writing")
@click.option("--replace", is_flag=True, default=False, help="Delete existing destination rows for repo_id before import")
@click.option("--yes", "skip_confirm", is_flag=True, default=False, help="Skip the confirmation prompt before writing")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable summary")
def remote_backfill_cmd(
    source_url,
    dest_url,
    repo_id,
    dry_run,
    replace,
    skip_confirm,
    as_json,
) -> None:
    """Copy one repository's history from another PostgreSQL authority.

    Always remaps IDs on import (never preserves the source's literal
    integer IDs): the destination is a shared, already-live database whose
    own identity sequences have advanced independently of the source's, so
    literal-ID preservation risks a silent collision with another repo's
    (or this repo's own later served-mode) rows. This is the same
    ``import_ndjson(remap_ids=True)`` path ``migrate-to-remote`` uses for
    exactly this reason when importing into a shared database.
    """
    from .. import pg as _pg  # noqa: PLC0415

    dest_url = dest_url or os.environ.get("SPRINTCTL_URL")
    if not dest_url:
        click.echo("Error: destination Postgres URL required. Pass --url or set SPRINTCTL_URL.", err=True)
        sys.exit(1)

    try:
        source_store = _pg.get_connection(source_url)
    except Exception as e:
        click.echo(f"Error: could not connect to --source-url: {e}", err=True)
        sys.exit(1)
    try:
        dest_store = _pg.get_connection(dest_url)
    except Exception as e:
        click.echo(f"Error: could not connect to destination Postgres: {e}", err=True)
        source_store.conn.close()
        sys.exit(1)

    source_counts = _pg.backfill_repo_row_counts(source_store.conn, repo_id)
    if sum(source_counts.values()) == 0:
        click.echo(f"Error: no rows found for repo_id '{repo_id}' at --source-url.", err=True)
        source_store.conn.close()
        dest_store.conn.close()
        sys.exit(1)

    existing_dest_counts = _pg.backfill_repo_row_counts(dest_store.conn, repo_id)
    existing_total = sum(existing_dest_counts.values())

    if dry_run:
        # Report-only: never enforce the existing-destination-data guard
        # here, since dry-run makes no write for it to protect.
        if as_json:
            click.echo(json.dumps({
                "dry_run": True,
                "repo_id": repo_id,
                "source_counts": source_counts,
                "existing_destination_counts": existing_dest_counts,
            }, indent=2))
        else:
            click.echo(f"Dry run for repo '{repo_id}'")
            for table, count in source_counts.items():
                click.echo(f"  {table}: {count} rows")
            if existing_total > 0:
                click.echo(
                    f"Note: destination already has {existing_total} row(s) "
                    "for this repo_id; a real run would require --replace."
                )
        source_store.conn.close()
        dest_store.conn.close()
        return

    if existing_total > 0 and not replace:
        click.echo(
            f"Error: destination already has data for repo_id '{repo_id}' "
            f"({existing_total} rows). Use --replace to re-import intentionally.",
            err=True,
        )
        source_store.conn.close()
        dest_store.conn.close()
        sys.exit(1)

    if not skip_confirm:
        total_rows = sum(source_counts.values())
        click.echo(f"About to backfill repo '{repo_id}' ({total_rows} rows) into the destination Postgres.")
        if not click.confirm("Proceed?"):
            click.echo("Aborted.")
            source_store.conn.close()
            dest_store.conn.close()
            sys.exit(0)

    dest_store.repo_id = repo_id
    records = _pg.export_from_postgres(source_store.conn, repo_id)
    try:
        imported_counts = _pg.import_ndjson(
            dest_store,
            records,
            replace=replace,
            remap_ids=True,
            trusted_state_transfer=False,
        )
    except Exception as e:
        click.echo(f"Error: import failed: {e}", err=True)
        click.echo("Source has NOT been modified. Fix the error and retry (use --replace if the destination now has partial data).", err=True)
        source_store.conn.close()
        dest_store.conn.close()
        sys.exit(1)

    dest_counts = _pg.backfill_repo_row_counts(dest_store.conn, repo_id)
    source_store.conn.close()
    dest_store.conn.close()

    parity = {
        table: {"source": source_counts.get(table, 0), "destination": dest_counts.get(table, 0)}
        for table in source_counts
    }
    all_match = all(v["source"] == v["destination"] for v in parity.values())

    if as_json:
        click.echo(json.dumps({
            "repo_id": repo_id,
            "imported_counts": imported_counts,
            "parity": parity,
            "parity_ok": all_match,
        }, indent=2))
    else:
        click.echo(f"Backfilled repo '{repo_id}'.")
        for table, v in parity.items():
            mark = "ok" if v["source"] == v["destination"] else "MISMATCH"
            click.echo(f"  {table}: source={v['source']} destination={v['destination']} [{mark}]")
        if not all_match:
            click.echo("Error: row count parity check failed after import.", err=True)
            sys.exit(1)

_RUNTIME: dict[str, object] = {}
__runtime_source: dict[str, object] | None = None


def _sync_runtime() -> None:
    source = __runtime_source if __runtime_source is not None else _RUNTIME
    globals().update({key: value for key, value in source.items() if not key.startswith("__")})


def _wrap_runtime_callbacks(command: click.Command) -> None:
    callback = getattr(command, "callback", None)
    if callback is not None and not getattr(callback, "__runtime_wrapped__", False):
        original = callback

        def wrapped(*args, **kwargs):
            _sync_runtime()
            return original(*args, **kwargs)

        wrapped.__name__ = getattr(original, "__name__", "callback")
        wrapped.__doc__ = getattr(original, "__doc__", None)
        wrapped.__runtime_wrapped__ = True
        command.callback = wrapped
    if isinstance(command, click.Group):
        for child in command.commands.values():
            _wrap_runtime_callbacks(child)


def register(root: click.Group, *, runtime: dict[str, object]) -> None:
    global __runtime_source
    __runtime_source = runtime
    _RUNTIME.clear()
    _RUNTIME.update({name: value for name, value in runtime.items() if not name.startswith("__")})
    _sync_runtime()
    for command in (handoff_cmd, agent_protocol_cmd, next_work_cmd, context_candidates_cmd, session, usage_cmd, git_context_cmd, render_cmd, migrate_to_remote_cmd, remote_backfill_cmd):
        root.add_command(command)
        _wrap_runtime_callbacks(command)
