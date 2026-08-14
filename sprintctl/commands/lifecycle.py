"""Takeup, maintenance, and claim command groups.

The callbacks retain the existing CLI runtime seams through an injected
runtime mapping, without importing cli.py.
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

# takeup
# ---------------------------------------------------------------------------

@click.group()
def takeup() -> None:
    """Manage sprint-level takeup events."""


def _takeup_payload(
    *,
    actor_kind: str,
    hostname: str | None,
    pid: int | None,
    instance_id: str | None,
    runtime_session_id: str | None,
    summary: str,
    detail: str | None,
    context: str | None = None,
    forced: bool | None = None,
    reason: str | None = None,
    matched_takeup_event_id: int | None = None,
) -> dict:
    payload = {
        "summary": summary,
        "detail": detail,
        "actor_kind": actor_kind,
        "hostname": hostname,
        "pid": pid,
        "instance_id": instance_id,
        "runtime_session_id": runtime_session_id,
    }
    if context is not None:
        payload["context"] = context
    if forced is not None:
        payload["forced"] = forced
    if reason is not None:
        payload["reason"] = reason
    if matched_takeup_event_id is not None:
        payload["matched_takeup_event_id"] = matched_takeup_event_id
    return payload


def _matching_active_takeups(
    conn,
    *,
    sprint_id: int,
    actor: str,
    instance_id: str | None,
    m=None,
) -> list[dict]:
    m = m or _db
    matches = [
        row for row in m.list_active_takeups(conn, sprint_id)
        if row["actor"] == actor
    ]
    if instance_id is not None:
        matches = [row for row in matches if row.get("instance_id") == instance_id]
    return sorted(matches, key=lambda row: (row["taken_up_at"], row["taken_up_event_id"]))


def _short_id(value: str | None) -> str:
    if not value:
        return "-"
    return value if len(value) <= 12 else f"{value[:8]}..."


def _render_takeup_rows(rows: list[dict], *, released: bool = False) -> None:
    if not rows:
        click.echo("  (none)")
        return
    headers = ["SPRINT", "ACTOR", "INSTANCE", "HOST", "SINCE", "CONTEXT"]
    table_rows: list[list[str]] = []
    for row in rows:
        context = row.get("context") or "-"
        values = [
            f"#{row['sprint_id']}",
            row["actor"],
            _short_id(row.get("instance_id")),
            row.get("hostname") or "-",
            row.get("taken_up_at") or "-",
            context,
        ]
        if released:
            if "RELEASED" not in headers:
                headers.append("RELEASED")
                headers.append("REASON")
            values.append(row.get("released_at") or "-")
            values.append(row.get("reason") or "-")
        table_rows.append(values)
    for line in _render_table(headers, table_rows):
        click.echo(f"  {line}")


def _parse_utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_active_actionq_session_ids(actionctl_bin: str) -> set[str]:
    result = subprocess.run(
        [actionctl_bin, "sessions", "--active"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise click.ClickException(f"actionctl sessions failed: {detail}")
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise click.ClickException("actionctl sessions returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise click.ClickException("actionctl sessions output must be a JSON array")

    active_statuses = {"running", "starting", "claimed", "active"}
    session_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "running")
        if status not in active_statuses:
            continue
        for key in ("runtime_session_id", "session_id"):
            value = row.get(key)
            if value:
                session_ids.add(str(value))
    return session_ids


def _release_takeup_from_sweep(store, m, row: dict, *, reason: str, detail: str) -> int:
    return m.create_event(
        store,
        int(row["sprint_id"]),
        "sweep",
        "sprint-released",
        payload=_takeup_payload(
            actor_kind="agent",
            hostname=_detect_hostname(None),
            pid=_detect_pid(None),
            instance_id=row.get("instance_id"),
            runtime_session_id=row.get("runtime_session_id"),
            summary="takeup sweep release",
            detail=detail,
            reason=reason,
            matched_takeup_event_id=int(row["taken_up_event_id"]),
        ),
    )


@takeup.command("sweep")
@click.option("--sprint-id", type=int, default=None, help="Limit sweep to one sprint")
@click.option("--actionctl-bin", default="actionctl", show_default=True, help="actionctl executable")
@click.option(
    "--stale-after",
    type=int,
    default=None,
    help="Also release takeups without runtime_session_id older than N seconds",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def takeup_sweep_cmd(obj, sprint_id, actionctl_bin, stale_after, as_json) -> None:
    """Release takeups whose actionq runtime sessions are no longer active."""
    store, m = _get_store(obj)
    if sprint_id is not None and m.get_sprint(store, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)

    active_session_ids = _load_active_actionq_session_ids(actionctl_bin)
    now = datetime.now(timezone.utc)
    released: list[dict] = []
    skipped: list[dict] = []

    for row in m.list_active_takeups(store, sprint_id):
        runtime_session_id = row.get("runtime_session_id")
        reason: str | None = None
        detail: str | None = None

        if runtime_session_id:
            if runtime_session_id in active_session_ids:
                skipped.append({
                    "taken_up_event_id": row["taken_up_event_id"],
                    "sprint_id": row["sprint_id"],
                    "actor": row["actor"],
                    "reason": "session-active",
                })
                continue
            reason = "session-not-active"
            detail = f"runtime_session_id {runtime_session_id} is not active in actionctl sessions"
        elif stale_after is not None:
            age_seconds = (now - _parse_utc_timestamp(row["taken_up_at"])).total_seconds()
            if age_seconds < stale_after:
                skipped.append({
                    "taken_up_event_id": row["taken_up_event_id"],
                    "sprint_id": row["sprint_id"],
                    "actor": row["actor"],
                    "reason": "takeup-not-stale",
                    "age_seconds": int(age_seconds),
                })
                continue
            reason = "no-session-stale"
            detail = f"takeup has no runtime_session_id and is older than {stale_after} seconds"
        else:
            skipped.append({
                "taken_up_event_id": row["taken_up_event_id"],
                "sprint_id": row["sprint_id"],
                "actor": row["actor"],
                "reason": "no-runtime-session-id",
            })
            continue

        event_id = _release_takeup_from_sweep(
            store,
            m,
            row,
            reason=reason,
            detail=detail,
        )
        released.append({
            "released_event_id": event_id,
            "matched_takeup_event_id": row["taken_up_event_id"],
            "sprint_id": row["sprint_id"],
            "actor": row["actor"],
            "runtime_session_id": runtime_session_id,
            "reason": reason,
        })

    payload = {
        "operation": "takeup_sweep",
        "sprint_id": sprint_id,
        "active_session_count": len(active_session_ids),
        "released_takeups": released,
        "skipped_takeups": skipped,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Released {len(released)} takeup(s); skipped {len(skipped)}.")
    for row in released:
        click.echo(
            f"  sprint #{row['sprint_id']} takeup #{row['matched_takeup_event_id']} "
            f"released as #{row['released_event_id']} ({row['reason']})"
        )


@takeup.command("take")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--actor", required=True, help="Actor name")
@click.option(
    "--actor-kind",
    default="agent",
    type=click.Choice(["agent", "human"]),
    help="Actor kind",
)
@click.option("--context", default=None, help="Free-form takeup context")
@click.option("--instance-id", default=None, help="Stable actor instance ID")
@click.option("--runtime-session-id", default=None, help="Runtime session ID")
@click.option("--hostname", default=None, help="Hostname")
@click.option("--pid", type=int, default=None, help="Process ID")
@click.option("--summary", default="sprint takeup", show_default=True, help="Event summary")
@click.option("--detail", default=None, help="Event detail")
@click.option("--force", is_flag=True, default=False, help="Record takeup even if this actor instance is active")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def takeup_take_cmd(
    obj,
    sprint_id,
    actor,
    actor_kind,
    context,
    instance_id,
    runtime_session_id,
    hostname,
    pid,
    summary,
    detail,
    force,
    as_json,
) -> None:
    """Record that an actor has taken up a sprint."""
    store, m = _get_store(obj)
    sprint = m.get_sprint(store, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)

    instance_id = _detect_instance_id(instance_id)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)

    active_matches = _matching_active_takeups(
        store,
        sprint_id=sprint_id,
        actor=actor,
        instance_id=instance_id,
        m=m,
    )
    if active_matches and not force:
        click.echo(
            f"Sprint #{sprint_id} already taken up by actor='{actor}' "
            f"instance='{instance_id}'. Use --force for crash recovery.",
            err=True,
        )
        sys.exit(2)

    if sprint.get("kind") != "active_sprint":
        click.echo(
            f"Warning: sprint #{sprint_id} kind is '{sprint.get('kind')}', not 'active_sprint'.",
            err=True,
        )

    event_id = m.create_event(
        store,
        sprint_id,
        actor,
        "sprint-taken-up",
        payload=_takeup_payload(
            actor_kind=actor_kind,
            hostname=hostname,
            pid=pid,
            instance_id=instance_id,
            runtime_session_id=runtime_session_id,
            summary=summary,
            detail=detail,
            context=context,
            forced=force,
        ),
    )
    _emit_audit_event(
        "sprint.taken_up",
        summary=f"Sprint {sprint_id} taken up by {actor}",
        refs=[f"sprint:{sprint_id}"],
        metadata={"sprint_id": sprint_id, "event_type": "sprint-taken-up", "actor": actor},
    )
    if as_json:
        click.echo(json.dumps({
            "operation": "takeup_take",
            "event_id": event_id,
            "sprint_id": sprint_id,
            "actor": actor,
            "actor_kind": actor_kind,
            "instance_id": instance_id,
            "hostname": hostname,
            "pid": pid,
            "forced": force,
            "context": context,
        }, indent=2))
        return
    click.echo(
        f"Sprint #{sprint_id} taken up by {actor} "
        f"(instance: {instance_id}, host: {hostname}) event #{event_id}"
    )


@takeup.command("release")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--actor", required=True, help="Actor name")
@click.option(
    "--actor-kind",
    default="agent",
    type=click.Choice(["agent", "human"]),
    help="Actor kind",
)
@click.option("--instance-id", default=None, help="Stable actor instance ID")
@click.option("--runtime-session-id", default=None, help="Runtime session ID")
@click.option("--hostname", default=None, help="Hostname")
@click.option("--pid", type=int, default=None, help="Process ID")
@click.option("--reason", default=None, help="Release reason")
@click.option("--summary", default="sprint release", show_default=True, help="Event summary")
@click.option("--detail", default=None, help="Event detail")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def takeup_release_cmd(
    obj,
    sprint_id,
    actor,
    actor_kind,
    instance_id,
    runtime_session_id,
    hostname,
    pid,
    reason,
    summary,
    detail,
    as_json,
) -> None:
    """Record that an actor has released a sprint takeup."""
    store, m = _get_store(obj)
    sprint = m.get_sprint(store, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)

    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    matches = _matching_active_takeups(
        store,
        sprint_id=sprint_id,
        actor=actor,
        instance_id=instance_id,
        m=m,
    )
    matched = matches[-1] if matches else None
    matched_takeup_event_id = matched["taken_up_event_id"] if matched else None
    if matched is None:
        click.echo("No matching takeup found; recording release anyway.", err=True)

    event_id = m.create_event(
        store,
        sprint_id,
        actor,
        "sprint-released",
        payload=_takeup_payload(
            actor_kind=actor_kind,
            hostname=hostname,
            pid=pid,
            instance_id=instance_id,
            runtime_session_id=runtime_session_id,
            summary=summary,
            detail=detail,
            reason=reason,
            matched_takeup_event_id=matched_takeup_event_id,
        ),
    )
    _emit_audit_event(
        "sprint.released",
        summary=f"Sprint {sprint_id} released by {actor}",
        refs=[f"sprint:{sprint_id}"],
        metadata={"sprint_id": sprint_id, "event_type": "sprint-released", "actor": actor},
    )
    if as_json:
        click.echo(json.dumps({
            "operation": "takeup_release",
            "event_id": event_id,
            "sprint_id": sprint_id,
            "actor": actor,
            "actor_kind": actor_kind,
            "instance_id": instance_id,
            "hostname": hostname,
            "pid": pid,
            "reason": reason,
            "matched_takeup_event_id": matched_takeup_event_id,
        }, indent=2))
        return
    matched_label = (
        f"matched takeup #{matched_takeup_event_id}"
        if matched_takeup_event_id is not None
        else "no prior takeup"
    )
    click.echo(f"Sprint #{sprint_id} released by {actor} ({matched_label}) event #{event_id}")


@takeup.command("list")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID")
@click.option("--all-history", is_flag=True, default=False, help="Include released takeups")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def takeup_list_cmd(obj, sprint_id, all_history, as_json) -> None:
    """List current sprint takeups."""
    store, m = _get_store(obj)
    if sprint_id is not None and m.get_sprint(store, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    history = m.list_takeup_history(store, sprint_id)
    payload = {
        "operation": "takeup_list",
        "active_takeups": history["active_takeups"],
        "released_takeups": history["released_takeups"] if all_history else [],
        "unmatched_releases": history["unmatched_releases"] if all_history else [],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo("Active takeups:")
    _render_takeup_rows(payload["active_takeups"])
    if all_history:
        click.echo("\nReleased takeups:")
        _render_takeup_rows(payload["released_takeups"], released=True)
        if payload["unmatched_releases"]:
            click.echo("\nUnmatched releases:")
            for row in payload["unmatched_releases"]:
                click.echo(
                    f"  #{row['sprint_id']}  {row['actor']}  "
                    f"instance={_short_id(row.get('instance_id'))}  "
                    f"released={row.get('released_at')}  reason={row.get('reason') or '-'}"
                )


@takeup.command("show")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def takeup_show_cmd(obj, sprint_id, as_json) -> None:
    """Show full takeup history for a sprint."""
    store, m = _get_store(obj)
    sprint = m.get_sprint(store, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    history = m.list_takeup_history(store, sprint_id)
    payload = {
        "operation": "takeup_show",
        "sprint": sprint,
        "active_takeups": history["active_takeups"],
        "released_takeups": history["released_takeups"],
        "unmatched_releases": history["unmatched_releases"],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Sprint #{sprint_id}: {sprint['name']}")
    click.echo("\nActive takeups:")
    _render_takeup_rows(payload["active_takeups"])
    click.echo("\nReleased takeups:")
    _render_takeup_rows(payload["released_takeups"], released=True)
    if payload["unmatched_releases"]:
        click.echo("\nUnmatched releases:")
        for row in payload["unmatched_releases"]:
            click.echo(
                f"  {row['actor']}  instance={_short_id(row.get('instance_id'))}  "
                f"released={row.get('released_at')}  reason={row.get('reason') or '-'}"
            )


# ---------------------------------------------------------------------------


@click.group()
def maintain() -> None:
    """Maintenance commands (check, sweep, carryover)."""


def _resolve_sprint(conn, sprint_id: int | None, *, m=None) -> dict:
    m = m or _db
    if sprint_id is not None:
        s = m.get_sprint(conn, sprint_id)
        if s is None:
            click.echo(f"Sprint #{sprint_id} not found.", err=True)
            sys.exit(1)
    else:
        s = _resolve_implicit_sprint(conn, m=m)
        if s is None:
            click.echo("No active sprint found. Use --sprint-id to specify one.", err=True)
            sys.exit(1)
    return s


def _parse_threshold(threshold_str: str | None) -> timedelta | None:
    if threshold_str is None:
        return None
    raw = threshold_str.rstrip("h")
    try:
        return timedelta(hours=float(raw))
    except ValueError:
        click.echo(f"Invalid threshold '{threshold_str}' — use format like '4h'.", err=True)
        sys.exit(1)


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _event_payload(event: dict) -> dict:
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _summarize_event(event: dict) -> dict:
    payload = _event_payload(event)
    tags = payload.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {
        "id": event["id"],
        "event_id": event["id"],
        "event_type": event["event_type"],
        "created_at": event["created_at"],
        "actor": event["actor"],
        "work_item_id": event.get("work_item_id"),
        "summary": payload.get("summary") or event["event_type"],
        "detail": payload.get("detail"),
        "tags": tags,
    }


def _dependency_waiting_items(conn, sprint_id: int, *, m=None) -> list[dict]:
    m = m or _db
    waiting: list[dict] = []
    pending_items = m.list_work_items(conn, sprint_id=sprint_id, status="pending")
    for item in pending_items:
        blockers = m.list_deps_blocking(conn, item["id"])
        unresolved = [blocker for blocker in blockers if blocker["blocker_status"] != "done"]
        if not unresolved:
            continue
        waiting.append(
            {
                "id": item["id"],
                "title": item["title"],
                "track": item["track_name"],
                "assignee": item.get("assignee"),
                "unresolved_blockers": len(unresolved),
                "unresolved_blocker_ids": [blocker["item_id"] for blocker in unresolved],
                "unresolved_blocker_titles": [blocker["blocker_title"] for blocker in unresolved],
            }
        )
    return waiting


def _active_items_without_reservations(active_items: list[dict], active_reservations: list[dict]) -> list[dict]:
    reserved_item_ids = {reservation["work_item_id"] for reservation in active_reservations}
    return [item for item in active_items if item["id"] not in reserved_item_ids]


def _format_ref_line(ref: dict) -> str:
    label = f"  {ref['label']}" if ref.get("label") else ""
    return f"[{ref['ref_type']}]  {ref['url']}{label}"


def _echo_item_refs(refs: list[dict], item_id: int) -> None:
    if not refs:
        click.echo(
            f"Refs: (none — attach the spec/plan doc with "
            f"'sprintctl item ref add --id {item_id} --type doc --url docs/<path>')"
        )
        return
    click.echo(f"Refs on item #{item_id}:")
    for r in refs:
        click.echo(f"  {_format_ref_line(r)}")


def _render_repo_reference(repo_id: str | None, identifier: int) -> str:
    """Render a reusable item/sprint input without changing local UX."""
    return f"{repo_id}#{identifier}" if repo_id is not None else str(identifier)


def _collect_next_work_explained_payload(
    *,
    conn,
    sprint: dict,
    ready_items: list[dict],
    now: datetime,
    m=None,
    repo_id: str | None = None,
) -> dict:
    m = m or _db
    dependency_waiting_items = _dependency_waiting_items(conn, sprint["id"], m=m)
    active_reservations = m.list_reservations_by_sprint(conn, sprint["id"], active_only=True)
    active_items = [
        {"id": item["id"], "title": item["title"], "track": item["track_name"]}
        for item in m.list_work_items(conn, sprint_id=sprint["id"], status="active")
    ]
    active_unreserved_items = _active_items_without_reservations(active_items, active_reservations)
    conflicts = _derive_conflicts(
        active_reservations=active_reservations,
        active_unreserved_items=active_unreserved_items,
        blocked_items=[],
        stale_items=[],
        dependency_waiting_items=dependency_waiting_items,
        now=now,
    )
    next_action = _derive_next_action(
        active_reservations=active_reservations,
        active_unreserved_items=active_unreserved_items,
        conflicts=conflicts,
        ready_items=ready_items,
        blocked_items=[],
        stale_items=[],
        dependency_waiting_items=dependency_waiting_items,
    )
    recommended_commands = _recommended_commands_for_next_action(
        sprint_id=sprint["id"],
        next_action=next_action,
        repo_id=repo_id,
    )
    recommended_command_bundle = _recommended_command_bundle(
        commands=recommended_commands,
        next_action=next_action,
    )
    refs_by_ready_item = m.list_refs_for_items(conn, [item["id"] for item in ready_items])
    ready_with_reason = [
        {
            **item,
            "reason_code": "ready-unblocked",
            "reason": "No unresolved blocking dependencies.",
            "refs": refs_by_ready_item.get(item["id"], []),
        }
        for item in ready_items
    ]
    dependency_waiting_with_reason = [
        {
            **item,
            "reason_code": "waiting-on-dependencies",
            "reason": "One or more blocking dependencies are not done.",
        }
        for item in dependency_waiting_items
    ]
    visible_reservations = [
        {
            "id": reservation["id"],
            "work_item_id": reservation["work_item_id"],
            "actor": reservation["actor"],
            "role": reservation["role"],
            "session_id": reservation["session_id"],
            "stale": reservation.get("stale", False),
        }
        for reservation in active_reservations
    ]
    return {
        "contract_version": "1",
        "sprint": {
            "id": sprint["id"],
            "name": sprint["name"],
            "status": sprint["status"],
        },
        "summary": {
            "pending_total": len(ready_items) + len(dependency_waiting_items),
            "ready": len(ready_items),
            "waiting_on_dependencies": len(dependency_waiting_items),
            "active_reservations": len(visible_reservations),
            "active_unreserved": len(active_unreserved_items),
        },
        "ready_items": ready_with_reason,
        "dependency_waiting_items": dependency_waiting_with_reason,
        "active_reservations": visible_reservations,
        "active_unreserved_items": active_unreserved_items,
        "conflicts": conflicts,
        "next_action": next_action,
        "recommended_commands": recommended_commands,
        "recommended_command_bundle": recommended_command_bundle,
    }


def _render_next_work_explained_text(payload: dict) -> str:
    sprint = payload["sprint"]
    summary = payload["summary"]
    lines = [
        f"Sprint #{sprint['id']}: {sprint['name']}",
        (
            "Summary: "
            f"{summary['pending_total']} pending total, "
            f"{summary['ready']} ready, "
            f"{summary['waiting_on_dependencies']} waiting on dependencies, "
            f"{summary['active_reservations']} active reservations, "
            f"{summary['active_unreserved']} active unreserved"
        ),
        "",
    ]

    ready_items = payload["ready_items"]
    lines.append(f"Ready items ({len(ready_items)}):")
    if ready_items:
        rows: list[list[str]] = []
        for item in ready_items:
            rows.append(
                [
                    f"#{item['id']}",
                    item["track_name"],
                    item.get("assignee") or "-",
                    item["title"],
                ]
            )
        for line in _render_table(["ID", "TRACK", "ASSIGNEE", "TITLE"], rows):
            lines.append(f"  {line}")
        items_with_refs = [item for item in ready_items if item.get("refs")]
        lines.append("  Refs:")
        if items_with_refs:
            for item in items_with_refs:
                for ref in item["refs"]:
                    lines.append(f"    #{item['id']}  {_format_ref_line(ref)}")
            without = [item for item in ready_items if not item.get("refs")]
            if without:
                ids = ", ".join(f"#{item['id']}" for item in without)
                lines.append(f"    (no refs: {ids})")
        else:
            lines.append("    (none — ready items carry no doc refs; see 'item ref add --type doc')")
    else:
        lines.append("  (none)")
    lines.append("")

    waiting_items = payload["dependency_waiting_items"]
    lines.append(f"Dependency waiting items ({len(waiting_items)}):")
    if waiting_items:
        rows = []
        for item in waiting_items:
            blocker_ids = ",".join(f"#{bid}" for bid in item["unresolved_blocker_ids"])
            rows.append(
                [
                    f"#{item['id']}",
                    item["track"],
                    item.get("assignee") or "-",
                    blocker_ids,
                    item["title"],
                ]
            )
        for line in _render_table(["ID", "TRACK", "ASSIGNEE", "BLOCKERS", "TITLE"], rows):
            lines.append(f"  {line}")
    else:
        lines.append("  (none)")
    lines.append("")

    active_reservations = payload["active_reservations"]
    lines.append(f"Active reservations ({len(active_reservations)}):")
    if active_reservations:
        rows = []
        for reservation in active_reservations:
            rows.append(
                [
                    f"#{reservation['id']}",
                    f"#{reservation['work_item_id']}",
                    reservation["actor"],
                    reservation["role"],
                    reservation["session_id"],
                ]
            )
        for line in _render_table(["RESERVATION", "ITEM", "ACTOR", "ROLE", "SESSION"], rows):
            lines.append(f"  {line}")
    else:
        lines.append("  (none)")
    lines.append("")

    active_unreserved_items = payload["active_unreserved_items"]
    lines.append(f"Active items without reservations ({len(active_unreserved_items)}):")
    if active_unreserved_items:
        rows = []
        for item in active_unreserved_items:
            rows.append(
                [
                    f"#{item['id']}",
                    item["track"],
                    item["title"],
                ]
            )
        for line in _render_table(["ID", "TRACK", "TITLE"], rows):
            lines.append(f"  {line}")
    else:
        lines.append("  (none)")
    lines.append("")

    conflicts = payload["conflicts"]
    lines.append(f"Conflicts ({len(conflicts)}):")
    if conflicts:
        for conflict in conflicts:
            lines.append(f"  [{conflict['kind']}]  {conflict['summary']}")
    else:
        lines.append("  (none)")
    lines.append("")

    next_action = payload["next_action"]
    lines.append("Next action:")
    lines.append(f"  [{next_action['kind']}]  {next_action['summary']}")
    lines.append("")

    commands = payload.get("recommended_commands", [])
    lines.append("Recommended commands:")
    if commands:
        for command in commands:
            lines.append(f"  - {command}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def _collect_session_resume_payload(*, conn, sprint: dict, now: datetime, m=None) -> dict:
    m = m or _db
    context = _collect_context_contract(conn, sprint, now, m=m)
    current_runtime_session_id = _detect_runtime_session_id(None)
    current_instance_id = os.environ.get("SPRINTCTL_INSTANCE_ID")
    ready_items = m.get_ready_items(conn, sprint["id"])
    next_work = _collect_next_work_explained_payload(
        conn=conn,
        sprint=sprint,
        ready_items=ready_items,
        now=now,
        m=m,
    )
    # Keep a single primary recommendation for resume flows and recompute command guidance.
    next_action = context["next_action"]
    next_work["next_action"] = next_action
    next_work["recommended_commands"] = _recommended_commands_for_next_action(
        sprint_id=sprint["id"],
        next_action=next_action,
    )
    next_work["recommended_command_bundle"] = _recommended_command_bundle(
        commands=next_work["recommended_commands"],
        next_action=next_action,
    )
    recommended_sequence = [
        f"sprintctl usage --context --sprint-id {sprint['id']} --json",
        f"sprintctl next-work --sprint-id {sprint['id']} --json --explain",
        "sprintctl reservation list --all --json",
    ]
    reserved_item_refs = m.list_refs_for_items(
        conn, [reservation["work_item_id"] for reservation in context["active_reservations"]]
    )
    reservation_status = {
        "current_identity": {
            "runtime_session_id": current_runtime_session_id,
            "instance_id": current_instance_id,
        },
        "active_reservations": [
            {
                **reservation,
                "session_matches": reservation["session_id"] == current_runtime_session_id,
                "refs": reserved_item_refs.get(reservation["work_item_id"], []),
            }
            for reservation in context["active_reservations"]
        ],
    }
    return {
        "contract_version": "2",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sprint": {
            "id": sprint["id"],
            "name": sprint["name"],
            "status": sprint["status"],
        },
        "context": context,
        "next_work": next_work,
        "git_context": _detect_git_context(),
        "reservation_status": reservation_status,
        "next_action": next_action,
        "recommended_sequence": recommended_sequence,
        "recommended_sequence_bundle": _recommended_command_bundle(
            commands=recommended_sequence,
            next_action=next_action,
        ),
    }


def _render_session_resume_text(payload: dict) -> str:
    sprint = payload["sprint"]
    next_action = payload["next_action"]
    reservation_status = payload.get("reservation_status", {})
    lines = [
        f"Session resume for sprint #{sprint['id']}: {sprint['name']}",
        f"Generated: {payload['generated_at']}",
        "",
        "Recommended sequence:",
    ]
    for command in payload["recommended_sequence"]:
        lines.append(f"  - {command}")

    lines.append("")
    lines.append("Next action:")
    lines.append(f"  [{next_action['kind']}]  {next_action['summary']}")
    lines.append("")
    lines.append("Git context:")

    git_context = payload["git_context"]
    if git_context is None:
        lines.append("  (not in a git repository)")
    else:
        lines.append(f"  Branch:   {git_context['branch']}")
        lines.append(f"  SHA:      {git_context['sha']}")
        lines.append(f"  Worktree: {git_context['worktree']}")
        dirty_files = git_context.get("dirty_files") or []
        lines.append(f"  Dirty files: {len(dirty_files)}")

    lines.append("")
    lines.append("Reservation status:")
    active_reservations = reservation_status.get("active_reservations", [])
    if not active_reservations:
        lines.append("  (no active reservations)")
    else:
        for reservation in active_reservations:
            lines.append(
                f"  Reservation #{reservation['id']} item #{reservation['work_item_id']}: "
                f"session_match={'yes' if reservation['session_matches'] else 'no'}"
            )
            refs = reservation.get("refs", [])
            if refs:
                for ref in refs:
                    lines.append(f"    ref: {_format_ref_line(ref)}")
            else:
                lines.append("    ref: (none — no doc attached to this item)")

    lines.append("")
    lines.append("usage --context snapshot:")
    for line in _render_context_text(payload["context"]).splitlines():
        lines.append(f"  {line}")

    lines.append("")
    lines.append("next-work --explain snapshot:")
    for line in _render_next_work_explained_text(payload["next_work"]).splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)


def _derive_conflicts(
    *,
    active_reservations: list[dict],
    active_unreserved_items: list[dict],
    blocked_items: list[dict],
    stale_items: list[dict],
    dependency_waiting_items: list[dict],
    now: datetime,
) -> list[dict]:
    conflicts: list[dict] = []

    stale_reservations = [reservation for reservation in active_reservations if reservation.get("stale")]
    if stale_reservations:
        conflicts.append(
            {
                "kind": "stale-reservation",
                "severity": "warning",
                "summary": f"{len(stale_reservations)} active reservation(s) have been idle for four hours.",
                "reservation_ids": [reservation["id"] for reservation in stale_reservations],
                "item_ids": [reservation["work_item_id"] for reservation in stale_reservations],
            }
        )

    if active_unreserved_items:
        conflicts.append(
            {
                "kind": "unreserved-active-work",
                "reason_code": "active-item-without-reservation",
                "severity": "warning",
                "summary": (
                    f"{len(active_unreserved_items)} active item(s) have no reservation "
                    "and need resume, reassignment, or status triage."
                ),
                "item_ids": [item["id"] for item in active_unreserved_items],
            }
        )

    if dependency_waiting_items:
        blocker_ids = sorted(
            {
                blocker_id
                for item in dependency_waiting_items
                for blocker_id in item["unresolved_blocker_ids"]
            }
        )
        conflicts.append(
            {
                "kind": "dependency-blocked",
                "severity": "warning",
                "summary": (
                    f"{len(dependency_waiting_items)} pending item(s) are waiting on unresolved blockers."
                ),
                "item_ids": [item["id"] for item in dependency_waiting_items],
                "blocker_ids": blocker_ids,
            }
        )

    if blocked_items:
        conflicts.append(
            {
                "kind": "blocked-work",
                "severity": "warning",
                "summary": f"{len(blocked_items)} item(s) are explicitly blocked and need triage.",
                "item_ids": [item["id"] for item in blocked_items],
            }
        )

    if stale_items:
        conflicts.append(
            {
                "kind": "stale-work",
                "severity": "warning",
                "summary": f"{len(stale_items)} item(s) are stale and may be drifting out of date.",
                "item_ids": [item["id"] for item in stale_items],
            }
        )

    return conflicts


def _derive_next_action(
    *,
    active_reservations: list[dict],
    active_unreserved_items: list[dict],
    conflicts: list[dict],
    ready_items: list[dict],
    blocked_items: list[dict],
    stale_items: list[dict],
    dependency_waiting_items: list[dict],
) -> dict:
    if conflicts:
        first = conflicts[0]
        if first["kind"] == "stale-reservation":
            return {
                "kind": "review-stale-reservation",
                "summary": "Review or reassign the stale reservation.",
                "reservation_id": first["reservation_ids"][0],
                "item_id": first["item_ids"][0],
                "reason": first["summary"],
            }
        if first["kind"] == "unreserved-active-work":
            item = active_unreserved_items[0]
            return {
                "kind": "resume-unreserved-active-item",
                "summary": f"Resume or triage active item #{item['id']} because it has no reservation.",
                "item_id": item["id"],
                "reason": first["summary"],
            }
        if first["kind"] == "dependency-blocked":
            waiting = dependency_waiting_items[0]
            return {
                "kind": "unblock-dependent-work",
                "summary": (
                    f"Resolve blocker #{waiting['unresolved_blocker_ids'][0]} "
                    f"to unblock item #{waiting['id']}."
                ),
                "item_id": waiting["id"],
                "blocker_item_id": waiting["unresolved_blocker_ids"][0],
                "reason": first["summary"],
            }
        if first["kind"] == "blocked-work":
            item = blocked_items[0]
            return {
                "kind": "triage-blocked-item",
                "summary": f"Triage blocked item #{item['id']} before pulling new work.",
                "item_id": item["id"],
                "reason": first["summary"],
            }
        if first["kind"] == "stale-work":
            item = stale_items[0]
            return {
                "kind": "refresh-stale-item",
                "summary": f"Refresh stale item #{item['id']} before it drifts further.",
                "item_id": item["id"],
                "reason": first["summary"],
            }

    if active_reservations:
        reservation = active_reservations[0]
        return {
            "kind": "inspect-active-reservation",
            "summary": f"Inspect reserved item #{reservation['work_item_id']} before starting new work.",
            "reservation_id": reservation["id"],
            "item_id": reservation["work_item_id"],
            "reason": "Active reserved work already exists in this sprint.",
        }

    if ready_items:
        item = ready_items[0]
        return {
            "kind": "start-ready-item",
            "summary": f"Start ready item #{item['id']} because it is unblocked and no active reservations are open.",
            "item_id": item["id"],
            "reason": "Ready work is available now.",
        }

    if dependency_waiting_items:
        waiting = dependency_waiting_items[0]
        return {
            "kind": "resolve-blocker",
            "summary": (
                f"Resolve blocker #{waiting['unresolved_blocker_ids'][0]} "
                f"to unblock item #{waiting['id']}."
            ),
            "item_id": waiting["id"],
            "blocker_item_id": waiting["unresolved_blocker_ids"][0],
            "reason": "All pending work is currently waiting on dependencies.",
        }

    return {
        "kind": "no-action",
        "summary": "No immediate action is suggested from current sprint state.",
        "reason": "There is no ready, active, blocked, or stale work to prioritize.",
    }


def _recommended_commands_for_next_action(
    *, sprint_id: int, next_action: dict, repo_id: str | None = None
) -> list[str]:
    kind = next_action.get("kind")
    item_id = next_action.get("item_id")
    reservation_id = next_action.get("reservation_id")
    blocker_id = next_action.get("blocker_item_id")
    sprint_ref = _render_repo_reference(repo_id, sprint_id)
    item_ref = lambda identifier: _render_repo_reference(repo_id, identifier)

    if kind in {"unblock-dependent-work", "resolve-blocker"}:
        commands = []
        if blocker_id is not None:
            commands.append(f"sprintctl item show --id {item_ref(blocker_id)}")
        if item_id is not None:
            commands.append(f"sprintctl item show --id {item_ref(item_id)}")
        commands.append(f"sprintctl next-work --sprint-id {sprint_ref} --json --explain")
        return commands

    if kind == "inspect-active-reservation":
        commands = []
        if item_id is not None:
            commands.append(f"sprintctl item show --id {item_ref(item_id)}")
        if reservation_id is not None:
            commands.append(f"sprintctl reservation show --id {reservation_id} --json")
        return commands

    if kind in {"resume-unreserved-active-item", "start-ready-item"}:
        commands = []
        if item_id is not None:
            commands.extend(
                [
                    f"sprintctl reservation reserve --item-id {item_ref(item_id)} --actor <name> --session-id <session-id> --json",
                    f"sprintctl item show --id {item_ref(item_id)}",
                ]
            )
        return commands

    if kind in {"triage-blocked-item", "refresh-stale-item"}:
        if item_id is None:
            return []
        return [f"sprintctl item show --id {item_ref(item_id)}"]

    if kind == "no-action":
        return [
            f"sprintctl usage --context --sprint-id {sprint_ref} --json",
            f"sprintctl next-work --sprint-id {sprint_ref} --json --explain",
        ]

    return []


def _recommended_command_bundle(*, commands: list[str], next_action: dict) -> dict:
    steps: list[dict] = []
    for idx, command in enumerate(commands, start=1):
        placeholders = re.findall(r"<[^>\n]+>", command)
        steps.append(
            {
                "step": idx,
                "kind": _command_step_kind(command),
                "command": command,
                "placeholders": placeholders,
                "requires_input": bool(placeholders),
                "is_executable": not placeholders,
            }
        )
    return {
        "bundle_version": "1",
        "next_action_kind": next_action.get("kind"),
        "steps": steps,
    }


def _command_step_kind(command: str) -> str:
    if command.startswith("sprintctl reservation reserve"):
        return "reservation-reserve"
    if command.startswith("sprintctl reservation list"):
        return "reservation-list"
    if command.startswith("sprintctl reservation show"):
        return "reservation-show"
    if command.startswith("sprintctl item show"):
        return "item-show"
    if command.startswith("sprintctl usage --context"):
        return "usage-context"
    if command.startswith("sprintctl next-work"):
        return "next-work"
    return "other"


def _collect_context_contract(conn, sprint: dict, now: datetime, *, m=None) -> dict:
    return _context_contract.build_context_contract(conn, sprint, now, backend=m or _db)


def _render_context_text(snapshot: dict) -> str:
    sprint = snapshot["sprint"]
    summary = snapshot["summary"]
    lines = [f"Sprint #{sprint['id']}: {sprint['name']}", f"Goal: {sprint['goal']}"]
    if sprint.get("start_date") and sprint.get("end_date"):
        lines.append(f"Dates: {sprint['start_date']} -> {sprint['end_date']}")
    lines.append(
        "Items: "
        f"{summary['total']} total — "
        f"{summary['done']} done, {summary['active']} active, "
        f"{summary['pending']} pending, {summary['blocked']} blocked"
    )
    lines.append("")

    active_reservations = snapshot["active_reservations"]
    lines.append(f"Active reservations ({len(active_reservations)}):")
    if active_reservations:
        for reservation in active_reservations:
            item_title = reservation.get("item_title") or f"item #{reservation['work_item_id']}"
            lines.append(
                f"  reservation #{reservation['id']}  [{reservation['actor']}]  {item_title}  "
                f"session: {reservation['session_id']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    active_unreserved_items = snapshot["active_unreserved_items"]
    lines.append(f"Active items without reservations ({len(active_unreserved_items)}):")
    if active_unreserved_items:
        for item in active_unreserved_items:
            lines.append(f"  #{item['id']}  {item['title']}  (track: {item['track']})")
    else:
        lines.append("  (none)")
    lines.append("")

    conflicts = snapshot["conflicts"]
    lines.append(f"Conflicts ({len(conflicts)}):")
    if conflicts:
        for conflict in conflicts:
            lines.append(f"  [{conflict['kind']}]  {conflict['summary']}")
    else:
        lines.append("  (none)")
    lines.append("")

    ready_items = snapshot["ready_items"]
    lines.append(f"Ready to start ({len(ready_items)}):")
    if ready_items:
        for item in ready_items[:5]:
            lines.append(f"  #{item['id']}  {item['title']}  (track: {item['track']})")
        if len(ready_items) > 5:
            lines.append(f"  ... {len(ready_items) - 5} more")
    else:
        lines.append("  (none)")
    lines.append("")

    blocked_items = snapshot["blocked_items"]
    lines.append(f"Blocked items ({len(blocked_items)}):")
    if blocked_items:
        for item in blocked_items:
            lines.append(f"  #{item['id']}  {item['title']}  (track: {item['track']})")
    else:
        lines.append("  (none)")
    lines.append("")

    stale_items = snapshot["stale_items"]
    lines.append(f"Stale items ({len(stale_items)}):")
    if stale_items:
        for item in stale_items:
            hours, rem = divmod(item["idle_seconds"], 3600)
            minutes = rem // 60
            lines.append(
                f"  #{item['id']}  [{item['status']:8}]  {item['title']}  "
                f"— idle {hours}h{minutes:02d}m  (track: {item['track']})"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    recent_decisions = snapshot["recent_decisions"]
    lines.append(f"Recent decisions ({len(recent_decisions)}):")
    if recent_decisions:
        for decision in recent_decisions:
            lines.append(f"  [{decision['event_type']}]  {decision['summary']}")
    else:
        lines.append("  (none)")
    lines.append("")

    next_action = snapshot["next_action"]
    lines.append("Next action:")
    lines.append(f"  [{next_action['kind']}]  {next_action['summary']}")
    return "\n".join(lines)


def _detect_git_context() -> dict | None:
    import subprocess  # noqa: PLC0415

    def _run(args: list[str]) -> str:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError
        return result.stdout.rstrip("\n")

    try:
        status = _run(["git", "status", "--porcelain=v2", "--branch"])
        worktree = _run(["git", "rev-parse", "--show-toplevel"])
    except RuntimeError:
        return None

    branch = "HEAD"
    sha = ""
    dirty_files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
            continue
        if line.startswith("# branch.oid "):
            sha = line.removeprefix("# branch.oid ")
            continue
        if line.startswith("? "):
            dirty_files.append(line[2:].strip())
            continue
        if line.startswith("1 ") or line.startswith("u "):
            fields = line.split(" ", 8)
            if len(fields) == 9:
                dirty_files.append(fields[8])
            continue
        if line.startswith("2 "):
            fields = line.split(" ", 9)
            if len(fields) == 10:
                dirty_files.append(fields[9].split("\t", 1)[0])

    return {
        "branch": branch,
        "sha": sha,
        "worktree": worktree,
        "dirty_files": dirty_files,
    }


def _previous_handoff_generated(conn, sprint_id: int, *, m=None) -> dict | None:
    m = m or _db
    events = m.list_events(conn, sprint_id)
    for event in reversed(events):
        if event["event_type"] == "handoff-generated":
            return event
    return None


def _build_handoff_bundle(conn, sprint: dict, events_limit: int, *, m=None) -> dict:
    from .. import handoff
    return handoff.build_handoff_bundle(conn, sprint, events_limit, backend=m or _db, version=__version__, git_context=_detect_git_context())


def _record_handoff_generated(conn, sprint_id: int, bundle: dict, *, m=None) -> None:
    from .. import handoff
    handoff.record_handoff_generated(conn, sprint_id, bundle, backend=m or _db, actor="handoff")


@maintain.command("check")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--threshold", default=None, help="Staleness threshold, e.g. 4h (default: 4h)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON")
@click.pass_obj
def maintain_check(obj, sprint_id, threshold, as_json) -> None:
    """Dry-run: report stale items and sprint health (no writes)."""
    store, m = _get_store(obj)
    s = _resolve_sprint(store, sprint_id, m=m)
    now = datetime.now(timezone.utc)
    td = _parse_threshold(threshold)
    report = _maintain.check(store, s["id"], now, threshold=td, _m=m)

    if as_json:
        pt = report["pending_threshold"]
        out = {
            "sprint": report["sprint"],
            "risk": report["risk"],
            "stale_items": report["stale_items"],
            "track_health": report["track_health"],
            "findings": report["findings"],
            "threshold_hours": report["threshold"].total_seconds() / 3600,
            "pending_threshold_hours": pt.total_seconds() / 3600 if pt else None,
        }
        click.echo(json.dumps(out, indent=2))
        return

    sprint = report["sprint"]
    risk = report["risk"]
    stale = report["stale_items"]
    track_health = report["track_health"]
    findings = report["findings"]
    threshold_hours = report["threshold"].total_seconds() / 3600
    pending_threshold = report["pending_threshold"]

    risk_tag = ""
    if risk["overdue"]:
        risk_tag = "  [OVERDUE]"
    elif risk["at_risk"]:
        risk_tag = "  [AT RISK]"
    if risk.get("date_bound", True):
        date_info = f"{risk['days_remaining']} days remaining, "
    else:
        date_info = ""
    click.echo(
        f"Sprint #{sprint['id']}: \"{sprint['name']}\" — "
        f"{date_info}{risk['active_items']} active item(s){risk_tag}"
    )
    click.echo("")

    pending_label = f", pending: {pending_threshold.total_seconds() / 3600:g}h" if pending_threshold else ", pending: off"
    click.echo(f"Stale items (active threshold: {threshold_hours:g}h{pending_label}):")
    if stale:
        for it in stale:
            h, rem = divmod(it["idle_seconds"], 3600)
            m = rem // 60
            idle = f"{h}h{m:02d}m"
            click.echo(f"  #{it['id']}  [{it['status']:8}]  {it['title']}  — idle {idle}  (track: {it['track_name']})")
    else:
        click.echo("  (none)")
    click.echo("")

    click.echo(f"Truth findings ({len(findings)}):")
    if findings:
        for finding in findings:
            click.echo(f"  [{finding['reason_code']}]  {finding['summary']}")
    else:
        click.echo("  (none)")
    click.echo("")

    click.echo("Track health:")
    for name, health in track_health.items():
        done_pct = int(health["done_ratio"] * 100)
        blocked_pct = int(health["blocked_ratio"] * 100)
        c = health["counts"]
        click.echo(
            f"  {name}: {health['total']} items — "
            f"{c['done']} done ({done_pct}%), "
            f"{c['active']} active, "
            f"{c['pending']} pending, "
            f"{c['blocked']} blocked ({blocked_pct}%)"
        )


@maintain.command("sweep")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--threshold", default=None, help="Staleness threshold, e.g. 4h (default: 4h)")
@click.option("--auto-close", is_flag=True, default=False,
              help="Auto-close overdue sprint if no active items remain after sweep")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def maintain_sweep(obj, sprint_id, threshold, auto_close, as_json) -> None:
    """Execute: block stale items and optionally auto-close overdue sprint."""
    store, m = _get_store(obj)
    s = _resolve_sprint(store, sprint_id, m=m)
    now = datetime.now(timezone.utc)
    td = _parse_threshold(threshold)
    result = _maintain.sweep(store, s["id"], now, threshold=td, auto_close=auto_close, _m=m)

    if as_json:
        click.echo(json.dumps({
            "sprint_id": s["id"],
            "blocked_items": [{"id": it["id"], "title": it["title"]} for it in result["blocked_items"]],
            "stale_reservations_interrupted": result["stale_reservations_interrupted"],
            "auto_closed": result["auto_closed"],
        }, indent=2))
        return

    blocked = result["blocked_items"]
    if blocked:
        click.echo(f"Blocked {len(blocked)} stale item(s):")
        for it in blocked:
            click.echo(f"  #{it['id']}  {it['title']}")
    else:
        click.echo("No stale items to block.")

    interrupted = result["stale_reservations_interrupted"]
    if interrupted:
        click.echo(f"Interrupted {len(interrupted)} stale reservation(s).")

    if result["auto_closed"]:
        click.echo(f"Sprint #{s['id']} auto-closed (overdue, no active items).")


@maintain.command("carryover")
@click.option("--from-sprint", "from_sprint_id", type=int, required=True, help="Source sprint ID")
@click.option("--to-sprint", "to_sprint_id", type=int, required=True, help="Target sprint ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def maintain_carryover(obj, from_sprint_id, to_sprint_id, as_json) -> None:
    """Carry incomplete items from one sprint to another."""
    store, m = _get_store(obj)
    if m.get_sprint(store, from_sprint_id) is None:
        click.echo(f"Source sprint #{from_sprint_id} not found.", err=True)
        sys.exit(1)
    if m.get_sprint(store, to_sprint_id) is None:
        click.echo(f"Target sprint #{to_sprint_id} not found.", err=True)
        sys.exit(1)
    try:
        created = _maintain.carryover(store, from_sprint_id, to_sprint_id, _m=m)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps({
            "from_sprint_id": from_sprint_id,
            "to_sprint_id": to_sprint_id,
            "carried_items": created,
        }, indent=2))
        return
    if created:
        click.echo(f"Carried {len(created)} item(s) from sprint #{from_sprint_id} to #{to_sprint_id}:")
        for it in created:
            click.echo(f"  #{it['id']}  {it['title']}")
    else:
        click.echo("No incomplete items to carry over.")


# ---------------------------------------------------------------------------

# claim
# ---------------------------------------------------------------------------











def _render_handoff_text(bundle: dict) -> str:
    """Render a handoff bundle as a human-readable text summary."""
    s = bundle["sprint"]
    reservations = bundle["active_reservations"]
    work = bundle["work"]
    recent_decisions = bundle["recent_decisions"]
    recent_events = bundle["recent_events"]
    next_action = bundle["next_action"]

    lines: list[str] = []
    lines.append(f"=== HANDOFF: {s['name']}  [{s['status']}] ===")
    lines.append(f"Generated: {bundle['generated_at']}")
    if s.get("goal"):
        lines.append(f"Goal: {s['goal']}")
    if s.get("start_date") and s.get("end_date"):
        lines.append(f"Dates: {s['start_date']} to {s['end_date']}")
    summary = bundle["summary"]
    lines.append(
        "Summary: "
        f"{summary['total']} total, {summary['done']} done, {summary['active']} active, "
        f"{summary['pending']} pending, {summary['blocked']} blocked"
    )
    lines.append("")

    lines.append(f"ACTIVE WORK ({len(work['active_items'])}):")
    if work["active_items"]:
        for item in work["active_items"]:
            lines.append(f"  #{item['id']}  {item['title']}  [track: {item['track']}]")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"READY TO START ({len(work['ready_items'])}):")
    if work["ready_items"]:
        for item in work["ready_items"]:
            lines.append(f"  #{item['id']}  {item['title']}  [track: {item['track']}]")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"BLOCKED ITEMS ({len(work['blocked_items'])}):")
    if work["blocked_items"]:
        for item in work["blocked_items"]:
            lines.append(f"  #{item['id']}  {item['title']}  [track: {item['track']}]")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"STALE ITEMS ({len(work['stale_items'])}):")
    if work["stale_items"]:
        for item in work["stale_items"]:
            idle_hours, rem = divmod(item["idle_seconds"], 3600)
            idle_minutes = rem // 60
            lines.append(
                f"  #{item['id']}  [{item['status']:8}]  {item['title']}  "
                f"idle {idle_hours}h{idle_minutes:02d}m  [track: {item['track']}]"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    if reservations:
        lines.append(f"ACTIVE RESERVATIONS ({len(reservations)}):")
        for reservation in reservations:
            lines.append(
                f"  #{reservation['id']}  item #{reservation['work_item_id']} "
                f"({reservation.get('item_title', '')})  {reservation['actor']}  "
                f"[{reservation['role']}]  session={reservation['session_id']}"
            )
        lines.append("")
        lines.append("NOTE: Incoming agent should reassign or release each active reservation.")
        lines.append("")

    conflicts = bundle["conflicts"]
    lines.append(f"CONFLICTS ({len(conflicts)}):")
    if conflicts:
        for conflict in conflicts:
            lines.append(f"  [{conflict['kind']}]  {conflict['summary']}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"RECENT DECISIONS ({len(recent_decisions)}):")
    if recent_decisions:
        for event in recent_decisions:
            lines.append(f"  [{event['event_type']}]  {event['summary']}")
    else:
        lines.append("  (none)")
    lines.append("")

    if recent_events:
        lines.append(f"RECENT EVENTS ({len(recent_events)}):")
        for event in recent_events[-10:]:
            item_label = f"  item #{event['work_item_id']}" if event.get("work_item_id") else ""
            lines.append(f"  [{event['event_type']}]  {event['actor']}  {event['created_at']}{item_label}")
        lines.append("")

    lines.append("NEXT ACTION:")
    lines.append(f"  [{next_action['kind']}]  {next_action['summary']}")
    lines.append("")

    lines.append("SHUTDOWN PROTOCOL:")
    for step in bundle.get("agent_shutdown_protocol", {}).get("required_before_termination", []):
        lines.append(f"  - {step}")
    lines.append("")

    lines.append("RESUME PATH:")
    for step in bundle.get("resume_instructions", []):
        lines.append(f"  - {step}")

    return "\n".join(lines)





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


def _register(root: click.Group, runtime: dict[str, object], commands: tuple[click.Command, ...]) -> None:
    global __runtime_source
    __runtime_source = runtime
    _RUNTIME.clear()
    _RUNTIME.update({name: value for name, value in runtime.items() if not name.startswith("__")})
    _sync_runtime()
    for command in commands:
        root.add_command(command)
        _wrap_runtime_callbacks(command)


def register_takeup_maintain(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach the takeup and maintain groups."""
    _register(root, runtime, (takeup, maintain))
