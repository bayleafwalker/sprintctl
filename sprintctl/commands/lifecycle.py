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


def _active_items_without_claims(active_items: list[dict], active_claims: list[dict]) -> list[dict]:
    claimed_item_ids = {claim["work_item_id"] for claim in active_claims}
    return [item for item in active_items if item["id"] not in claimed_item_ids]


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
    active_claims = m.list_claims_by_sprint(conn, sprint["id"], active_only=True)
    active_items = [
        {"id": item["id"], "title": item["title"], "track": item["track_name"]}
        for item in m.list_work_items(conn, sprint_id=sprint["id"], status="active")
    ]
    active_unclaimed_items = _active_items_without_claims(active_items, active_claims)
    conflicts = _derive_conflicts(
        active_claims=active_claims,
        active_unclaimed_items=active_unclaimed_items,
        blocked_items=[],
        stale_items=[],
        dependency_waiting_items=dependency_waiting_items,
        now=now,
    )
    next_action = _derive_next_action(
        active_claims=active_claims,
        active_unclaimed_items=active_unclaimed_items,
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
    visible_claims = [
        {
            "claim_id": claim["claim_id"],
            "work_item_id": claim["work_item_id"],
            "agent": claim["agent"],
            "claim_type": claim["claim_type"],
            "expires_at": claim["expires_at"],
            "identity_status": claim.get("identity_status"),
        }
        for claim in active_claims
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
            "active_claims": len(visible_claims),
            "active_unclaimed": len(active_unclaimed_items),
        },
        "ready_items": ready_with_reason,
        "dependency_waiting_items": dependency_waiting_with_reason,
        "active_claims": visible_claims,
        "active_unclaimed_items": active_unclaimed_items,
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
            f"{summary['active_claims']} active claims, "
            f"{summary['active_unclaimed']} active unclaimed"
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

    active_claims = payload["active_claims"]
    lines.append(f"Active claims ({len(active_claims)}):")
    if active_claims:
        rows = []
        for claim in active_claims:
            rows.append(
                [
                    f"#{claim['claim_id']}",
                    f"#{claim['work_item_id']}",
                    claim["agent"],
                    claim["claim_type"],
                    claim["expires_at"],
                ]
            )
        for line in _render_table(["CLAIM", "ITEM", "AGENT", "TYPE", "EXPIRES_AT"], rows):
            lines.append(f"  {line}")
    else:
        lines.append("  (none)")
    lines.append("")

    active_unclaimed_items = payload["active_unclaimed_items"]
    lines.append(f"Active items without claims ({len(active_unclaimed_items)}):")
    if active_unclaimed_items:
        rows = []
        for item in active_unclaimed_items:
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


def _claims_expiring_within(active_claims: list[dict], now: datetime, seconds: int) -> list[dict]:
    expiring: list[dict] = []
    for claim in active_claims:
        expires_at = _parse_utc_timestamp(claim.get("expires_at"))
        if expires_at is None:
            continue
        if (expires_at - now).total_seconds() <= seconds:
            expiring.append(claim)
    return expiring


def _derive_conflicts(
    *,
    active_claims: list[dict],
    active_unclaimed_items: list[dict],
    blocked_items: list[dict],
    stale_items: list[dict],
    dependency_waiting_items: list[dict],
    now: datetime,
) -> list[dict]:
    conflicts: list[dict] = []

    legacy_claims = [claim for claim in active_claims if claim.get("identity_status") != "proven"]
    if legacy_claims:
        conflicts.append(
            {
                "kind": "claim-identity",
                "severity": "warning",
                "summary": (
                    f"{len(legacy_claims)} active claim(s) have ambiguous ownership proof "
                    "and require explicit adoption or expiry."
                ),
                "claim_ids": [claim["claim_id"] for claim in legacy_claims],
                "item_ids": [claim["work_item_id"] for claim in legacy_claims],
            }
        )

    expiring_claims = _claims_expiring_within(active_claims, now, seconds=120)
    if expiring_claims:
        conflicts.append(
            {
                "kind": "claim-expiry",
                "severity": "warning",
                "summary": (
                    f"{len(expiring_claims)} active claim(s) expire within 120 seconds "
                    "and may need heartbeat or handoff."
                ),
                "claim_ids": [claim["claim_id"] for claim in expiring_claims],
                "item_ids": [claim["work_item_id"] for claim in expiring_claims],
            }
        )

    if active_unclaimed_items:
        conflicts.append(
            {
                "kind": "unclaimed-active-work",
                "reason_code": "active-item-without-live-claim",
                "severity": "warning",
                "summary": (
                    f"{len(active_unclaimed_items)} active item(s) have no live claim "
                    "and need resume, handoff, or status triage."
                ),
                "item_ids": [item["id"] for item in active_unclaimed_items],
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
    active_claims: list[dict],
    active_unclaimed_items: list[dict],
    conflicts: list[dict],
    ready_items: list[dict],
    blocked_items: list[dict],
    stale_items: list[dict],
    dependency_waiting_items: list[dict],
) -> dict:
    if conflicts:
        first = conflicts[0]
        if first["kind"] == "claim-identity":
            return {
                "kind": "resolve-claim-identity",
                "summary": "Resolve ambiguous active claim ownership before resuming or starting new work.",
                "claim_id": first["claim_ids"][0],
                "item_id": first["item_ids"][0],
                "reason": first["summary"],
            }
        if first["kind"] == "claim-expiry":
            return {
                "kind": "refresh-claim",
                "summary": "Heartbeat or hand off the next expiring claim before it lapses.",
                "claim_id": first["claim_ids"][0],
                "item_id": first["item_ids"][0],
                "reason": first["summary"],
            }
        if first["kind"] == "unclaimed-active-work":
            item = active_unclaimed_items[0]
            return {
                "kind": "resume-unclaimed-active-item",
                "summary": f"Resume or triage active item #{item['id']} because it has no live claim.",
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

    if active_claims:
        claim = active_claims[0]
        return {
            "kind": "inspect-active-claim",
            "summary": f"Inspect claimed item #{claim['work_item_id']} before starting new work.",
            "claim_id": claim["claim_id"],
            "item_id": claim["work_item_id"],
            "reason": "Active claimed work already exists in this sprint.",
        }

    if ready_items:
        item = ready_items[0]
        return {
            "kind": "start-ready-item",
            "summary": f"Start ready item #{item['id']} because it is unblocked and no active claims are open.",
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

    active_unclaimed_items = snapshot["active_unclaimed_items"]
    lines.append(f"Active items without reservations ({len(active_unclaimed_items)}):")
    if active_unclaimed_items:
        for item in active_unclaimed_items:
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


def _build_delta_since_last_handoff(
    *,
    previous_handoff: dict | None,
    items: list[dict],
    all_events: list[dict],
    active_claims: list[dict],
) -> dict:
    previous_handoff_at = previous_handoff["created_at"] if previous_handoff else None
    if previous_handoff_at is None:
        return {
            "previous_handoff_at": None,
            "item_ids_touched": [],
            "event_count": len(all_events),
            "claim_ids_touched": [],
        }

    item_ids_touched = [item["id"] for item in items if item["updated_at"] > previous_handoff_at]
    claim_ids_touched = [
        claim["claim_id"]
        for claim in active_claims
        if (
            (claim.get("created_at") and claim["created_at"] > previous_handoff_at)
            or (claim.get("heartbeat") and claim["heartbeat"] > previous_handoff_at)
        )
    ]
    previous_handoff_id = previous_handoff["id"]
    event_count = sum(1 for event in all_events if event["id"] > previous_handoff_id)
    return {
        "previous_handoff_at": previous_handoff_at,
        "item_ids_touched": item_ids_touched,
        "event_count": event_count,
        "claim_ids_touched": claim_ids_touched,
    }


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
            "expired_claims_purged": result["expired_claims_purged"],
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

    purged = result["expired_claims_purged"]
    if purged:
        click.echo(f"Purged {purged} expired claim(s).")

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

@click.group()
def claim() -> None:
    """Manage agent claims on work items."""


@claim.command("create")
@click.option("--item-id", type=str, required=True, help="Work item ID or repo#id to claim")
@click.option("--actor", "--agent", "actor", required=True, help="Actor identifier")
@click.option(
    "--type", "claim_type",
    default="execute",
    type=click.Choice(["inspect", "execute", "review", "coordinate"]),
    help="Claim type (default: execute)",
)
@click.option("--non-exclusive", is_flag=True, default=False, help="Allow concurrent claims (non-exclusive)")
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="TTL in seconds (default: 300)")
@click.option("--branch", default=None, help="Git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="PR reference (e.g. owner/repo#123)")
@click.option("--runtime-session-id", default=None, help="Runtime session identifier when available")
@click.option("--instance-id", default=None, help="Stable client-process-local instance ID")
@click.option("--hostname", default=None, help="Hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="PID override (defaults to current process)")
@click.option("--coordinate-claim-id", type=int, default=None, help="Coordinator's claim ID (sub-agent use: bypass coordinate claim lock)")
@click.option("--coordinate-claim-token", default=None, help="Coordinator's claim token (required with --coordinate-claim-id)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output the created claim as JSON")
@click.pass_obj
def claim_create(
    obj,
    item_id: str,
    actor,
    claim_type,
    non_exclusive,
    ttl_seconds,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    runtime_session_id,
    instance_id,
    hostname,
    pid,
    coordinate_claim_id,
    coordinate_claim_token,
    as_json,
) -> None:
    """Claim a work item for an actor.

    Sub-agents spawned by a coordinator should pass --coordinate-claim-id and
    --coordinate-claim-token to create an execute/inspect/review claim under
    an active coordinate claim without triggering a conflict error.
    """
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        _served_claim_create(
            config, item_id, actor, claim_type, non_exclusive, ttl_seconds,
            branch, worktree_path, commit_sha, pr_ref, runtime_session_id,
            instance_id, hostname, pid, coordinate_claim_id,
            coordinate_claim_token, as_json,
        )
        return
    store, m = _get_store(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        cid = m.create_claim(
            store,
            work_item_id=item_id,
            agent=actor,
            claim_type=claim_type,
            exclusive=not non_exclusive,
            ttl_seconds=ttl_seconds,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
            coordinate_claim_id=coordinate_claim_id,
            coordinate_claim_token=coordinate_claim_token,
        )
    except (_db.ClaimConflict, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    claim = m.get_claim(store, cid, include_secret=True)
    assert claim is not None
    recovery_path = _write_claim_recovery_record(claim)
    refs = m.list_refs(store, item_id)
    if as_json:
        claim = dict(claim)
        claim["refs"] = refs
        if recovery_path is not None:
            claim["local_recovery"] = {
                "recovery_token_exists": True,
                "recovery_token_path": str(recovery_path),
            }
        click.echo(json.dumps(claim, indent=2))
        return
    click.echo(f"Claim #{cid} created: {actor} → item #{item_id} ({claim_type}, ttl={ttl_seconds}s)")
    click.echo(f"Claim token: {claim['claim_token']}")
    if recovery_path is not None:
        click.echo(f"Recovery token file: {recovery_path}")
    _echo_item_refs(refs, item_id)


def _served_claim_create(
    config,
    item_id: int,
    actor: str,
    claim_type: str,
    non_exclusive: bool,
    ttl_seconds: int,
    branch: str | None,
    worktree_path: str | None,
    commit_sha: str | None,
    pr_ref: str | None,
    runtime_session_id: str | None,
    instance_id: str | None,
    hostname: str | None,
    pid: int | None,
    coordinate_claim_id: int | None,
    coordinate_claim_token: str | None,
    as_json: bool,
) -> None:
    """Create any claim type through the existing claim arbitration operation."""
    context = _resolved_context(config)
    if (coordinate_claim_id is None) != (coordinate_claim_token is None):
        click.echo(
            "Error: --coordinate-claim-id and --coordinate-claim-token must be supplied together",
            err=True,
        )
        sys.exit(1)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    item_result = _run_served(
        "claim create", _served.read_item, config.served_profile,
        repo_id=config.repo_id, item_id=item_id, resolved_context=context,
    )
    item = item_result["item"]
    identity = _run_served(
        "claim create", _served.identity_current, config.served_profile,
        repo_id=config.repo_id, resolved_context=context,
    )
    authenticated_actor = identity["actor"]
    if actor != authenticated_actor:
        click.echo(
            f"Note: served mode claims as the authenticated identity "
            f"({authenticated_actor}); --actor {actor!r} was not sent and is ignored.",
            err=True,
        )
    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    pending = _find_pending_served_claim_acquire_record(
        rollout_paths.outbox_path,
        item_id=item_id,
        aggregate_uuid=item["aggregate_uuid"],
    )
    credentials: dict[str, str]
    if pending is not None:
        request = _contracts.record_from_dict(pending.payload)
        assert isinstance(request, _contracts.AuthorityCommand)
        try:
            saved = _authority_config.load_pending_authority_credential(
                rollout_paths, event_id=pending.event_id
            )
        except _authority_config.AuthorityCommandConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        if saved is None:
            raise click.ClickException(
                f"pending claim.acquire {pending.event_id} has no private credential sidecar"
            )
        credentials = dict(saved.credentials)
        durable = pending
    else:
        proposed_token = secrets.token_urlsafe(24)
        proposed_ref = _authority.credential_ref(proposed_token)
        credentials = {proposed_ref: proposed_token}
        metadata = {
            key: value for key, value in {
                "runtime_session_id": runtime_session_id,
                "instance_id": instance_id,
                "branch": branch,
                "worktree_path": worktree_path,
                "commit_sha": commit_sha,
                "pr_ref": pr_ref,
                "hostname": hostname,
                "pid": pid,
            }.items() if value is not None
        }
        payload: dict[str, object] = {
            "agent": authenticated_actor,
            "claim_type": claim_type,
            "exclusive": not non_exclusive,
            "ttl_seconds": ttl_seconds,
            "credential_ref": proposed_ref,
            "metadata": metadata,
        }
        if coordinate_claim_id is not None:
            assert coordinate_claim_token is not None
            coordinate_ref = _authority.credential_ref(coordinate_claim_token)
            payload["coordinate_claim_id"] = coordinate_claim_id
            payload["coordinate_credential_ref"] = coordinate_ref
            credentials[coordinate_ref] = coordinate_claim_token
        try:
            durable = _mint_authority_command_record(
                record_type="claim.acquire",
                actor=authenticated_actor,
                refs={
                    "repo_id": _authority_repo_uuid(rollout_paths.repo_root),
                    "aggregate_type": "item",
                    "aggregate_uuid": item["aggregate_uuid"],
                    "aggregate_id": item_id,
                },
                payload=payload,
                basis_revision=_authority.item_revision(item),
                outbox_path=rollout_paths.outbox_path,
            )
        except (TypeError, ValueError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        request = _contracts.record_from_dict(durable.payload)
        assert isinstance(request, _contracts.AuthorityCommand)
        _authority_config.store_pending_authority_credentials(
            rollout_paths,
            event_id=durable.event_id,
            credentials=credentials,
            recovery_credential_ref=request.payload["credential_ref"],
        )
    decision = _run_served(
        "claim create", _served.claim_arbitrate, config.served_profile,
        repo_id=config.repo_id, record=_served_record_argument(durable),
        transient_credentials=credentials, resolved_context=context,
    )
    if decision["outcome"] != "accepted":
        _authority_config.mark_terminal_authority_decision(
            rollout_paths, event_id=durable.event_id, outcome=decision["outcome"]
        )
        _authority_config.remove_pending_authority_credential(
            rollout_paths, event_id=durable.event_id
        )
        click.echo(
            f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}\n"
            f"{_render_resolved_context(context)}", err=True,
        )
        sys.exit(1)
    effect = dict(decision["effect"])
    proposed_ref = request.payload["credential_ref"]
    claim_token = credentials[proposed_ref]
    claim = _served_claim_recovery_projection(
        effect,
        item_id=item_id,
        actor=authenticated_actor,
        claim_type=str(request.payload["claim_type"]),
        claim_token=claim_token,
    )
    if claim is not None:
        claim = {
            **claim,
            "runtime_session_id": claim.get("runtime_session_id", request.payload["metadata"].get("runtime_session_id")),
            "instance_id": claim.get("instance_id", request.payload["metadata"].get("instance_id")),
        }
    recovery_path = _write_claim_recovery_record(claim) if claim is not None else None
    if recovery_path is None:
        click.echo(
            "Error: claim acquisition was accepted but its local recovery proof "
            f"could not be persisted. Immutable request {durable.event_id} remains "
            "pending with private recovery credentials; retry this exact claim create "
            "command to recover the accepted result without minting another claim.",
            err=True,
        )
        sys.exit(1)
    _authority_config.mark_terminal_authority_decision(
        rollout_paths, event_id=durable.event_id, outcome=decision["outcome"]
    )
    _authority_config.remove_pending_authority_credential(
        rollout_paths, event_id=durable.event_id
    )
    refs = item_result.get("refs", [])
    claim["refs"] = refs
    claim["local_recovery"] = {
        "recovery_token_exists": recovery_path is not None,
        "recovery_token_path": str(recovery_path) if recovery_path is not None else None,
    }
    if as_json:
        click.echo(json.dumps(claim, indent=2))
        return
    click.echo(
        f"Claim #{claim['claim_id']} created: {authenticated_actor} → item #{item_id} "
        f"({claim_type}, ttl={ttl_seconds}s)"
    )
    click.echo(f"Claim token: {claim_token}")
    if recovery_path is not None:
        click.echo(f"Recovery token file: {recovery_path}")
    _echo_item_refs(refs, item_id)
    click.echo(_render_resolved_context(context))


@claim.command("start")
@click.option("--item-id", type=str, required=True, help="Work item ID or repo#id to claim and move to active")
@click.option("--actor", "--agent", "actor", required=True, help="Actor identifier")
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="TTL in seconds (default: 300)")
@click.option("--branch", default=None, help="Git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="PR reference (e.g. owner/repo#123)")
@click.option("--runtime-session-id", default=None, help="Runtime session identifier when available")
@click.option("--instance-id", default=None, help="Stable client-process-local instance ID")
@click.option("--hostname", default=None, help="Hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="PID override (defaults to current process)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output the created claim and status transition as JSON")
@click.pass_obj
def claim_start(
    obj,
    item_id: str,
    actor,
    ttl_seconds,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    runtime_session_id,
    instance_id,
    hostname,
    pid,
    as_json,
) -> None:
    """Create an execute claim and move the item to active in one flow.

    If activating the item fails after claim creation, sprintctl attempts to
    release the new claim automatically to avoid leaving accidental ownership.
    """
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        context = _resolved_context(config)
        runtime_session_id = _detect_runtime_session_id(runtime_session_id)
        instance_id = _detect_instance_id(instance_id)
        hostname = _detect_hostname(hostname)
        pid = _detect_pid(pid)
        result = _run_served(
            "claim start",
            _served.claim_start,
            config.served_profile,
            repo_id=config.repo_id,
            item_id=item_id,
            ttl_seconds=ttl_seconds,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
            resolved_context=context,
        )
        claim = result["claim"]
        # work.claim.start's catalog contract has no actor/agent input field:
        # the claim's owning actor is the authenticated identity the server
        # resolves from the credential, not the --actor value below.
        served_actor = claim.get("actor")
        if served_actor is not None and served_actor != actor:
            click.echo(
                f"Note: served mode claims as the authenticated identity "
                f"({served_actor}); --actor {actor!r} was not sent and is ignored.",
                err=True,
            )
        cid = result["claim_id"]
        # Served and local modes both persist a recovery sidecar so
        # ``claim recover`` can restore the token after context loss.
        recovery_path = _write_claim_recovery_record(claim)
        if as_json:
            click.echo(json.dumps({
                "operation": result["operation"],
                "claim_id": cid,
                "claim_token": result["claim_token"],
                "claim": claim,
                "local_recovery": {
                    "recovery_token_exists": recovery_path is not None,
                    "recovery_token_path": str(recovery_path) if recovery_path is not None else None,
                },
                "item_id": result["item_id"],
                "item_status_before": result["item_status_before"],
                "item_status_after": result["item_status_after"],
                "status_transition_applied": result["status_transition_applied"],
                "refs": result["refs"],
            }, indent=2))
            return

        click.echo(f"Claim #{cid} created: {served_actor} → item #{item_id} (execute, ttl={ttl_seconds}s)")
        if result["status_transition_applied"]:
            click.echo(
                f"Item #{item_id} status: {result['item_status_before']} -> {result['item_status_after']}"
            )
        else:
            click.echo(f"Item #{item_id} already active; status unchanged.")
        click.echo(f"Claim token: {result['claim_token']}")
        if recovery_path is not None:
            click.echo(f"Recovery token file: {recovery_path}")
        _echo_item_refs(result["refs"], item_id)
        click.echo(_render_resolved_context(context))
        return

    store, m = _get_store(obj)
    item = m.get_work_item(store, item_id)
    if item is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)
    previous_status = item["status"]
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        cid = m.create_claim(
            store,
            work_item_id=item_id,
            agent=actor,
            claim_type="execute",
            exclusive=True,
            ttl_seconds=ttl_seconds,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
        )
    except (_db.ClaimConflict, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    claim = m.get_claim(store, cid, include_secret=True)
    assert claim is not None
    recovery_path = _write_claim_recovery_record(claim)

    transitioned = False
    transition_error = None
    if previous_status != "active":
        try:
            m.set_work_item_status(
                store,
                item_id,
                "active",
                actor=actor,
                claim_id=cid,
                claim_token=claim["claim_token"],
            )
            transitioned = True
        except Exception as e:
            transition_error = e

    if transition_error is not None:
        release_note = ""
        try:
            m.release_claim(store, cid, claim["claim_token"], actor=actor)
            _remove_claim_recovery_record(cid)
            release_note = f" Claim #{cid} was released."
        except ValueError as release_error:
            release_note = f" Automatic release failed: {release_error}"
        click.echo(
            f"Error: claim #{cid} was created but item #{item_id} could not be moved to active: "
            f"{transition_error}.{release_note}",
            err=True,
        )
        sys.exit(1)

    updated_item = m.get_work_item(store, item_id)
    assert updated_item is not None
    refs = m.list_refs(store, item_id)
    if as_json:
        click.echo(json.dumps({
            "operation": "claim_start",
            "claim_id": claim["claim_id"],
            "claim_token": claim["claim_token"],
            "claim": claim,
            "local_recovery": {
                "recovery_token_exists": recovery_path is not None,
                "recovery_token_path": str(recovery_path) if recovery_path is not None else None,
            },
            "item_id": item_id,
            "item_status_before": previous_status,
            "item_status_after": updated_item["status"],
            "status_transition_applied": transitioned,
            "refs": refs,
        }, indent=2))
        return

    click.echo(f"Claim #{cid} created: {actor} → item #{item_id} (execute, ttl={ttl_seconds}s)")
    if transitioned:
        click.echo(f"Item #{item_id} status: {previous_status} -> {updated_item['status']}")
    else:
        click.echo(f"Item #{item_id} already active; status unchanged.")
    click.echo(f"Claim token: {claim['claim_token']}")
    if recovery_path is not None:
        click.echo(f"Recovery token file: {recovery_path}")
    _echo_item_refs(refs, item_id)


def _served_claim_heartbeat(
    config,
    claim_id,
    claim_token,
    actor,
    ttl_seconds,
    warn_before_expiry,
    runtime_session_id,
    instance_id,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    hostname,
    pid,
    as_json,
) -> None:
    """Served-mode ``claim heartbeat``: mints a ``claim.renew`` authority
    command, carries its proof over the ``invocation/v2`` transient-
    credential channel (never a catalog argument), and arbitrates it via
    ``work.claim.arbitrate``.

    Per "Approved authority-context contract" in the claim-proof transport
    clarification, ``work.claim.context`` supplies the authenticated actor,
    authority repo UUID, and current claim revision this needs to construct
    a canonical ``AuthorityCommand`` without database access. Like
    ``claim_start``, the minted record's actor is always that authenticated
    identity, never an advisory ``--actor`` override (the server rejects an
    actor mismatch downstream anyway, per ``_validate_record`` in
    ``application.py``).
    """
    resolved_context = _resolved_context(config)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)

    context = _run_served(
        "claim heartbeat", _served.claim_context, config.served_profile, repo_id=config.repo_id, claim_id=claim_id,
        resolved_context=resolved_context,
    )
    authenticated_actor = context["actor"]
    if actor is not None and actor != authenticated_actor:
        click.echo(
            f"Note: served mode claims as the authenticated identity "
            f"({authenticated_actor}); --actor {actor!r} was not sent and is ignored.",
            err=True,
        )

    # Same credential_ref/credentials-map shape ``authority submit`` builds
    # from a claim token -- see its ``if claim_token is not None:`` branch.
    ref = _authority.credential_ref(claim_token)
    credentials = {ref: claim_token}
    metadata = {
        key: value
        for key, value in {
            "runtime_session_id": runtime_session_id,
            "instance_id": instance_id,
            "branch": branch,
            "worktree_path": worktree_path,
            "commit_sha": commit_sha,
            "pr_ref": pr_ref,
            "hostname": hostname,
            "pid": pid,
        }.items()
        if value is not None
    }
    payload: dict[str, object] = {
        "claim_id": claim_id,
        "ttl_seconds": ttl_seconds,
        "credential_ref": ref,
    }
    if metadata:
        payload["metadata"] = metadata

    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    authority_repo_uuid = _served_claim_authority_repo_uuid(
        context, rollout_paths.repo_root
    )
    try:
        durable = _mint_authority_command_record(
            record_type="claim.renew",
            actor=authenticated_actor,
            refs={
                "repo_id": authority_repo_uuid,
                "aggregate_type": "claim",
                "aggregate_id": claim_id,
                "claim_id": claim_id,
            },
            payload=payload,
            basis_revision=context["claim_revision"],
            outbox_path=rollout_paths.outbox_path,
        )
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Written before the served invocation below so an unknown/transport
    # outcome leaves retry material for the identical durable record --
    # mirrors ``authority submit``'s enforce-mode sequencing.
    _authority_config.store_pending_authority_credentials(
        rollout_paths,
        event_id=durable.event_id,
        credentials=credentials,
        recovery_credential_ref=None,
    )

    decision = _run_served(
        "claim heartbeat",
        _served.claim_arbitrate,
        config.served_profile,
        repo_id=config.repo_id,
        record=_served_record_argument(durable),
        transient_credentials=credentials,
        resolved_context=resolved_context,
    )
    # A resolved decision (accepted or rejected) is terminal either way, so
    # the retry sidecar is cleared now; an exception from the call above
    # would have exited via _run_served before reaching this line, leaving
    # the sidecar in place for a retry.
    _authority_config.remove_pending_authority_credential(
        rollout_paths, event_id=durable.event_id
    )
    if decision["outcome"] != "accepted":
        click.echo(
            f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}\n"
            f"{_render_resolved_context(resolved_context)}",
            err=True,
        )
        sys.exit(1)

    # decision["effect"] is _claim_effect(...)'s post-update row (claim_id,
    # work_item_id, actor, claim_type, exclusive, heartbeat, expires_at,
    # status, lease_epoch, runtime_session_id, instance_id) -- a smaller
    # shape than the full non-served ``m.get_claim(...)`` dict (no
    # branch/worktree_path/commit_sha/pr_ref/hostname/pid/identity/
    # ownership_proof fields; served mode never fetches those non-secret-but-
    # unnecessary extras with a second round trip just for cosmetic parity).
    # The wording, the fields actually referenced by the text output
    # (``expires_at``), and ``--warn-before-expiry`` behavior match the
    # non-served command exactly.
    refreshed = dict(decision["effect"])
    if as_json:
        refreshed["heartbeat_ttl_seconds"] = ttl_seconds
        click.echo(json.dumps(refreshed, indent=2))
        return
    click.echo(
        f"Claim #{claim_id} heartbeat refreshed (ttl={ttl_seconds}s, expires={refreshed['expires_at']})"
    )
    if warn_before_expiry > 0 and ttl_seconds <= warn_before_expiry:
        click.echo(
            f"Warning: claim #{claim_id} expires in {ttl_seconds}s which is within "
            f"the --warn-before-expiry window ({warn_before_expiry}s). "
            "Consider increasing --ttl or heartbeating more frequently.",
            err=True,
        )
    click.echo(_render_resolved_context(resolved_context))


@claim.command("heartbeat")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=True, help="Claim token returned when the claim was created")
@click.option("--actor", "--agent", "actor", default=None, help="Actor identifier (advisory metadata only)")
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="Refresh TTL in seconds (default: 300)")
@click.option(
    "--warn-before-expiry", "warn_before_expiry", type=int, default=60,
    help="Emit a warning if the refreshed claim expires within N seconds (default: 60). Set 0 to disable.",
)
@click.option("--runtime-session-id", default=None, help="Runtime session identifier when available")
@click.option("--instance-id", default=None, help="Stable client-process-local instance ID")
@click.option("--branch", default=None, help="Git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="PR reference (e.g. owner/repo#123)")
@click.option("--hostname", default=None, help="Hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="PID override (defaults to current process)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output refreshed claim state as JSON")
@click.pass_obj
def claim_heartbeat(
    obj,
    claim_id,
    claim_token,
    actor,
    ttl_seconds,
    warn_before_expiry,
    runtime_session_id,
    instance_id,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    hostname,
    pid,
    as_json,
) -> None:
    """Refresh the TTL on an existing claim."""
    config = _served_config_or_none(obj)
    if config is not None:
        _served_claim_heartbeat(
            config,
            claim_id,
            claim_token,
            actor,
            ttl_seconds,
            warn_before_expiry,
            runtime_session_id,
            instance_id,
            branch,
            worktree_path,
            commit_sha,
            pr_ref,
            hostname,
            pid,
            as_json,
        )
        return
    store, m = _get_store(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        m.heartbeat_claim(
            store,
            claim_id,
            claim_token,
            ttl_seconds=ttl_seconds,
            actor=actor,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    refreshed = m.get_claim(store, claim_id)
    assert refreshed is not None
    if as_json:
        refreshed["heartbeat_ttl_seconds"] = ttl_seconds
        click.echo(json.dumps(refreshed, indent=2))
        return
    click.echo(f"Claim #{claim_id} heartbeat refreshed (ttl={ttl_seconds}s, expires={refreshed['expires_at']})")
    if warn_before_expiry > 0 and ttl_seconds <= warn_before_expiry:
        click.echo(
            f"Warning: claim #{claim_id} expires in {ttl_seconds}s which is within "
            f"the --warn-before-expiry window ({warn_before_expiry}s). "
            "Consider increasing --ttl or heartbeating more frequently.",
            err=True,
        )


def _served_claim_release(config, claim_id, claim_token, actor) -> None:
    """Served-mode ``claim release``: mints a ``claim.release`` authority
    command, carries its proof over the ``invocation/v2`` transient-
    credential channel, and arbitrates it via ``work.claim.arbitrate``.

    See :func:`_served_claim_heartbeat` for the shared context-read /
    proof-reference / sidecar / mint / arbitrate / cleanup sequence this
    mirrors; release's authority-command payload needs only ``claim_id`` and
    ``credential_ref`` (``_handle_claim_mutation``'s ``claim.release`` branch
    in ``authority.py`` reads nothing else from the payload).
    """
    resolved_context = _resolved_context(config)
    context = _run_served(
        "claim release", _served.claim_context, config.served_profile, repo_id=config.repo_id, claim_id=claim_id,
        resolved_context=resolved_context,
    )
    authenticated_actor = context["actor"]
    if actor is not None and actor != authenticated_actor:
        click.echo(
            f"Note: served mode claims as the authenticated identity "
            f"({authenticated_actor}); --actor {actor!r} was not sent and is ignored.",
            err=True,
        )

    ref = _authority.credential_ref(claim_token)
    credentials = {ref: claim_token}
    payload = {"claim_id": claim_id, "credential_ref": ref}

    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    authority_repo_uuid = _served_claim_authority_repo_uuid(
        context, rollout_paths.repo_root
    )
    try:
        durable = _mint_authority_command_record(
            record_type="claim.release",
            actor=authenticated_actor,
            refs={
                "repo_id": authority_repo_uuid,
                "aggregate_type": "claim",
                "aggregate_id": claim_id,
                "claim_id": claim_id,
            },
            payload=payload,
            basis_revision=context["claim_revision"],
            outbox_path=rollout_paths.outbox_path,
        )
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    _authority_config.store_pending_authority_credentials(
        rollout_paths,
        event_id=durable.event_id,
        credentials=credentials,
        recovery_credential_ref=None,
    )

    decision = _run_served(
        "claim release",
        _served.claim_arbitrate,
        config.served_profile,
        repo_id=config.repo_id,
        record=_served_record_argument(durable),
        transient_credentials=credentials,
        resolved_context=resolved_context,
    )
    _authority_config.remove_pending_authority_credential(
        rollout_paths, event_id=durable.event_id
    )
    if decision["outcome"] != "accepted":
        click.echo(
            f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}\n"
            f"{_render_resolved_context(resolved_context)}",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Claim #{claim_id} released.")
    click.echo(_render_resolved_context(resolved_context))


@claim.command("release")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=True, help="Claim token returned when the claim was created")
@click.option("--actor", "--agent", "actor", default=None, help="Actor identifier (advisory metadata only)")
@click.pass_obj
def claim_release(obj, claim_id, claim_token, actor) -> None:
    """Release (delete) a claim."""
    config = _served_config_or_none(obj)
    if config is not None:
        _served_claim_release(config, claim_id, claim_token, actor)
        return
    store, m = _get_store(obj)
    try:
        m.release_claim(store, claim_id, claim_token, actor=actor)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    _remove_claim_recovery_record(claim_id)
    click.echo(f"Claim #{claim_id} released.")


def _served_claim_handoff(
    config,
    claim_id,
    claim_token,
    actor,
    mode,
    ttl_seconds,
    runtime_session_id,
    instance_id,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    hostname,
    pid,
    performed_by,
    note,
    allow_legacy_adopt,
    output_path,
    as_json,
) -> None:
    """Served-mode ``claim handoff``: mints a ``claim.handoff`` authority
    command, carries the current (and, for rotate mode, a freshly minted
    proposed) claim proof over the ``invocation/v2`` transient-credential
    channel, and arbitrates it via ``work.claim.arbitrate``.

    See :func:`_served_claim_heartbeat` for the shared context-read / sidecar
    / mint / arbitrate / cleanup sequence this mirrors. Handoff differs from
    heartbeat/release in three ways (#1195 Group A, Build A3 scope
    decisions):

    * ``--allow-legacy-adopt`` has no served-mode equivalent. The legacy-
      ambiguous-claim concept it exists for -- a claim row with no
      ``claim_token`` at all -- is a local-sqlite/legacy-remote artifact with
      no evidence the served backend's claim rows can ever be in that state,
      and there is no local ambiguity-detection event to fall back on here.
      Rather than guess server behavior, this rejects explicitly. Because
      served mode has no such adoption escape hatch, ``--claim-token`` is
      effectively required in served mode.
    * ``--actor`` here is the *recipient* identifier (becomes
      ``payload["to_actor"]``), never the authenticated identity -- do not
      confuse it with ``context["actor"]``, which (like heartbeat/release)
      is always who *performed* the handoff (``envelope.actor``).
    * Rotate mode (the default) must mint the new claim token client-side --
      the server never invents one, see ``_handle_claim_mutation``'s
      ``claim.handoff`` branch in authority.py -- and carry *two* transient
      credential bindings in one map: the current token's ref (proving
      current ownership) and the newly minted token's ref
      (``proposed_credential_ref`` in the payload), so the server learns the
      new secret without it ever appearing in the payload itself. Transfer
      mode leaves the token unchanged and needs only the current ref.
    """
    resolved_context = _resolved_context(config)
    if allow_legacy_adopt:
        click.echo(
            "Error: --allow-legacy-adopt is not supported in served mode\n"
            f"{_render_resolved_context(resolved_context)}", err=True
        )
        sys.exit(1)
    if claim_token is None:
        click.echo(
            "Error: --claim-token is required in served mode "
            "(there is no legacy-adoption fallback)\n"
            f"{_render_resolved_context(resolved_context)}",
            err=True,
        )
        sys.exit(1)

    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)

    context = _run_served(
        "claim handoff", _served.claim_context, config.served_profile, repo_id=config.repo_id, claim_id=claim_id,
        resolved_context=resolved_context,
    )
    authenticated_actor = context["actor"]
    if performed_by is not None and performed_by != authenticated_actor:
        click.echo(
            f"Note: served mode records the authenticated identity "
            f"({authenticated_actor}) as who performed the handoff; "
            f"--performed-by {performed_by!r} was not sent and is ignored.",
            err=True,
        )

    ref = _authority.credential_ref(claim_token)
    credentials = {ref: claim_token}
    new_token = claim_token
    metadata = {
        key: value
        for key, value in {
            "runtime_session_id": runtime_session_id,
            "instance_id": instance_id,
            "branch": branch,
            "worktree_path": worktree_path,
            "commit_sha": commit_sha,
            "pr_ref": pr_ref,
            "hostname": hostname,
            "pid": pid,
        }.items()
        if value is not None
    }
    payload: dict[str, object] = {
        "claim_id": claim_id,
        "to_actor": actor,
        "mode": mode,
        "ttl_seconds": ttl_seconds,
        "credential_ref": ref,
    }
    if mode == "rotate":
        # The server never invents the new token (authority.py's claim.handoff
        # branch only ever reads it back out of the transient credentials map
        # via ``proposed_credential_ref``) -- matches
        # ``db.py::_generate_claim_token``'s technique exactly.
        new_token = secrets.token_urlsafe(24)
        proposed_ref = _authority.credential_ref(new_token)
        credentials[proposed_ref] = new_token
        payload["proposed_credential_ref"] = proposed_ref
    if metadata:
        payload["metadata"] = metadata
    if note is not None:
        payload["note"] = note

    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    authority_repo_uuid = _served_claim_authority_repo_uuid(
        context, rollout_paths.repo_root
    )
    try:
        durable = _mint_authority_command_record(
            record_type="claim.handoff",
            actor=authenticated_actor,
            refs={
                "repo_id": authority_repo_uuid,
                "aggregate_type": "claim",
                "aggregate_id": claim_id,
                "claim_id": claim_id,
            },
            payload=payload,
            basis_revision=context["claim_revision"],
            outbox_path=rollout_paths.outbox_path,
        )
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Written before the served invocation below so an unknown/transport
    # outcome leaves retry material for the identical durable record -- both
    # credential bindings (current +, for rotate, proposed) are captured here
    # since the server needs both in the same transient_credentials map.
    _authority_config.store_pending_authority_credentials(
        rollout_paths,
        event_id=durable.event_id,
        credentials=credentials,
        recovery_credential_ref=None,
    )

    decision = _run_served(
        "claim handoff",
        _served.claim_arbitrate,
        config.served_profile,
        repo_id=config.repo_id,
        record=_served_record_argument(durable),
        transient_credentials=credentials,
        resolved_context=resolved_context,
    )
    # Unlike ``authority submit``'s claim.handoff-rotate special case (which
    # retains the sidecar after an accepted decision so the new token can be
    # recovered later via ``authority recover-proof``, because that generic
    # command never echoes the secret in its own output), this command
    # already holds ``new_token`` in local memory and echoes it directly
    # below -- so, exactly like heartbeat/release, any resolved (accepted or
    # rejected) decision clears the sidecar now; only an exception from the
    # call above (which exits via _run_served before reaching this line)
    # leaves it in place for a retry.
    _authority_config.remove_pending_authority_credential(
        rollout_paths, event_id=durable.event_id
    )
    if decision["outcome"] != "accepted":
        click.echo(
            f"Error: {decision.get('reason_code')}: {decision.get('reason_detail')}\n"
            f"{_render_resolved_context(resolved_context)}",
            err=True,
        )
        sys.exit(1)

    # decision["effect"] is _claim_effect(...)'s post-update row -- never
    # carries claim_token (see the heartbeat helper's comment on that shape),
    # so the token to report is whatever this command itself used or minted
    # above.
    effect = dict(decision["effect"])
    # The handoff itself is already accepted and durable at this point (the
    # sidecar above is cleared), so a failure fetching item details for the
    # bundle must not be reported as a handoff failure via _run_served's
    # sys.exit(1) -- that would tell the caller a successful mutation failed,
    # and worse, would look retryable when the current claim proof is already
    # invalidated. Degrade to a smaller bundle instead.
    try:
        item_payload = _served.read_item(
            config.served_profile,
            repo_id=config.repo_id,
            item_id=effect["work_item_id"],
        )
        item = item_payload.get("item")
    except Exception as exc:  # noqa: BLE001 - degrade, don't fail an already-accepted handoff
        click.echo(
            f"Warning: claim #{claim_id} handoff succeeded, but fetching item "
            f"details for the bundle failed: {exc}",
            err=True,
        )
        item = None
    bundle = {
        "bundle_type": "claim_handoff",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "claim": {**effect, "claim_token": new_token},
        "item": item,
        # served mode has no single-sprint read operation (only the list-
        # returning work.read.sprints, work.read.item's sibling); rather than
        # fetch and filter the full sprint list on every handoff just for a
        # cosmetic parity field, this reports the item's sprint_id alone --
        # a smaller shape than the local bundle's full "sprint" object
        # (#1195 Build A3 scope decision, in the same spirit as the
        # documented heartbeat effect-shape gap).
        "sprint_id": item.get("sprint_id") if item else None,
        "performed_by": authenticated_actor,
    }

    if output_path and output_path != "-":
        with open(output_path, "w") as fh:
            json.dump(bundle, fh, indent=2)
        click.echo(f"Claim handoff bundle written to {output_path}")
        if not as_json:
            click.echo(f"Claim #{claim_id} handed off to {actor} (mode={mode})")
            click.echo(f"Claim token: {new_token}")
            click.echo(_render_resolved_context(resolved_context))
        return

    if as_json or output_path == "-":
        click.echo(json.dumps(bundle, indent=2))
        return

    click.echo(f"Claim #{claim_id} handed off to {actor} (mode={mode})")
    click.echo(f"Claim token: {new_token}")
    click.echo(_render_resolved_context(resolved_context))


@claim.command("handoff")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", default=None, help="Existing claim token (required unless explicitly adopting a lost or legacy proof)")
@click.option("--actor", "--agent", "actor", required=True, help="Recipient actor identifier")
@click.option(
    "--mode",
    default="rotate",
    type=click.Choice(["transfer", "rotate"]),
    help="Transfer keeps the token; rotate mints a new one (default: rotate)",
)
@click.option("--ttl", "ttl_seconds", default=300, type=int, help="Refresh TTL in seconds after handoff (default: 300)")
@click.option("--runtime-session-id", default=None, help="Recipient runtime session identifier")
@click.option("--instance-id", default=None, help="Recipient client-process-local instance ID")
@click.option("--branch", default=None, help="Recipient git branch name")
@click.option("--worktree", "worktree_path", default=None, help="Recipient worktree path")
@click.option("--commit-sha", "commit_sha", default=None, help="Recipient commit SHA")
@click.option("--pr-ref", "pr_ref", default=None, help="Recipient PR reference (e.g. owner/repo#123)")
@click.option("--hostname", default=None, help="Recipient hostname override (defaults to current host)")
@click.option("--pid", type=int, default=None, help="Recipient PID override (defaults to current process)")
@click.option("--performed-by", default=None, help="Actor performing the handoff")
@click.option("--note", default=None, help="Structured note to include in the handoff event")
@click.option("--allow-legacy-adopt", is_flag=True, default=False, help="Explicitly adopt a lost or legacy claim proof and mint a fresh token")
@click.option("--output", "output_path", default=None, help="Write the claim handoff bundle to a file instead of stdout")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the claim handoff bundle as JSON")
@click.pass_obj
def claim_handoff(
    obj,
    claim_id,
    claim_token,
    actor,
    mode,
    ttl_seconds,
    runtime_session_id,
    instance_id,
    branch,
    worktree_path,
    commit_sha,
    pr_ref,
    hostname,
    pid,
    performed_by,
    note,
    allow_legacy_adopt,
    output_path,
    as_json,
) -> None:
    """Explicitly transfer or rotate claim ownership and emit a claim handoff bundle."""
    config = _served_config_or_none(obj)
    if config is not None:
        _served_claim_handoff(
            config,
            claim_id,
            claim_token,
            actor,
            mode,
            ttl_seconds,
            runtime_session_id,
            instance_id,
            branch,
            worktree_path,
            commit_sha,
            pr_ref,
            hostname,
            pid,
            performed_by,
            note,
            allow_legacy_adopt,
            output_path,
            as_json,
        )
        return
    store, m = _get_store(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = _detect_instance_id(instance_id)
    hostname = _detect_hostname(hostname)
    pid = _detect_pid(pid)
    try:
        claim = m.handoff_claim(
            store,
            claim_id,
            claim_token,
            actor=actor,
            mode=mode,
            ttl_seconds=ttl_seconds,
            runtime_session_id=runtime_session_id,
            instance_id=instance_id,
            branch=branch,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            pr_ref=pr_ref,
            hostname=hostname,
            pid=pid,
            performed_by=performed_by,
            note=note,
            allow_legacy_adopt=allow_legacy_adopt,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    recovery_path = _write_claim_recovery_record(claim)

    item = m.get_work_item(store, claim["work_item_id"])
    sprint = m.get_sprint(store, item["sprint_id"]) if item else None
    bundle = {
        "bundle_type": "claim_handoff",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "claim": claim,
        "item": item,
        "sprint": sprint,
        "performed_by": performed_by or actor,
    }

    if output_path and output_path != "-":
        with open(output_path, "w") as fh:
            json.dump(bundle, fh, indent=2)
        click.echo(f"Claim handoff bundle written to {output_path}")
        if not as_json:
            click.echo(f"Claim #{claim_id} handed off to {actor} (mode={mode})")
            click.echo(f"Claim token: {claim['claim_token']}")
            if recovery_path is not None:
                click.echo(f"Recovery token file: {recovery_path}")
        return

    if as_json or output_path == "-":
        click.echo(json.dumps(bundle, indent=2))
        return

    click.echo(f"Claim #{claim_id} handed off to {actor} (mode={mode})")
    click.echo(f"Claim token: {claim['claim_token']}")
    if recovery_path is not None:
        click.echo(f"Recovery token file: {recovery_path}")


@claim.command("list")
@click.option("--item-id", type=str, required=True, help="Work item ID or repo#id")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired claims")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_list(obj, item_id, show_all, as_json) -> None:
    """List claims on a work item."""
    item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        claims = _run_served("claim list", _served.read_claims, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, active_only=not show_all,
            resolved_context=_resolved_context(config))["claims"]
        if as_json: click.echo(json.dumps(claims, indent=2))
        elif not claims: click.echo(f"No {'active ' if not show_all else ''}claims on item #{item_id}.")
        else:
            for c in claims: click.echo(f"#{c['claim_id']}  {c['actor']}  [{c['claim_type']}]  {'exclusive' if c['exclusive'] else 'shared'}  status={c['status']}  epoch={c['lease_epoch']}  proof={c['identity_status']}  expires={c['expires_at']}  heartbeat={c['heartbeat']}")
        return
    store, m = _get_store(obj)
    claims = m.list_claims(store, item_id, active_only=not show_all)
    if as_json:
        click.echo(json.dumps(claims, indent=2))
        return
    if not claims:
        click.echo(f"No {'active ' if not show_all else ''}claims on item #{item_id}.")
        return
    for c in claims:
        excl = "exclusive" if c["exclusive"] else "shared"
        proof = c["identity_status"]
        click.echo(
            f"#{c['claim_id']}  {c['actor']}  [{c['claim_type']}]  {excl}  "
            f"status={c['status']}  epoch={c['lease_epoch']}  proof={proof}  "
            f"expires={c['expires_at']}  heartbeat={c['heartbeat']}"
        )


@claim.command("list-sprint")
@click.option("--sprint-id", type=str, default=None, help="Sprint ID or repo#id (defaults to active)")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired claims")
@click.option(
    "--expiring-within", "expiring_within", type=int, default=None,
    help="Only show claims expiring within N seconds",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_list_sprint(obj, sprint_id, show_all, expiring_within, as_json) -> None:
    """List all claims across a sprint, optionally filtered by expiry window."""
    if sprint_id is not None:
        sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    config = _served_config_or_none(obj)
    if config is not None:
        if expiring_within is not None:
            _served_operation_unavailable("claim list-sprint --expiring-within", replacement="The served catalog has no clock-window claim filter yet.")
        claims = _run_served("claim list-sprint", _served.read_claims, config.served_profile,
            repo_id=config.repo_id, sprint_id=sprint_id, active_only=not show_all,
            resolved_context=_resolved_context(config))["claims"]
        if as_json: click.echo(json.dumps(claims, indent=2))
        elif not claims: click.echo("No claims found.")
        else:
            for c in claims: click.echo(f"#{c['claim_id']}  item #{c['work_item_id']} ({c.get('item_title', '-')})  {c['actor']}  [{c['claim_type']}]  status={c['status']}  expires={c['expires_at']}")
        return
    store, m = _get_store(obj)
    if sprint_id is not None:
        sprint = m.get_sprint(store, sprint_id)
    else:
        sprint = _resolve_implicit_sprint(store, m=m)
    if sprint is None:
        click.echo("No sprint found. Use --sprint-id to specify one.", err=True)
        sys.exit(1)
    claims = m.list_claims_by_sprint(
        store,
        sprint["id"],
        active_only=not show_all,
        expiring_within_seconds=expiring_within,
    )
    if as_json:
        click.echo(json.dumps(claims, indent=2))
        return
    if not claims:
        label = "expiring" if expiring_within is not None else ("active " if not show_all else "")
        click.echo(f"No {label}claims in sprint #{sprint['id']} ({sprint['name']}).")
        return
    click.echo(f"Claims in sprint #{sprint['id']} ({sprint['name']}):")
    for c in claims:
        excl = "exclusive" if c["exclusive"] else "shared"
        click.echo(
            f"  #{c['claim_id']}  item #{c['work_item_id']} ({c['item_title']})  "
            f"{c['actor']}  [{c['claim_type']}]  {excl}  "
            f"status={c['status']}  epoch={c['lease_epoch']}  "
            f"proof={c['identity_status']}  expires={c['expires_at']}"
        )


@claim.command("show")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=False, help="Claim token (required only by the local backend)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_show(obj, claim_id, claim_token, as_json) -> None:
    """Show a claim. Local mode can re-display its token with proof.

    Requires the current claim_token to prove ownership before revealing it again.
    """
    config = _served_config_or_none(obj)
    if config is not None:
        claim = _run_served("claim show", _served.read_claim, config.served_profile,
            repo_id=config.repo_id, claim_id=claim_id, resolved_context=_resolved_context(config))["claim"]
        if as_json:
            click.echo(json.dumps(claim, indent=2))
            return
        click.echo(f"Claim #{claim_id}  actor={claim['actor']}  type={claim['claim_type']}")
        click.echo(f"  status={claim['status']}  lease_epoch={claim['lease_epoch']}  expires={claim['expires_at']}  identity_status={claim['identity_status']}")
        click.echo("  claim_token: unavailable in served reads")
        return
    if claim_token is None:
        click.echo("Error: --claim-token is required outside served mode", err=True)
        sys.exit(1)
    store, m = _get_store(obj)
    claim = m.get_claim(store, claim_id, include_secret=True)
    if claim is None:
        click.echo(f"Error: Claim #{claim_id} not found", err=True)
        sys.exit(1)
    try:
        from ..db import _require_claim_proof
        _require_claim_proof(claim, claim_token)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(claim, indent=2))
        return
    click.echo(f"Claim #{claim_id}  actor={claim['actor']}  type={claim['claim_type']}")
    click.echo(
        f"  status={claim['status']}  lease_epoch={claim['lease_epoch']}  "
        f"expires={claim['expires_at']}  identity_status={claim['identity_status']}"
    )
    click.echo(f"  claim_token: {claim['claim_token']}")


@claim.command("resume")
@click.option("--item-id", type=str, default=None, help="Filter results to a specific work item or repo#id")
@click.option("--instance-id", default=None, help="Your stable instance ID (preferred)")
@click.option("--runtime-session-id", default=None, help="Your runtime session ID")
@click.option("--hostname", default=None, help="Hostname (use with --pid)")
@click.option("--pid", type=int, default=None, help="PID (use with --hostname)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_resume(obj, item_id, instance_id, runtime_session_id, hostname, pid, as_json) -> None:
    """Find active claims matching your agent identity for session resumption.

    Use this when restarting after context loss to locate your existing claims.
    Claims are returned without the token — use 'claim show' with the token once
    recovered, or 'claim handoff --allow-legacy-adopt' to re-mint a fresh proof.
    Provide at least one of: --instance-id, --runtime-session-id, or --hostname + --pid.
    """
    if item_id is not None:
        item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        runtime_session_id = _detect_runtime_session_id(runtime_session_id)
        instance_id = instance_id or os.environ.get("SPRINTCTL_INSTANCE_ID")
        if not any((instance_id, runtime_session_id, hostname and pid)):
            click.echo("Error: provide an identity to resume claims.", err=True); sys.exit(1)
        claims = _run_served("claim resume", _served.read_claims, config.served_profile,
            repo_id=config.repo_id, item_id=item_id, active_only=True, instance_id=instance_id,
            runtime_session_id=runtime_session_id, hostname=hostname, pid=pid,
            resolved_context=_resolved_context(config))["claims"]
        if as_json: click.echo(json.dumps(claims, indent=2))
        elif not claims: click.echo("No active claims found matching the provided identity.")
        else:
            for c in claims: click.echo(f"#{c['claim_id']}  item #{c['work_item_id']}  {c['actor']}  [{c['claim_type']}]  expires={c['expires_at']}  proof={c['identity_status']}")
        return
    store, m = _get_store(obj)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    instance_id = instance_id or os.environ.get("SPRINTCTL_INSTANCE_ID")
    try:
        claims = m.find_claim_by_identity(
            store,
            instance_id=instance_id,
            hostname=hostname,
            pid=pid,
            runtime_session_id=runtime_session_id,
            active_only=True,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if item_id is not None:
        claims = [claim for claim in claims if claim["work_item_id"] == item_id]
    claims = [
        _claim_with_recovery_status(
            claim,
            current_runtime_session_id=runtime_session_id,
            current_instance_id=instance_id,
        )
        for claim in claims
    ]
    if as_json:
        click.echo(json.dumps(claims, indent=2))
        return
    if not claims:
        click.echo("No active claims found matching the provided identity.")
        return
    click.echo(f"Found {len(claims)} active claim(s) matching your identity:")
    for c in claims:
        click.echo(
            f"  #{c['claim_id']}  item #{c['work_item_id']}  {c['actor']}  "
            f"[{c['claim_type']}]  expires={c['expires_at']}  "
            f"proof={c['identity_status']}"
        )
        click.echo(
            f"    local_token={'yes' if c['local_recovery']['recovery_token_exists'] else 'no'}  "
            f"identity_match={'yes' if c['local_recovery']['plausible_identity_match'] else 'no'}"
        )
        click.echo(f"    recovery_path={c['local_recovery']['recovery_token_path']}")
    click.echo("Use 'claim recover --id <id>' or '--item-id <id>' to restore a locally persisted token.")
    click.echo("Use 'claim handoff --allow-legacy-adopt' if the token is lost and the claim has no secret.")


def _served_claim_recover(
    config: _backend.BackendConfig,
    claim_id: int | None,
    item_id: int | None,
    as_json: bool,
) -> None:
    """Served-mode claim recover: validate sidecar identity against the served
    active claim before returning the token. Never opens a local work store."""
    context = _resolved_context(config)

    def require_recoverable_claim(
        claim: dict, *, require_live_expiry: bool
    ) -> None:
        if claim.get("status") != "active":
            click.echo(
                f"Error: Claim #{claim.get('claim_id')} is not active (status={claim.get('status')}).",
                err=True,
            )
            sys.exit(1)
        try:
            expires_at = datetime.fromisoformat(str(claim["expires_at"]).replace("Z", "+00:00"))
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("expiry timezone is required")
        except (KeyError, TypeError, ValueError):
            click.echo(f"Error: Claim #{claim.get('claim_id')} has no valid expiry.", err=True)
            sys.exit(1)
        if require_live_expiry and expires_at <= datetime.now(timezone.utc):
            click.echo(f"Error: Claim #{claim.get('claim_id')} is expired.", err=True)
            sys.exit(1)

    if claim_id is not None:
        result = _run_served(
            "claim recover",
            _served.read_claim,
            config.served_profile,
            repo_id=config.repo_id,
            claim_id=claim_id,
            resolved_context=context,
        )
        claim = (result or {}).get("claim", {})
        if not claim:
            click.echo(f"Error: Claim #{claim_id} not found.", err=True)
            sys.exit(1)
        if claim.get("claim_id") != claim_id:
            click.echo(f"Error: served claim response does not match requested claim #{claim_id}.", err=True)
            sys.exit(1)
        # Explicit identity-bound recovery is also the supported route to
        # proof-bound cleanup after lease expiry. The authority still verifies
        # the recovered proof before accepting claim.release. Broad item
        # discovery below remains live-only.
        require_recoverable_claim(claim, require_live_expiry=False)
        served_claim_id = claim["claim_id"]
    else:
        assert item_id is not None
        result = _run_served(
            "claim recover",
            _served.read_claims,
            config.served_profile,
            repo_id=config.repo_id,
            item_id=item_id,
            active_only=True,
            resolved_context=context,
        )
        claims = (result or {}).get("claims", [])
        if not claims:
            click.echo(
                f"Error: No active claims found for item #{item_id}.", err=True
            )
            sys.exit(1)
        if len(claims) > 1:
            candidates = ", ".join(str(c["claim_id"]) for c in claims)
            click.echo(
                "Error: Multiple active claims found for item "
                f"#{item_id}; rerun with --id. Candidates: {candidates}",
                err=True,
            )
            sys.exit(1)
        claim = claims[0]
        if claim.get("work_item_id") != item_id:
            click.echo(f"Error: served claim response does not match requested item #{item_id}.", err=True)
            sys.exit(1)
        require_recoverable_claim(claim, require_live_expiry=True)
        served_claim_id = claim["claim_id"]

    record = _load_claim_recovery_record(served_claim_id)
    if record is None:
        message = (
            f"No local recovery token file exists for claim #{served_claim_id}. "
            f"Expected {_claim_recovery_path(served_claim_id)}"
        )
        if as_json:
            click.echo(json.dumps(
                {"claim": claim, "claim_token": None, "error": message}, indent=2,
            ))
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    token = record.get("claim_token") if isinstance(record, dict) else None
    if not token or not isinstance(token, str):
        message = (
            "Local recovery token file for claim "
            f"#{served_claim_id} is malformed (missing or empty claim_token)."
        )
        if as_json:
            click.echo(json.dumps(
                {"claim": claim, "claim_token": None, "error": message}, indent=2,
            ))
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    mismatches: list[str] = []
    if record.get("claim_id") != served_claim_id:
        mismatches.append(
            f"claim_id: sidecar={record.get('claim_id')}, served={served_claim_id}"
        )
    if record.get("work_item_id") != claim.get("work_item_id"):
        mismatches.append(
            f"work_item_id: sidecar={record.get('work_item_id')}, "
            f"served={claim.get('work_item_id')}"
        )
    if record.get("actor") != claim.get("actor"):
        mismatches.append(
            f"actor: sidecar={record.get('actor')!r}, "
            f"served={claim.get('actor')!r}"
        )
    if record.get("claim_type") != claim.get("claim_type"):
        mismatches.append(
            f"claim_type: sidecar={record.get('claim_type')!r}, "
            f"served={claim.get('claim_type')!r}"
        )

    if mismatches:
        message = (
            "Identity mismatch between sidecar and served active claim "
            f"for claim #{served_claim_id}: {'; '.join(mismatches)}"
        )
        if as_json:
            click.echo(json.dumps(
                {"claim": claim, "claim_token": None, "error": message}, indent=2,
            ))
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps({"claim": claim, "claim_token": token}, indent=2))
        return

    click.echo(
        f"Claim #{served_claim_id} recovered for item "
        f"#{claim['work_item_id']} ({claim['claim_type']})"
    )
    click.echo(f"Claim token: {token}")
    click.echo(_render_resolved_context(context))


@claim.command("recover")
@click.option("--id", "claim_id", type=int, default=None, help="Claim ID to recover")
@click.option("--item-id", type=str, default=None, help="Recover the only active claim for a work item or repo#id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_recover(obj, claim_id, item_id, as_json) -> None:
    """Recover a claim token from sprintctl's local recovery record."""
    if (claim_id is None) == (item_id is None):
        click.echo("Error: Provide exactly one of --id or --item-id", err=True)
        sys.exit(1)
    if item_id is not None:
        item_id = _apply_scoped_id(obj, item_id, field="item")
    config = _served_config_or_none(obj)
    if config is not None:
        _served_claim_recover(config, claim_id, item_id, as_json)
        return
    try:
        config = _backend.load_backend_config()
    except _backend.BackendConfigError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    if config.mode == "remote":
        click.echo(
            "Error: claim recovery files are local-mode only. "
            "Use pg claim state or an explicit claim token.",
            err=True,
        )
        sys.exit(1)
    conn = _get_conn(obj)
    try:
        claim = _find_recoverable_claim(conn, claim_id=claim_id, item_id=item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    current_runtime_session_id = _detect_runtime_session_id(None)
    current_instance_id = os.environ.get("SPRINTCTL_INSTANCE_ID")
    recovery_status = _claim_recovery_status(
        claim,
        current_runtime_session_id=current_runtime_session_id,
        current_instance_id=current_instance_id,
    )
    record = _load_claim_recovery_record(claim["claim_id"])
    payload = {
        "claim": claim,
        "local_recovery": recovery_status,
        "claim_token": record.get("claim_token") if record else None,
    }
    if record is None:
        message = (
            f"No local recovery token file exists for claim #{claim['claim_id']}. "
            f"Expected {recovery_status['recovery_token_path']}"
        )
        if as_json:
            payload["error"] = message
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Claim #{claim['claim_id']} recovered for item #{claim['work_item_id']} ({claim['claim_type']})")
    click.echo(f"Claim token: {record['claim_token']}")
    click.echo(f"Recovery token file: {recovery_status['recovery_token_path']}")
    click.echo(
        "Identity match: "
        f"runtime_session_id={'yes' if recovery_status['runtime_session_id_matches'] else 'no'}, "
        f"instance_id={'yes' if recovery_status['instance_id_matches'] else 'no'}"
    )


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


def register_claim(root: click.Group, *, runtime: dict[str, object]) -> None:
    """Attach the claim group at its historical insertion point."""
    _register(root, runtime, (claim,))
