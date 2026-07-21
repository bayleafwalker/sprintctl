import json
import os
import re
import secrets
import sqlite3
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

import click

from . import __version__
from . import backend as _backend
from . import authority as _authority
from . import authority_config as _authority_config
from . import context_candidates as _context_candidates
from . import contracts as _contracts
from . import cutover as _cutover
from . import db as _db
from . import doctor as _doctor
from . import dualwrite as _dualwrite
from . import maintain as _maintain
from . import observations as _observations
from . import outbox as _outbox
from . import pilot as _pilot
from . import project as _project
from . import projection as _projection
from . import projection_reads as _projection_reads
from . import shadow as _shadow
from . import sync as _sync
from .render import render_sprint_doc


def _emit_audit_event(
    event_type: str,
    *,
    summary: str,
    refs: list[str],
    metadata: dict,
) -> None:
    """Emit an auditctl event via subprocess. Non-fatal: warns to stderr on failure.

    Uses subprocess (not AuditctlClient) to keep the decoupling boundary —
    sprintctl does not depend on auditctl at import time.
    """
    cmd = [
        "auditctl", "add",
        "--type", event_type,
        "--source", "sprintctl",
        "--actor", os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown",
        "--summary", summary,
        "--metadata", json.dumps(metadata, separators=(",", ":")),
    ]
    for ref in refs:
        cmd.extend(["--ref", ref])
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            click.echo(
                f"warning: auditctl emit failed: {result.stderr.decode(errors='replace').strip()}",
                err=True,
            )
    except Exception as exc:
        click.echo(f"warning: auditctl emit failed: {exc}", err=True)


def _detect_runtime_session_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return (
        os.environ.get("SPRINTCTL_RUNTIME_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
    )


def _detect_instance_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get("SPRINTCTL_INSTANCE_ID") or str(uuid.uuid4())


def _detect_hostname(explicit: str | None) -> str:
    if explicit:
        return explicit
    return socket.gethostname()


def _detect_pid(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    return os.getpid()


@click.group()
@click.version_option(__version__, prog_name="sprintctl")
@click.pass_context
def cli(ctx: click.Context) -> None:
    ctx.ensure_object(dict)
    ctx.obj.setdefault("conn", None)


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit deterministic JSON diagnostics")
def doctor_cmd(as_json: bool) -> None:
    """Diagnose install provenance, extras, backend config, and schema compatibility."""
    report = _doctor.collect_report()
    click.echo(_doctor.dumps(report) if as_json else _doctor.render_text(report))
    if report["status"] == "error":
        raise click.exceptions.Exit(1)


def _get_conn(obj: dict) -> sqlite3.Connection:
    conn = obj.get("conn")
    if conn is None:
        try:
            _backend.require_local_backend()
        except _backend.BackendConfigError as e:
            click.echo(str(e), err=True)
            sys.exit(1)
        db_path = _db.get_db_path()
        conn = _db.get_connection(db_path)
        _db.init_db(conn)
        obj["conn"] = conn
        click.get_current_context().call_on_close(conn.close)
    return conn


def _redacted_postgres_error(exc: Exception, url: str | None) -> str:
    message = str(exc)
    if url:
        message = message.replace(url, "<redacted SPRINTCTL_URL>")
    return re.sub(
        r"(postgres(?:ql)?://)[^\s@]+@",
        r"\1<redacted>@",
        message,
        flags=re.IGNORECASE,
    )


def _get_store(obj: dict):
    """Return (store, db_module) for the configured backend. Exits on error."""
    try:
        config = _backend.load_backend_config()
    except _backend.BackendConfigError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    obj["backend_config"] = config

    if config.mode == "local":
        conn = obj.get("conn")
        if conn is None:
            db_path = _db.get_db_path()
            conn = _db.get_connection(db_path)
            _db.init_db(conn)
            obj["conn"] = conn
            click.get_current_context().call_on_close(conn.close)
        return conn, _db

    # Remote mode — lazy import so psycopg is optional for local-only use
    from . import pg as _pg  # noqa: PLC0415
    store = obj.get("pg_store")
    if store is None:
        try:
            store = _pg.get_connection(config.url)
            from . import pg_migrations as _pg_migrations  # noqa: PLC0415
            obj["remote_compatibility"] = _pg_migrations.startup_schema_handshake(
                store,
                os.environ,
            )
        except Exception as e:
            if store is not None:
                store.conn.close()
            detail = _redacted_postgres_error(e, config.url)
            click.echo(
                f"Error: could not connect to postgres from SPRINTCTL_URL: {detail}",
                err=True,
            )
            sys.exit(1)
        obj["pg_store"] = store
        click.get_current_context().call_on_close(store.conn.close)
    return store, _pg


def _get_project_stores(obj: dict, project_value: str | Path):
    """Return a validated project plus one read-only store per backlog member."""
    try:
        project_path = _project.resolve_project_path(project_value)
        project = _project.load_project(project_path)
    except _project.ProjectConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    store, m = _get_store(obj)
    config = obj["backend_config"]
    members = project.backlog_members
    if config.mode == "local":
        if len(members) != 1:
            raise click.ClickException(
                "multi-repository --project views require the remote backend; "
                "local SQLite supports one backlog member only"
            )
        member = members[0]
        if config.repo_id is not None and member.repo_id != config.repo_id:
            raise click.ClickException(
                f"local project backlog member {member.repo_id!r} does not match "
                f"the current repository {config.repo_id!r}"
            )
        return project, [(member.repo_id, store, m)]

    scopes = [
        (member.repo_id, m.PgStore(conn=store.conn, repo_id=member.repo_id), m)
        for member in members
    ]
    return project, scopes


def _with_origin(value: dict, repo_id: str) -> dict:
    return {**value, "origin_repo": repo_id}


def _project_sprints(scopes: list[tuple[str, object, object]], sprint_id: int | None):
    resolved: list[tuple[str, object, object, dict]] = []
    unavailable: list[dict] = []
    for repo_id, store, m in scopes:
        if sprint_id is not None:
            sprint = m.get_sprint(store, sprint_id)
            if sprint is None:
                unavailable.append(
                    {
                        "origin_repo": repo_id,
                        "reason_code": "sprint-not-found",
                        "message": f"Sprint #{sprint_id} not found.",
                    }
                )
                continue
        else:
            backlog_sprints = [
                sprint
                for sprint in m.list_sprints(store)
                if sprint.get("kind") == "backlog" and sprint.get("status") != "closed"
            ]
            if len(backlog_sprints) > 1:
                candidates = ", ".join(f"#{sprint['id']}" for sprint in backlog_sprints)
                unavailable.append(
                    {
                        "origin_repo": repo_id,
                        "reason_code": "ambiguous-backlog-sprints",
                        "message": f"Multiple backlog sprints ({candidates}).",
                    }
                )
                continue
            if backlog_sprints:
                sprint = backlog_sprints[0]
                resolved.append((repo_id, store, m, sprint))
                continue

            active = m.list_active_sprints(store)
            if not active:
                unavailable.append(
                    {
                        "origin_repo": repo_id,
                        "reason_code": "no-backlog-or-active-sprint",
                        "message": "No backlog or active sprint found.",
                    }
                )
                continue
            if len(active) > 1:
                candidates = ", ".join(f"#{sprint['id']}" for sprint in active)
                unavailable.append(
                    {
                        "origin_repo": repo_id,
                        "reason_code": "ambiguous-active-sprints",
                        "message": f"Multiple active sprints ({candidates}).",
                    }
                )
                continue
            sprint = active[0]
        resolved.append((repo_id, store, m, sprint))
    if not resolved:
        detail = "; ".join(
            f"{entry['origin_repo']}: {entry['message']}" for entry in unavailable
        )
        raise click.ClickException(f"project scope has no resolvable sprint ({detail})")
    return resolved, unavailable


def _tag_next_work_payload(payload: dict, repo_id: str) -> dict:
    tagged = dict(payload)
    tagged["sprint"] = _with_origin(payload["sprint"], repo_id)
    for key in (
        "ready_items",
        "dependency_waiting_items",
        "active_claims",
        "active_unclaimed_items",
        "conflicts",
    ):
        tagged[key] = [_with_origin(value, repo_id) for value in payload[key]]
    tagged["next_action"] = _with_origin(payload["next_action"], repo_id)
    return tagged


def _tag_context_payload(payload: dict, repo_id: str) -> dict:
    tagged = dict(payload)
    tagged["sprint"] = _with_origin(payload["sprint"], repo_id)
    for key in (
        "active_claims",
        "active_unclaimed_items",
        "conflicts",
        "ready_items",
        "blocked_items",
        "stale_items",
        "recent_decisions",
    ):
        tagged[key] = [_with_origin(value, repo_id) for value in payload[key]]
    tagged["next_action"] = _with_origin(payload["next_action"], repo_id)
    return tagged


def _local_recovery_available() -> bool:
    try:
        config = _backend.load_backend_config()
        return config.mode == "local"
    except _backend.BackendConfigError:
        return False


def _claim_recovery_dir() -> Path:
    return _db.get_db_path().parent / "claim-recovery"


def _claim_recovery_path(claim_id: int) -> Path:
    return _claim_recovery_dir() / f"claim-{claim_id}.json"


def _write_claim_recovery_record(claim: dict) -> Path | None:
    if not _local_recovery_available():
        return None
    claim_id = claim.get("claim_id")
    claim_token = claim.get("claim_token")
    if claim_id is None or not claim_token:
        return None
    path = _claim_recovery_path(int(claim_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "claim_id": claim["claim_id"],
        "work_item_id": claim["work_item_id"],
        "actor": claim["actor"],
        "claim_type": claim["claim_type"],
        "claim_token": claim_token,
        "runtime_session_id": claim.get("runtime_session_id"),
        "instance_id": claim.get("instance_id"),
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _remove_claim_recovery_record(claim_id: int) -> None:
    if not _local_recovery_available():
        return
    path = _claim_recovery_path(claim_id)
    if path.exists():
        path.unlink()


def _load_claim_recovery_record(claim_id: int) -> dict | None:
    path = _claim_recovery_path(claim_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _claim_recovery_status(
    claim: dict,
    *,
    current_runtime_session_id: str | None,
    current_instance_id: str | None,
) -> dict:
    path = _claim_recovery_path(claim["claim_id"])
    record = _load_claim_recovery_record(claim["claim_id"])
    claim_runtime_session_id = claim.get("runtime_session_id")
    claim_instance_id = claim.get("instance_id")
    runtime_session_id_matches = bool(
        current_runtime_session_id and claim_runtime_session_id == current_runtime_session_id
    )
    instance_id_matches = bool(current_instance_id and claim_instance_id == current_instance_id)
    return {
        "claim_id": claim["claim_id"],
        "work_item_id": claim["work_item_id"],
        "actor": claim["actor"],
        "claim_type": claim["claim_type"],
        "current_identity": {
            "runtime_session_id": current_runtime_session_id,
            "instance_id": current_instance_id,
        },
        "claim_identity": {
            "runtime_session_id": claim_runtime_session_id,
            "instance_id": claim_instance_id,
        },
        "runtime_session_id_matches": runtime_session_id_matches,
        "instance_id_matches": instance_id_matches,
        "plausible_identity_match": runtime_session_id_matches or instance_id_matches,
        "recovery_token_exists": record is not None,
        "recovery_token_path": str(path),
        "recovery_record_written_at": record.get("written_at") if record else None,
    }


def _claim_with_recovery_status(
    claim: dict,
    *,
    current_runtime_session_id: str | None,
    current_instance_id: str | None,
) -> dict:
    enriched = dict(claim)
    enriched["local_recovery"] = _claim_recovery_status(
        claim,
        current_runtime_session_id=current_runtime_session_id,
        current_instance_id=current_instance_id,
    )
    return enriched


def _find_recoverable_claim(conn: sqlite3.Connection, *, claim_id: int | None, item_id: int | None) -> dict:
    if claim_id is not None:
        claim = _db.get_claim(conn, claim_id)
        if claim is None:
            raise ValueError(f"Claim #{claim_id} not found")
        return claim
    assert item_id is not None
    claims = _db.list_claims(conn, item_id, active_only=True)
    if not claims:
        raise ValueError(f"No active claims found for item #{item_id}")
    if len(claims) > 1:
        candidates = ", ".join(str(c["claim_id"]) for c in claims)
        raise ValueError(
            f"Multiple active claims found for item #{item_id}; rerun with --id. Candidates: {candidates}"
        )
    return claims[0]


def _style_status(status: str) -> str:
    palette = {
        "planned": "yellow",
        "pending": "yellow",
        "active": "cyan",
        "done": "green",
        "blocked": "red",
        "closed": "magenta",
    }
    return click.style(status, fg=palette.get(status, "white"), bold=True)


def _format_priority(item: dict) -> str:
    priority = _db.effective_priority(item)
    return f"p{priority}" if priority is not None else "-"


def _pad_styled(value: str, width: int) -> str:
    visible = len(click.unstyle(value))
    if visible >= width:
        return value
    return value + (" " * (width - visible))


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(click.unstyle(str(cell))))
    header = "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    rendered_rows = [
        "  ".join(_pad_styled(str(row[i]), widths[i]) for i in range(len(headers)))
        for row in rows
    ]
    return [header, separator, *rendered_rows]


def _clear_terminal_for_watch(stdout: TextIO | None = None, term: str | None = None) -> bool:
    stream = stdout if stdout is not None else sys.stdout
    active_term = term if term is not None else os.environ.get("TERM", "")
    if not stream.isatty() or not active_term or active_term.lower() == "dumb":
        return False
    click.echo("\033[2J\033[H", nl=False, file=stream)
    return True


def _escape_fzf_field(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _collect_sprint_show_payload(conn, s: dict, detail: bool, *, m=None) -> dict:
    m = m or _db
    out: dict = {
        "id": s["id"],
        "name": s["name"],
        "goal": s["goal"],
        "start_date": s["start_date"],
        "end_date": s["end_date"],
        "status": s["status"],
        "kind": s["kind"],
    }
    if not detail:
        return out

    from . import calc as _calc

    now = datetime.now(timezone.utc)
    items = m.list_work_items(conn, sprint_id=s["id"])
    tracks = m.list_tracks(conn, s["id"])
    active_items = [it for it in items if it["status"] == "active"]
    risk = _calc.sprint_overrun_risk(s, len(active_items), now)
    pending_threshold = _maintain._pending_stale_threshold()
    stale_count = sum(
        1 for it in items if _calc.item_staleness(it, now, pending_threshold=pending_threshold)["is_stale"]
    )
    items_by_track: dict[int, list] = {}
    for it in items:
        items_by_track.setdefault(it["track_id"], []).append(it)
    track_health_out = {}
    for t in tracks:
        track_health_out[t["name"]] = _calc.track_health(items_by_track.get(t["id"], []))
    active_takeups = m.list_active_takeups(conn, s["id"])
    out["detail"] = {
        "risk": risk,
        "stale_count": stale_count,
        "track_health": track_health_out,
        "takeup": {
            "active_count": len(active_takeups),
            "active": active_takeups,
        },
    }
    return out


def _resolve_implicit_sprint(conn, *, option_name: str = "--sprint-id", m=None) -> dict | None:
    m = m or _db
    active_sprints = m.list_active_sprints(conn)
    if not active_sprints:
        return None
    if len(active_sprints) > 1:
        candidates = ", ".join(f"#{s['id']}" for s in active_sprints)
        click.echo(
            f"Multiple active sprints ({candidates}). Pass {option_name} explicitly.",
            err=True,
        )
        sys.exit(1)
    return active_sprints[0]


def _emit_sprint_show_text(payload: dict, detail: bool) -> None:
    click.echo(f"ID:     {payload['id']}")
    click.echo(f"Name:   {payload['name']}")
    click.echo(f"Goal:   {payload['goal']}")
    if payload.get("start_date") and payload.get("end_date"):
        click.echo(f"Dates:  {payload['start_date']} to {payload['end_date']}")
    click.echo(f"Status: {payload['status']}")
    click.echo(f"Kind:   {payload['kind']}")

    if not detail:
        return

    detail_payload = payload["detail"]
    risk = detail_payload["risk"]
    stale_count = detail_payload["stale_count"]
    risk_tag = ""
    if risk["overdue"]:
        risk_tag = " [OVERDUE]"
    elif risk["at_risk"]:
        risk_tag = " [AT RISK]"
    if risk.get("date_bound", True):
        click.echo(
            f"\nHealth: {risk['days_remaining']} days remaining, "
            f"{risk['active_items']} active, {stale_count} stale{risk_tag}"
        )
    else:
        click.echo(f"\nHealth: {risk['active_items']} active, {stale_count} stale")
    click.echo("Track health:")
    track_health = detail_payload["track_health"]
    for track_name, health in track_health.items():
        done_pct = int(health["done_ratio"] * 100)
        blocked_pct = int(health["blocked_ratio"] * 100)
        c = health["counts"]
        click.echo(
            f"  {track_name}: {health['total']} items — "
            f"{c['done']} done ({done_pct}%), "
            f"{c['active']} active, "
            f"{c['pending']} pending, "
            f"{c['blocked']} blocked ({blocked_pct}%)"
        )
    takeup = detail_payload.get("takeup", {})
    active_takeups = takeup.get("active", [])
    if active_takeups:
        click.echo("\nTakeup:")
        for row in active_takeups:
            click.echo(
                f"  {row['actor']}@{row.get('hostname') or '-'}  "
                f"(instance {row.get('instance_id') or '-'})  "
                f"since {row['taken_up_at']}  ctx: {row.get('context') or '-'}"
            )


# ---------------------------------------------------------------------------
# sprint
# ---------------------------------------------------------------------------

@cli.group()
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
@click.option("--id", "sprint_id", type=int, default=None, help="Sprint ID")
@click.option("--detail", is_flag=True, default=False, help="Include sprint health, track health, and stale item count")
@click.option("--watch", "watch_mode", is_flag=True, default=False, help="Refresh output in a loop until interrupted")
@click.option("--interval", type=float, default=30.0, show_default=True, help="Watch refresh interval in seconds")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_show(obj, sprint_id, detail, watch_mode, interval, as_json) -> None:
    """Show a sprint (defaults to active sprint)."""
    if watch_mode and as_json:
        click.echo("Error: --watch cannot be combined with --json.", err=True)
        sys.exit(1)
    if interval <= 0:
        click.echo("Error: --interval must be > 0.", err=True)
        sys.exit(1)

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


@sprint.command("status")
@click.option("--id", "sprint_id", type=int, required=True, help="Sprint ID")
@click.option(
    "--status",
    "new_status",
    required=True,
    type=click.Choice(["planned", "active", "closed"]),
    help="New status",
)
@click.option("--actor", default=None, help="Actor name (defaults to the current OS user)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def sprint_status(obj, sprint_id, new_status, actor, as_json) -> None:
    """Update a sprint's status (enforces allowed transitions)."""
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
            boundary_event_id = m.close_sprint_with_boundary_event(store, sprint_id, actor)
        else:
            m.set_sprint_status(store, sprint_id, new_status)
    except (_db.InvalidTransition, ValueError) as e:
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
    if project_path is None:
        scopes = [(None, *_get_store(obj))]
    else:
        _binding, scopes = _get_project_stores(obj, project_path)

    sprints: list[dict] = []
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


@sprint.command("kind")
@click.option("--id", "sprint_id", type=int, required=True, help="Sprint ID")
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
@click.option("--from-sprint-id", "source_sprint_id", type=int, required=True,
              help="Sprint ID to read knowledge candidates from")
@click.option("--to-sprint-id", "target_sprint_id", type=int, required=True,
              help="Sprint ID (backlog) to seed items into")
@click.option("--actor", default="system", help="Actor name (default: system)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output seeded items as JSON")
@click.pass_obj
def sprint_backlog_seed(obj, source_sprint_id, target_sprint_id, actor, as_json) -> None:
    """Seed backlog items from knowledge candidate events in another sprint."""
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

@cli.group()
def item() -> None:
    """Manage work items."""


@item.command("add")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
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
def item_add(obj, sprint_id, track_name, title, description, assignee, priority, as_json) -> None:
    """Add a work item to a sprint track."""
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
@click.option("--id", "item_id", type=int, required=True, help="Item ID")
@click.option("--description", required=True, help="Non-empty implementation scope or objective")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output updated item as JSON")
@click.pass_obj
def item_edit(obj, item_id, description, as_json) -> None:
    """Replace a work item's description."""
    try:
        _db.validate_work_item_description(description)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--description") from exc

    store, m = _get_store(obj)
    try:
        m.update_work_item_description(store, item_id, description)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    updated = m.get_work_item(store, item_id)
    assert updated is not None
    if as_json:
        click.echo(json.dumps(updated, indent=2))
        return
    click.echo(f"Updated item #{item_id} description.")


@item.command("priority")
@click.option("--id", "item_id", type=int, required=True, help="Item ID")
@click.option("--set", "priority", type=int, default=None, help="Priority 1-9 (1 = highest)")
@click.option("--clear", is_flag=True, default=False, help="Clear the priority (unprioritized)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output updated item as JSON")
@click.pass_obj
def item_priority(obj, item_id, priority, clear, as_json) -> None:
    """Set or clear a work item's native priority.

    Priority orders next-work suggestions (1 first, unprioritized last) and
    replaces the legacy [pN] title-prefix convention, which remains recognized
    as a fallback when no native priority is set.
    """
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
# surfaces are served from the cached projection populated by the shadow
# pilot sync path (sprintctl/pilot.py, sprintctl/sync.py) instead of hitting
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
        pilot_status = _pilot.shadow_pilot_status(cwd=cwd)
    except _pilot.ShadowPilotConfigError:
        base["health"] = "missing"
        return base
    path = pilot_status.paths.projection_path
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

    Only observation-classified events mirrored via the shadow pilot (see
    ``_shadow_observation_envelope``) are present here; authority-changing
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
@click.option("--id", "item_id", type=int, required=True, help="Item ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_show(obj, item_id, as_json) -> None:
    """Show a single work item with its recent events and active claims."""
    store, m = _get_store(obj)
    it = m.get_work_item(store, item_id)
    if it is None:
        click.echo(f"Item #{item_id} not found.", err=True)
        sys.exit(1)

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

    claims = m.list_claims(store, item_id, active_only=True)
    refs = m.list_refs(store, item_id)
    blocking = m.list_deps_blocking(store, item_id)
    blocked_by_me = m.list_deps_blocked_by(store, item_id)

    if as_json:
        click.echo(json.dumps({
            "item": dict(it),
            "events": item_events,
            "active_claims": claims,
            "refs": refs,
            "deps": {"blocked_by": blocking, "blocks": blocked_by_me},
            "projection": projection_status,
        }, indent=2))
        return

    click.echo(f"#{it['id']}  [{it['status']}]  {it['title']}")
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

    if claims:
        click.echo("\nActive claims:")
        for c in claims:
            excl = "exclusive" if c["exclusive"] else "shared"
            parts = [
                f"  #{c['claim_id']}  {c['actor']}  [{c['claim_type']}]  {excl}  "
                f"proof={c['identity_status']}  expires={c['expires_at']}"
            ]
            if c.get("runtime_session_id"):
                parts.append(f"  runtime={c['runtime_session_id']}")
            if c.get("instance_id"):
                parts.append(f"  instance={c['instance_id']}")
            if c.get("branch"):
                parts.append(f"  branch={c['branch']}")
            if c.get("commit_sha"):
                parts.append(f"  commit={c['commit_sha']}")
            if c.get("pr_ref"):
                parts.append(f"  pr={c['pr_ref']}")
            if c.get("worktree_path"):
                parts.append(f"  worktree={c['worktree_path']}")
            if c.get("hostname"):
                parts.append(f"  host={c['hostname']}")
            if c.get("pid") is not None:
                parts.append(f"  pid={c['pid']}")
            click.echo("".join(parts))

    if item_events:
        click.echo("\nEvents:")
        for e in item_events[-10:]:
            click.echo(f"  #{e['id']}  [{e['event_type']}]  {e['actor']}  {e['created_at']}")
    else:
        click.echo("\nEvents: (none)")


@item.command("list")
@click.option("--sprint-id", type=int, default=None, help="Filter by sprint ID")
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
    if as_json and as_fzf:
        click.echo("Error: --fzf cannot be combined with --json.", err=True)
        sys.exit(1)

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


@item.command("note")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--type", "note_type", required=True, help="Note type (e.g. decision, blocker, update)")
@click.option("--summary", required=True, help="Short summary")
@click.option("--detail", default=None, help="Extended detail")
@click.option("--tags", default=None, help="Comma-separated tags")
@click.option("--actor", default="actor", help="Actor name (default: actor)")
@click.option("--evidence-item-id", type=int, default=None, help="Work item ID this knowledge came from")
@click.option("--evidence-event-id", type=int, default=None, help="Event ID this knowledge came from")
@click.option("--git-branch", default=None, help="Git branch name at time of note")
@click.option("--git-sha", default=None, help="Git commit SHA at time of note")
@click.option("--git-worktree", default=None, help="Git worktree path at time of note")
@click.pass_obj
def item_note(
    obj, item_id, note_type, summary, detail, tags, actor,
    evidence_item_id, evidence_event_id,
    git_branch, git_sha, git_worktree,
) -> None:
    """Record a structured note event on a work item."""
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


@item.command("status")
@click.option("--id", "item_id", type=int, required=True, help="Item ID")
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
@click.option("--json", "as_json", is_flag=True, default=False, help="Output status transition as JSON")
@click.pass_obj
def item_status(obj, item_id, new_status, actor, claim_id, claim_token, as_json) -> None:
    """Update an item's status (enforces transitions, claims, and dependency safety)."""
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
        )
    except (_db.InvalidTransition, _db.ClaimConflict) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps({"item_id": item_id, "previous": current, "status": new_status}, indent=2))
        return
    click.echo(f"Item #{item_id} status: {current} -> {new_status}")


@item.command("done-from-claim")
@click.option("--id", "item_id", type=int, default=None, help="Item ID (defaults to the claim's item)")
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
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option(
    "--type", "ref_type",
    required=True,
    type=click.Choice(["pr", "issue", "doc", "other", "file", "glob", "manifest"]),
    help="Reference type",
)
@click.option(
    "--url",
    required=True,
    help=("Reference target. Doc refs accept URLs or repo-relative paths; "
          "file, glob, and manifest refs require repo-relative POSIX paths."),
)
@click.option("--label", default="", help="Short human-readable label")
@click.pass_obj
def item_ref_add(obj, item_id, ref_type, url, label) -> None:
    """Attach an external reference to a work item."""
    store, m = _get_store(obj)
    try:
        ref_id = m.add_ref(store, item_id, ref_type, url, label)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Ref #{ref_id} added to item #{item_id}: [{ref_type}] {url}")


@item_ref.command("list")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_ref_list(obj, item_id, as_json) -> None:
    """List external references on a work item."""
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
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--ref-id", type=int, required=True, help="Ref ID to remove")
@click.pass_obj
def item_ref_remove(obj, item_id, ref_id) -> None:
    """Remove an external reference from a work item."""
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
@click.option("--id", "item_id", type=int, required=True, help="Blocker item ID (must complete first)")
@click.option("--blocks-item-id", type=int, required=True, help="ID of the item being blocked")
@click.pass_obj
def item_dep_add(obj, item_id, blocks_item_id) -> None:
    """Record that item --id must complete before --blocks-item-id can start."""
    store, m = _get_store(obj)
    try:
        dep_id = m.add_dep(store, item_id, blocks_item_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Dep #{dep_id}: item #{item_id} blocks item #{blocks_item_id}")


@item_dep.command("list")
@click.option("--id", "item_id", type=int, required=True, help="Work item ID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def item_dep_list(obj, item_id, as_json) -> None:
    """List dependencies for a work item (what blocks it and what it blocks)."""
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
@click.option("--id", "item_id", type=int, required=True, help="Work item ID (either side of the dep)")
@click.option("--dep-id", type=int, required=True, help="Dep ID to remove")
@click.pass_obj
def item_dep_remove(obj, item_id, dep_id) -> None:
    """Remove a dependency."""
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

@cli.group()
def event() -> None:
    """Manage events."""


@event.group("observation")
def event_observation() -> None:
    """Manage offline, item-linked evidence observations."""


def _parse_evidence_ref_option(value: str, option_name: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{option_name} must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise click.ClickException(f"{option_name} must be a JSON object")
    return parsed


def _item_evidence_pilot_status(*, require_enabled: bool) -> _pilot.ShadowPilotStatus:
    try:
        status = _pilot.shadow_pilot_status(cwd=Path.cwd())
    except _pilot.ShadowPilotConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if require_enabled and not status.enabled:
        raise click.ClickException(
            "shadow pilot is disabled; run 'sprintctl pilot enable' before appending observations"
        )
    return status


@event_observation.command("add")
@click.option(
    "--type",
    "event_type",
    type=click.Choice(_observations.ITEM_EVIDENCE_EVENT_TYPES),
    required=True,
)
@click.option("--sprint-id", type=int, required=True, help="Linked sprint ID")
@click.option("--item-id", "work_item_id", type=int, required=True, help="Linked work item ID")
@click.option("--actor", required=True, help="Observation author")
@click.option("--repo-id", default=None, help="Repository scope; defaults to the current repo")
@click.option(
    "--runtime-session-id",
    default=None,
    help="Runtime session correlation; defaults from SPRINTCTL_RUNTIME_SESSION_ID or CODEX_THREAD_ID",
)
@click.option("--summary", default=None, help="Required summary for work.completed")
@click.option(
    "--evidence-ref",
    "evidence_ref_values",
    multiple=True,
    help='Immutable ref JSON: {"kind":"git-commit","source":"repo:name","revision":"..."}',
)
@click.option(
    "--capsule-ref",
    default=None,
    help='session-capsule/v1 artifact ref JSON with an artifact kind and sha256 revision',
)
@click.option("--basis-revision", default=None, help="Aggregate revision observed by the producer")
@click.option("--event-id", default=None, help="Stable observation UUID for idempotent retry")
@click.option("--occurred-at", default=None, help="ISO 8601 observation time; defaults to now")
@click.option("--correlation-id", default=None, help="Optional correlation UUID")
@click.option("--causation-id", default=None, help="Optional causation UUID")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON")
def event_observation_add(
    event_type,
    sprint_id,
    work_item_id,
    actor,
    repo_id,
    runtime_session_id,
    summary,
    evidence_ref_values,
    capsule_ref,
    basis_revision,
    event_id,
    occurred_at,
    correlation_id,
    causation_id,
    as_json,
) -> None:
    """Append evidence without reading or mutating authoritative item state."""
    status = _item_evidence_pilot_status(require_enabled=True)
    runtime_session_id = _detect_runtime_session_id(runtime_session_id)
    if runtime_session_id is None:
        raise click.ClickException(
            "runtime session identity is required; pass --runtime-session-id or set "
            "SPRINTCTL_RUNTIME_SESSION_ID"
        )
    if repo_id is None:
        try:
            _root, repo_id, _marker = _backend.resolve_repo_identity(Path.cwd())
        except _backend.BackendConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        if repo_id is None:
            raise click.ClickException("cannot resolve repo_id; pass --repo-id explicitly")
    evidence_refs = [
        _parse_evidence_ref_option(value, "--evidence-ref")
        for value in evidence_ref_values
    ]
    capsule = (
        _parse_evidence_ref_option(capsule_ref, "--capsule-ref")
        if capsule_ref is not None
        else None
    )
    producer = _outbox.open_outbox(status.paths.outbox_path)
    try:
        existing = _outbox.get_record(producer, event_id) if event_id is not None else None
        duplicate = existing is not None
        occurred_at = occurred_at or (
            existing.occurred_at
            if existing is not None
            else datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
        )
        record = _observations.append_item_evidence_observation(
            producer,
            event_type=event_type,
            actor=actor,
            repo_id=repo_id,
            sprint_id=sprint_id,
            work_item_id=work_item_id,
            evidence_refs=evidence_refs,
            summary=summary,
            capsule_ref=capsule,
            basis_revision=basis_revision,
            event_id=event_id,
            authored_at=occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            runtime_session_id=runtime_session_id,
        )
        projected = _observations.project_item_evidence(record)
    except (TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        producer.close()

    payload = {
        "operation": "event_observation_add",
        "disposition": "duplicate" if duplicate else "appended",
        "observation": projected.to_dict(),
        "outbox_path": str(status.paths.outbox_path),
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(
            f"{payload['disposition'].capitalize()} {event_type} observation "
            f"{record.event_id} for item #{work_item_id}; authority unchanged."
        )


@event_observation.command("list")
@click.option("--item-id", "work_item_id", type=int, default=None, help="Filter by work item ID")
@click.option(
    "--type",
    "event_type",
    type=click.Choice(_observations.ITEM_EVIDENCE_EVENT_TYPES),
    default=None,
)
@click.option(
    "--current-basis-revision",
    default=None,
    help="Current aggregate revision used to classify retained observations",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON")
def event_observation_list(work_item_id, event_type, current_basis_revision, as_json) -> None:
    """List local and ingested evidence with explicit stale-basis visibility."""
    status = _item_evidence_pilot_status(require_enabled=False)
    records_by_id: dict[str, dict] = {}

    if status.paths.outbox_path.exists():
        producer = _outbox.open_outbox(status.paths.outbox_path)
        try:
            for record in _outbox.list_records(producer):
                records_by_id[record.event_id] = {
                    "record": record,
                    "local": True,
                    "ingested": False,
                    "ingest_offset": None,
                }
        finally:
            producer.close()

    watermark = None
    if status.paths.projection_path.exists():
        cache = _projection.open_cached_projection(status.paths.projection_path)
        try:
            projected_watermark = _projection.get_watermark(cache)
            watermark = {
                "ingest_offset": projected_watermark.ingest_offset,
                "advanced_at": projected_watermark.advanced_at,
            }
            for cached in _projection.list_cached_records(cache):
                record = _observations.transport_record_from_mapping(cached.record)
                entry = records_by_id.setdefault(
                    record.event_id,
                    {
                        "record": record,
                        "local": False,
                        "ingested": True,
                        "ingest_offset": cached.ingest_offset,
                    },
                )
                if entry["record"] != record:
                    raise click.ClickException(
                        f"local and cached observation {record.event_id} disagree"
                    )
                entry["ingested"] = True
                entry["ingest_offset"] = cached.ingest_offset
        finally:
            cache.close()

    rendered = []
    try:
        observations = _observations.list_item_evidence(
            (entry["record"] for entry in records_by_id.values()),
            work_item_id=work_item_id,
            event_type=event_type,
            current_revision=current_basis_revision,
        )
    except (TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for observation in observations:
        value = observation.to_dict()
        storage = records_by_id[observation.event_id]
        value["storage"] = {
            "local": storage["local"],
            "ingested": storage["ingested"],
            "ingest_offset": storage["ingest_offset"],
        }
        rendered.append(value)

    payload = {
        "observations": rendered,
        "count": len(rendered),
        "watermark": watermark,
        "authority_mutated": False,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not rendered:
        click.echo("No item evidence observations found.")
        return
    for value in rendered:
        click.echo(
            f"{value['event_id']}  {value['event_type']}  item #{value['work_item_id']}  "
            f"basis={value['basis']['classification']}  session={value['runtime_session_id']}"
        )


def _event_add_impl(
    obj,
    sprint_id: int,
    event_type: str,
    actor: str,
    work_item_id: int | None,
    source_type: str,
    payload: str | None,
    as_json: bool,
) -> None:
    store, m = _get_store(obj)
    if m.get_sprint(store, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    if work_item_id is not None and m.get_work_item(store, work_item_id) is None:
        click.echo(f"Work item #{work_item_id} not found.", err=True)
        sys.exit(1)
    payload_dict: dict | None = None
    if payload:
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError as e:
            click.echo(f"Invalid JSON payload: {e}", err=True)
            sys.exit(1)
    try:
        eid = m.create_event(
            store, sprint_id, actor, event_type,
            source_type=source_type, work_item_id=work_item_id, payload=payload_dict,
            expected_project=(obj.get("backend_config").repo_id if obj.get("backend_config") else None),
        )
    except (TypeError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    backend_config = obj.get("backend_config")
    repo_id = backend_config.repo_id if backend_config is not None else Path.cwd().name
    persisted = next((event for event in m.list_events(store, sprint_id) if event["id"] == eid), None)
    shadow_result = (
        _mirror_shadow_event(persisted, repo_id=repo_id)
        if persisted is not None
        else {"status": "unavailable", "detail": "created event could not be read back"}
    )
    if as_json:
        click.echo(json.dumps({
            "operation": "event_add",
            "event_id": eid,
            "sprint_id": sprint_id,
            "item_id": work_item_id,
            "type": event_type,
            "actor": actor,
            "source": source_type,
            "shadow_pilot": shadow_result,
        }, indent=2))
        return
    click.echo(f"Recorded event #{eid}: {event_type}  (actor: {actor})")
    if shadow_result["status"] not in {"disabled", "unsupported"}:
        click.echo(f"Shadow pilot: {shadow_result['status']}")


@event.command("add")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--type", "--event-type", "event_type", required=True, help="Event type")
@click.option("--actor", required=True, help="Actor name")
@click.option("--item-id", "work_item_id", type=int, default=None, help="Work item ID")
@click.option(
    "--source",
    "source_type",
    default="actor",
    type=click.Choice(["actor", "daemon", "system"]),
    help="Source type",
)
@click.option("--payload", default=None, help="JSON payload string")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output created event metadata as JSON")
@click.pass_obj
def event_add(obj, sprint_id, event_type, actor, work_item_id, source_type, payload, as_json) -> None:
    """Record an event."""
    _event_add_impl(obj, sprint_id, event_type, actor, work_item_id, source_type, payload, as_json)


@event.command("log")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--type", "--event-type", "event_type", required=True, help="Event type")
@click.option("--actor", required=True, help="Actor name")
@click.option("--item-id", "work_item_id", type=int, default=None, help="Work item ID")
@click.option(
    "--source",
    "source_type",
    default="actor",
    type=click.Choice(["actor", "daemon", "system"]),
    help="Source type",
)
@click.option("--payload", default=None, help="JSON payload string")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output created event metadata as JSON")
@click.pass_obj
def event_log(obj, sprint_id, event_type, actor, work_item_id, source_type, payload, as_json) -> None:
    """Alias for 'event add'."""
    _event_add_impl(obj, sprint_id, event_type, actor, work_item_id, source_type, payload, as_json)


# ---------------------------------------------------------------------------
# feature-flagged remote authority command path
# ---------------------------------------------------------------------------

_AUTHORITY_COMMAND_TYPES = (
    "claim.acquire",
    "claim.renew",
    "claim.handoff",
    "claim.release",
    "item.transition",
    "item.done",
    "sprint.activate",
    "sprint.close",
    "capability-receipt.accept",
)


def _authority_repo_uuid(repo_root: Path) -> str:
    manifest = repo_root / "sprintctl.dispatch.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        return str(uuid.UUID(str(raw["repo_id"])))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _authority_config.AuthorityCommandConfigError(
            "authority commands require a committed UUID repo_id in sprintctl.dispatch.json"
        ) from exc


def _authority_rollout_status() -> _authority_config.AuthorityCommandStatus:
    try:
        return _authority_config.authority_command_status(cwd=Path.cwd())
    except _authority_config.AuthorityCommandConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _authority_command_target(store, m, record_type: str, aggregate_id: int):
    if record_type == "claim.acquire":
        item = m.get_work_item(store, aggregate_id)
        if item is None:
            raise click.ClickException(f"Item #{aggregate_id} not found")
        return "item", item, item["aggregate_uuid"]
    if record_type in {"item.transition", "item.done"}:
        item = m.get_work_item(store, aggregate_id)
        if item is None:
            raise click.ClickException(f"Item #{aggregate_id} not found")
        return "item", item, item["aggregate_uuid"]
    if record_type in {"claim.renew", "claim.handoff", "claim.release"}:
        claim = m.get_claim(store, aggregate_id, include_secret=False)
        if claim is None:
            raise click.ClickException(f"Claim #{aggregate_id} not found")
        return "claim", claim, None
    sprint = m.get_sprint(store, aggregate_id)
    if sprint is None:
        raise click.ClickException(f"Sprint #{aggregate_id} not found")
    return "sprint", sprint, sprint["aggregate_uuid"]


def _authority_basis_revision(
    store,
    m,
    record_type: str,
    aggregate_id: int,
    aggregate: dict,
) -> str:
    if record_type in {"item.transition", "item.done", "claim.acquire"}:
        return _authority.item_revision(aggregate)
    if record_type in {"sprint.activate", "sprint.close"}:
        return _authority.sprint_revision(aggregate)
    if record_type in {"claim.renew", "claim.handoff", "claim.release"}:
        return _authority.claim_revision(aggregate)
    events = [
        event
        for event in m.list_events(store, aggregate_id)
        if event["event_type"] == _contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
    ]
    if len(events) != 1:
        raise click.ClickException(
            "capability receipt acceptance requires exactly one sprint-close-boundary"
        )
    return f"event:{events[0]['id']}"


@cli.group("authority")
def authority_commands() -> None:
    """Operate the feature-flagged remote authority command journal."""


@authority_commands.command("status")
@click.option("--json", "as_json", is_flag=True, default=False)
def authority_status(as_json: bool) -> None:
    """Show rollout mode and local durable command counts without secrets."""
    status = _authority_rollout_status()
    payload = status.to_dict()
    payload["outbox_records"] = 0
    payload["pending_credentials"] = 0
    if status.paths.outbox_path.exists():
        producer = _outbox.open_outbox(status.paths.outbox_path)
        try:
            payload["outbox_records"] = len(_outbox.list_records(producer))
        finally:
            producer.close()
    if status.paths.credential_dir.exists():
        payload["pending_credentials"] = len(
            [path for path in status.paths.credential_dir.iterdir() if path.is_file()]
        )
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Authority command mode: {payload['mode']}")
        click.echo(f"Durable producer records: {payload['outbox_records']}")
        click.echo(f"Pending proof sidecars: {payload['pending_credentials']}")


@authority_commands.command("mode")
@click.option(
    "--set",
    "mode",
    type=click.Choice(["off", "shadow", "enforce"]),
    required=True,
)
@click.option("--json", "as_json", is_flag=True, default=False)
def authority_mode(mode: str, as_json: bool) -> None:
    """Set the explicit per-repository authority rollout mode."""
    try:
        status = _authority_config.set_authority_command_mode(mode, cwd=Path.cwd())
    except _authority_config.AuthorityCommandConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(status.to_dict(), indent=2))
    else:
        click.echo(f"Authority command mode set to {status.mode.value}.")


@authority_commands.command("submit")
@click.option("--type", "record_type", type=click.Choice(_AUTHORITY_COMMAND_TYPES), required=True)
@click.option("--aggregate-id", type=int, required=True, help="Item, sprint, or claim integer ID")
@click.option("--payload", default="{}", help="Command payload JSON object")
@click.option("--basis-revision", default=None, help="Expected authority revision (auto-detected by default)")
@click.option("--event-id", default=None, help="Caller-supplied stable request UUID")
@click.option("--actor", required=True)
@click.option(
    "--claim-token",
    default=None,
    envvar="SPRINTCTL_AUTHORITY_CLAIM_TOKEN",
    help="Transient existing claim proof (prefer the environment variable)",
)
@click.option(
    "--coordinate-claim-token",
    default=None,
    envvar="SPRINTCTL_AUTHORITY_COORDINATE_CLAIM_TOKEN",
    help="Transient coordinator proof (prefer the environment variable)",
)
@click.option(
    "--proposed-claim-token",
    default=None,
    envvar="SPRINTCTL_AUTHORITY_PROPOSED_CLAIM_TOKEN",
    help="Transient pre-minted new proof (auto-generated when omitted)",
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def authority_submit(
    obj,
    record_type,
    aggregate_id,
    payload,
    basis_revision,
    event_id,
    actor,
    claim_token,
    coordinate_claim_token,
    proposed_claim_token,
    as_json,
) -> None:
    """Durably append and, in enforce mode, remotely arbitrate one command."""
    rollout = _authority_rollout_status()
    if rollout.mode is _authority_config.AuthorityCommandMode.OFF:
        raise click.ClickException(
            "authority command mode is off; use 'sprintctl authority mode --set shadow|enforce'"
        )
    store, m = _get_store(obj)
    backend_config = obj["backend_config"]
    if rollout.mode is _authority_config.AuthorityCommandMode.ENFORCE and backend_config.mode != "remote":
        raise click.ClickException("authority enforce mode requires a remote PostgreSQL backend")
    if backend_config.mode == "remote":
        store.authority_repo_uuid = _authority_repo_uuid(rollout.paths.repo_root)
    try:
        command_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --payload JSON: {exc}") from exc
    if not isinstance(command_payload, dict):
        raise click.ClickException("--payload must be a JSON object")

    generated_secret: str | None = None
    producer = _outbox.open_outbox(rollout.paths.outbox_path)
    try:
        durable = _outbox.get_record(producer, event_id) if event_id else None
    finally:
        producer.close()

    if durable is not None:
        try:
            request = _contracts.record_from_dict(durable.payload)
        except (TypeError, ValueError) as exc:
            raise click.ClickException(
                f"durable authority request {durable.event_id!r} is invalid: {exc}"
            ) from exc
        if not isinstance(request, _contracts.AuthorityCommand):
            raise click.ClickException(
                f"event_id {durable.event_id!r} already identifies a non-authority producer record"
            )
        if request.record_type != record_type:
            raise click.ClickException(
                f"event_id {durable.event_id!r} already identifies {request.record_type!r}"
            )
        if request.actor != actor:
            raise click.ClickException(
                f"event_id {durable.event_id!r} already identifies a command from a different actor"
            )
        if request.refs.get("aggregate_id") != aggregate_id:
            raise click.ClickException(
                f"event_id {durable.event_id!r} already identifies a different aggregate"
            )
        if basis_revision is not None and request.basis_revision != basis_revision:
            raise click.ClickException(
                f"event_id {durable.event_id!r} already identifies a different basis revision"
            )
        if any(request.payload.get(key) != value for key, value in command_payload.items()):
            raise click.ClickException(
                f"event_id {durable.event_id!r} already identifies a command with a different payload"
            )
        try:
            pending = _authority_config.load_pending_authority_credential(
                rollout.paths,
                event_id=durable.event_id,
            )
        except _authority_config.AuthorityCommandConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        credentials = dict(pending.credentials) if pending is not None else {}
    else:
        aggregate_type, aggregate, aggregate_uuid = _authority_command_target(
            store, m, record_type, aggregate_id
        )
        basis_revision = basis_revision or _authority_basis_revision(
            store, m, record_type, aggregate_id, aggregate
        )
        credentials: dict[str, str] = {}
        generated_ref: str | None = None

        if claim_token is not None:
            ref = _authority.credential_ref(claim_token)
            command_payload.setdefault("credential_ref", ref)
            credentials[ref] = claim_token
        if coordinate_claim_token is not None:
            ref = _authority.credential_ref(coordinate_claim_token)
            command_payload.setdefault("coordinate_credential_ref", ref)
            credentials[ref] = coordinate_claim_token
        if record_type == "claim.acquire" or (
            record_type == "claim.handoff" and command_payload.get("mode", "rotate") == "rotate"
        ):
            generated_secret = proposed_claim_token or secrets.token_urlsafe(24)
            ref = _authority.credential_ref(generated_secret)
            generated_ref = ref
            target_field = "credential_ref" if record_type == "claim.acquire" else "proposed_credential_ref"
            command_payload.setdefault(target_field, ref)
            credentials[ref] = generated_secret
        if record_type in {"claim.renew", "claim.handoff", "claim.release"}:
            command_payload.setdefault("claim_id", aggregate_id)

        refs: dict[str, object] = {
            "repo_id": _authority_repo_uuid(rollout.paths.repo_root),
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        }
        if aggregate_uuid is not None:
            refs["aggregate_uuid"] = aggregate_uuid
        if aggregate_type == "claim":
            refs["claim_id"] = aggregate_id
        try:
            request = _contracts.AuthorityCommand(
                event_id=event_id or str(uuid.uuid4()),
                record_type=record_type,
                schema_version="1",
                actor=actor,
                authored_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                refs=refs,
                payload=command_payload,
                basis_revision=basis_revision,
                correlation_id=event_id or str(uuid.uuid4()),
            )
        except (TypeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc

        if credentials:
            _authority_config.store_pending_authority_credentials(
                rollout.paths,
                event_id=request.event_id,
                credentials=credentials,
                recovery_credential_ref=generated_ref,
            )
        producer = _outbox.open_outbox(rollout.paths.outbox_path)
        try:
            durable = _outbox.append_authority_command(
                producer,
                request,
                runtime_session_id=_detect_runtime_session_id(None),
            )
        finally:
            producer.close()

    result: dict[str, object] = {
        "request_event_id": durable.event_id,
        "origin_stream_id": durable.origin_stream_id,
        "origin_seq": durable.origin_seq,
        "mode": rollout.mode.value,
        "status": "pending-shadow",
    }
    exit_code = 0
    if rollout.mode is _authority_config.AuthorityCommandMode.ENFORCE:
        decision = _authority.arbitrate_command(store, durable, credentials=credentials)
        result.update(decision.to_dict())
        result["status"] = decision.outcome
        retains_recovery_proof = request.record_type == "claim.acquire" or (
            request.record_type == "claim.handoff"
            and request.payload.get("mode", "rotate") == "rotate"
        )
        if retains_recovery_proof:
            result["credential_recovery_event_id"] = request.event_id
        if not decision.accepted or not retains_recovery_proof:
            _authority_config.remove_pending_authority_credential(
                rollout.paths,
                event_id=durable.event_id,
            )
        if not decision.accepted:
            exit_code = 2
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(
            f"Authority request {result['request_event_id']}: {result['status']} "
            f"(origin sequence {result['origin_seq']})"
        )
        if generated_secret is not None:
            click.echo(
                "New proof retained in the private sidecar for recovery event "
                f"{request.event_id}."
            )
        if result.get("reason_code"):
            click.echo(f"Reason: {result['reason_code']}: {result.get('reason_detail')}", err=True)
    if exit_code:
        raise click.exceptions.Exit(exit_code)


@authority_commands.command("sync")
@click.option("--batch-size", default=100, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def authority_sync(obj, batch_size: int, as_json: bool) -> None:
    """Retry durable commands whose local proof sidecar is available."""
    rollout = _authority_rollout_status()
    if rollout.mode is not _authority_config.AuthorityCommandMode.ENFORCE:
        raise click.ClickException("authority sync requires enforce mode")
    store, _m = _get_store(obj)
    if obj["backend_config"].mode != "remote":
        raise click.ClickException("authority sync requires a remote PostgreSQL backend")
    store.authority_repo_uuid = _authority_repo_uuid(rollout.paths.repo_root)
    producer = _outbox.open_outbox(rollout.paths.outbox_path)
    cache = _projection.open_cached_projection(
        rollout.paths.state_dir / "authority-command-projection.db"
    )

    def resolve(record: _outbox.OutboxRecord) -> dict[str, str] | None:
        envelope = _contracts.record_from_dict(record.payload)
        required_refs = {
            value
            for key, value in envelope.payload.items()
            if key.endswith("credential_ref") and isinstance(value, str)
        }
        pending = _authority_config.load_pending_authority_credential(
            rollout.paths,
            event_id=record.event_id,
        )
        if pending is None:
            return None if required_refs else {}
        credentials = dict(pending.credentials)
        return credentials if required_refs <= set(credentials) else None

    try:
        result = _sync.synchronize_outbox(
            producer,
            store,
            cache,
            batch_size=batch_size,
            credential_resolver=resolve,
            apply_ingest_projection=False,
        )
        records_by_id = {
            record.event_id: record for record in _outbox.list_records(producer)
        }
    finally:
        producer.close()
        cache.close()
    for decision in result.command_decisions:
        record = records_by_id.get(decision.request_event_id)
        keep_for_recovery = (
            decision.accepted
            and record is not None
            and (
                record.event_type == "claim.acquire"
                or (
                    record.event_type == "claim.handoff"
                    and record.payload.get("payload", {}).get("mode") == "rotate"
                )
            )
        )
        if not keep_for_recovery:
            _authority_config.remove_pending_authority_credential(
                rollout.paths,
                event_id=decision.request_event_id,
            )
    payload = {
        "decisions": [decision.to_dict() for decision in result.command_decisions],
        "pending_command_event_ids": list(result.pending_command_event_ids),
        "decision_applied_count": result.decision_applied_count,
        "decision_watermark": (
            result.decision_watermark.ingest_offset if result.decision_watermark else 0
        ),
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(
            f"Authority sync: {len(payload['decisions'])} decisions, "
            f"{len(payload['pending_command_event_ids'])} pending; "
            f"decision watermark {payload['decision_watermark']}."
        )


@authority_commands.command("recover-proof")
@click.option("--event-id", required=True, help="Authority request UUID")
def authority_recover_proof(event_id: str) -> None:
    """Recover a private pre-minted proof after an accepted/lost response."""
    rollout = _authority_rollout_status()
    try:
        pending = _authority_config.load_pending_authority_credential(
            rollout.paths,
            event_id=event_id,
        )
    except _authority_config.AuthorityCommandConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if pending is None:
        raise click.ClickException(f"no pending authority proof for event {event_id}")
    try:
        secret = pending.secret
    except _authority_config.AuthorityCommandConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(secret)


@authority_commands.command("clear-proof")
@click.option("--event-id", required=True, help="Authority request UUID")
def authority_clear_proof(event_id: str) -> None:
    """Remove a private proof sidecar after the proof is stored elsewhere."""
    rollout = _authority_rollout_status()
    try:
        removed = _authority_config.remove_pending_authority_credential(
            rollout.paths,
            event_id=event_id,
        )
    except _authority_config.AuthorityCommandConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        raise click.ClickException(f"no pending authority proof for event {event_id}")
    click.echo(f"Removed pending authority proof for event {event_id}.")


# ---------------------------------------------------------------------------
# observation-only shadow pilot
# ---------------------------------------------------------------------------

@cli.group()
def pilot() -> None:
    """Operate the opt-in, observation-only shadow projection pilot."""


@pilot.command("status")
@click.option("--json", "as_json", is_flag=True, default=False)
def pilot_status(as_json: bool) -> None:
    """Show pilot opt-in state, local outbox size, and cached watermark."""
    try:
        payload = _pilot_status_payload()
    except _pilot.ShadowPilotConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Shadow pilot: {payload['state']}")
    click.echo(f"Outbox records: {payload['outbox_records'] if payload['outbox_records'] is not None else 0}")
    watermark = payload["watermark"]
    click.echo(
        "Remote watermark: "
        + (str(watermark["ingest_offset"]) if watermark is not None else "not synchronized")
    )


def _set_pilot_enabled(enabled: bool, *, as_json: bool) -> None:
    try:
        status = _pilot.set_shadow_pilot_enabled(enabled, cwd=Path.cwd())
    except _pilot.ShadowPilotConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    payload = status.to_dict()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Shadow pilot {payload['state']}.")


@pilot.command("enable")
@click.option("--json", "as_json", is_flag=True, default=False)
def pilot_enable(as_json: bool) -> None:
    """Explicitly opt this repository into observation-only shadow writes."""
    _set_pilot_enabled(True, as_json=as_json)


@pilot.command("disable")
@click.option("--json", "as_json", is_flag=True, default=False)
def pilot_disable(as_json: bool) -> None:
    """Stop future shadow writes without changing authority data."""
    _set_pilot_enabled(False, as_json=as_json)


@pilot.command("verify")
@click.option("--sprint-id", type=int, required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def pilot_verify(obj, sprint_id: int, as_json: bool) -> None:
    """Compare mirrored observations with current authoritative event history."""
    try:
        status = _pilot.shadow_pilot_status(cwd=Path.cwd())
    except _pilot.ShadowPilotConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if not status.enabled:
        click.echo("Error: shadow pilot is disabled; run 'sprintctl pilot enable' first.", err=True)
        sys.exit(1)
    store, m = _get_store(obj)
    config = obj["backend_config"]
    authoritative = [
        _shadow_source(envelope)
        for event in m.list_events(store, sprint_id)
        if (envelope := _shadow_observation_envelope(event, config.repo_id)) is not None
    ]
    producer = _outbox.open_outbox(status.paths.outbox_path)
    try:
        report = _shadow.compare_parity(authoritative, _outbox.list_records(producer))
    finally:
        producer.close()
    payload = {"sprint_id": sprint_id, **report.to_dict()}
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo("Shadow parity: " + ("equal" if report.is_equal else "diverged"))
        click.echo(json.dumps(report.counts, sort_keys=True))


@pilot.command("sync")
@click.option("--batch-size", default=100, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def pilot_sync(obj, batch_size: int, as_json: bool) -> None:
    """Synchronize the local observation outbox into the configured remote ledger."""
    try:
        status = _pilot.shadow_pilot_status(cwd=Path.cwd())
    except _pilot.ShadowPilotConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if not status.enabled:
        click.echo("Error: shadow pilot is disabled; run 'sprintctl pilot enable' first.", err=True)
        sys.exit(1)
    store, _m = _get_store(obj)
    if obj["backend_config"].mode != "remote":
        click.echo("Error: pilot synchronization requires a remote sprintctl backend.", err=True)
        sys.exit(1)
    producer = _outbox.open_outbox(status.paths.outbox_path)
    cache = _projection.open_cached_projection(status.paths.projection_path)
    try:
        result = _sync.synchronize_outbox(producer, store, cache, batch_size=batch_size)
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    finally:
        producer.close()
        cache.close()
    payload = {
        "uploaded": len(result.uploaded),
        "duplicates": sum(outcome.duplicate for outcome in result.uploaded),
        "applied_count": result.applied_count,
        "watermark": {
            "ingest_offset": result.watermark.ingest_offset,
            "advanced_at": result.watermark.advanced_at,
        },
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Synchronized {payload['uploaded']} observation records; watermark {result.watermark.ingest_offset}.")


@pilot.command("cutover-evidence")
@click.option(
    "--sprint-id",
    type=int,
    default=None,
    help="Sprint ID to compute parity evidence for (defaults to active).",
)
@click.option(
    "--skip-parity",
    is_flag=True,
    default=False,
    help="Omit parity computation (e.g. before the pilot has ever synchronized).",
)
@click.option(
    "--max-watermark-age-seconds",
    type=int,
    default=_cutover.DEFAULT_MAX_WATERMARK_AGE_SECONDS,
    show_default=True,
    help="Reconciliation-lag bound the promotion gate checks the cached watermark against.",
)
@click.option(
    "--skip-rollback-rehearsal",
    is_flag=True,
    default=False,
    help="Skip the rollback round-trip rehearsal (not recommended before a promotion decision).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def pilot_cutover_evidence(
    obj, sprint_id, skip_parity, max_watermark_age_seconds, skip_rollback_rehearsal, as_json
) -> None:
    """Assemble per-repo authority + projection cutover dogfood evidence.

    Combines shadow-pilot parity, cached-projection watermark/reconciliation
    lag, sprintctl-doctor stale-tool-incident findings, and a rollback
    round-trip rehearsal into one evidence packet with an explicit
    promotion gate (``promotable`` + ``blockers``). This never performs a
    fleet cutover, never deletes a backend, and never itself promotes a
    repository -- it only assembles evidence for an operator-directed
    decision. See docs/reference/cutover-dogfood.md.
    """
    parity_payload = None
    if not skip_parity:
        try:
            status = _pilot.shadow_pilot_status(cwd=Path.cwd())
        except _pilot.ShadowPilotConfigError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        if status.enabled:
            store, m = _get_store(obj)
            config = obj["backend_config"]
            if sprint_id is not None:
                s = m.get_sprint(store, sprint_id)
                if s is None:
                    click.echo(f"Sprint #{sprint_id} not found.", err=True)
                    sys.exit(1)
            else:
                s = _resolve_implicit_sprint(store, m=m)
            if s is not None:
                authoritative = [
                    _shadow_source(envelope)
                    for event in m.list_events(store, s["id"])
                    if (envelope := _shadow_observation_envelope(event, config.repo_id)) is not None
                ]
                producer = _outbox.open_outbox(status.paths.outbox_path)
                try:
                    report = _shadow.compare_parity(authoritative, _outbox.list_records(producer))
                finally:
                    producer.close()
                parity_payload = report.to_dict()

    try:
        payload = _cutover.build_cutover_evidence(
            cwd=Path.cwd(),
            parity=parity_payload,
            max_watermark_age_seconds=max_watermark_age_seconds,
            rehearse=not skip_rollback_rehearsal,
        )
    except _cutover.CutoverEvidenceError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Cutover dogfood evidence (contract v{payload['contract_version']}):")
    cfg = payload["config"]
    click.echo(
        f"  Config: pilot={cfg['pilot_enabled']} authority_mode={cfg['authority_command_mode']} "
        f"projection_reads={cfg['projection_reads_enabled']}"
    )
    if payload["parity"] is not None:
        click.echo(
            "  Parity: "
            + ("equal" if payload["parity"]["is_equal"] else "diverged")
            + f" {payload['parity']['counts']}"
        )
    else:
        click.echo("  Parity: not evaluated")
    watermark = payload["watermark"]
    click.echo(
        f"  Watermark: healthy={watermark.get('healthy')} age={watermark.get('age_seconds')} "
        f"(max {watermark.get('max_age_seconds')}s)"
    )
    click.echo(f"  Stale-tool incidents: {len(payload['stale_tools']['incidents'])}")
    if payload["rollback_rehearsal"] is not None:
        rehearsal_ok = payload["rollback_rehearsal"]["rollback_ok"]
        click.echo(f"  Rollback rehearsal: {'ok' if rehearsal_ok else 'FAILED'}")
    else:
        click.echo("  Rollback rehearsal: skipped")
    click.echo(f"  Promotable: {payload['promotable']}")
    if payload["blockers"]:
        click.echo("  Blockers: " + ", ".join(payload["blockers"]))


# ---------------------------------------------------------------------------
# guarded projection-backed reads: per-repo operator toggle
# ---------------------------------------------------------------------------

@cli.group("projection-reads")
def projection_reads_group() -> None:
    """Operate the opt-in, guarded projection-backed read path.

    When enabled, some CLI read surfaces (currently `item show`'s event
    history) are served from the cached projection populated by
    `sprintctl pilot sync` instead of backend, with explicit freshness
    disclosure and automatic fallback to backend whenever the cache is
    missing, stale, on an old schema, or never synchronized. Disabling this
    (or leaving it disabled, the default) returns all reads to the current
    backend-only behavior -- this is the rollback path.
    """


@projection_reads_group.command("status")
@click.option("--json", "as_json", is_flag=True, default=False)
def projection_reads_status_cmd(as_json: bool) -> None:
    """Show whether projection reads are enabled and the cache's freshness."""
    try:
        reads_status = _projection_reads.projection_reads_status(cwd=Path.cwd())
    except _projection_reads.ProjectionReadsConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    health = _projection_health()
    payload = reads_status.to_dict()
    payload["health"] = health["health"]
    payload["watermark_offset"] = health["watermark_offset"]
    payload["watermark_age_seconds"] = health["watermark_age_seconds"]
    payload["schema_version"] = health["schema_version"]
    payload["stale_after_seconds"] = health["stale_after_seconds"]
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(f"Projection reads: {'enabled' if payload['enabled'] else 'disabled'} (source={payload['source']})")
    click.echo(f"Cache health: {payload['health']}")
    if payload["watermark_offset"] is not None:
        age = payload["watermark_age_seconds"]
        age_text = f"{age:.0f}s" if age is not None else "unknown"
        click.echo(f"Watermark: offset={payload['watermark_offset']} age={age_text}")


def _set_projection_reads_enabled(enabled: bool, *, as_json: bool) -> None:
    try:
        status = _projection_reads.set_projection_reads_enabled(enabled, cwd=Path.cwd())
    except _projection_reads.ProjectionReadsConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    payload = status.to_dict()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Projection reads {'enabled' if payload['enabled'] else 'disabled'}.")


@projection_reads_group.command("enable")
@click.option("--json", "as_json", is_flag=True, default=False)
def projection_reads_enable(as_json: bool) -> None:
    """Opt this repository into guarded projection-backed reads."""
    _set_projection_reads_enabled(True, as_json=as_json)


@projection_reads_group.command("disable")
@click.option("--json", "as_json", is_flag=True, default=False)
def projection_reads_disable(as_json: bool) -> None:
    """Rollback: return every read surface to backend-only reads."""
    _set_projection_reads_enabled(False, as_json=as_json)


@event.command("list")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID")
@click.option("--item-id", "work_item_id", type=int, default=None, help="Filter by work item ID")
@click.option("--type", "event_type", default=None, help="Filter by event type")
@click.option("--knowledge", "knowledge_only", is_flag=True, default=False,
              help="Show only knowledge candidate events (decision, pattern-noted, lesson-learned, risk-accepted)")
@click.option("--limit", default=None, type=int, help="Maximum number of events to return (most recent)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def event_list(obj, sprint_id, work_item_id, event_type, knowledge_only, limit, as_json) -> None:
    """List events for a sprint."""
    if knowledge_only and event_type is not None:
        click.echo("Error: --knowledge and --type are mutually exclusive.", err=True)
        sys.exit(1)
    store, m = _get_store(obj)
    if m.get_sprint(store, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    if knowledge_only:
        events = m.list_knowledge_candidates(store, sprint_id)
        # list_knowledge_candidates already deserializes payload; re-serialize for JSON output
        if work_item_id is not None:
            events = [e for e in events if e.get("work_item_id") == work_item_id]
        if limit is not None:
            events = events[-limit:]
        if as_json:
            click.echo(json.dumps(events, indent=2))
            return
        if not events:
            click.echo("No events found.")
            return
        for e in events:
            item_label = f"  item #{e['work_item_id']}" if e.get("work_item_id") else ""
            click.echo(
                f"#{e['id']}  [{e['event_type']}]  {e['actor']}  "
                f"{e['created_at']}{item_label}"
            )
        return
    events = m.list_events(store, sprint_id)
    if work_item_id is not None:
        events = [e for e in events if e.get("work_item_id") == work_item_id]
    if event_type is not None:
        events = [e for e in events if e.get("event_type") == event_type]
    if limit is not None:
        events = events[-limit:]
    if as_json:
        click.echo(json.dumps(events, indent=2))
        return
    if not events:
        click.echo("No events found.")
        return
    for e in events:
        item_label = f"  item #{e['work_item_id']}" if e.get("work_item_id") else ""
        click.echo(
            f"#{e['id']}  [{e['event_type']}]  {e['actor']}  "
            f"{e['created_at']}{item_label}"
        )


# ---------------------------------------------------------------------------
# takeup
# ---------------------------------------------------------------------------

@cli.group()
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
# maintain
# ---------------------------------------------------------------------------

@cli.group()
def maintain() -> None:
    """Maintenance commands (check, sweep, carryover)."""


@cli.group("db")
def db_group() -> None:
    """Database maintenance (vacuum, integrity)."""


@db_group.command("vacuum")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def db_vacuum(obj, as_json) -> None:
    """Reclaim space: VACUUM the local sqlite DB or the remote repo tables."""
    store, m = _get_store(obj)
    report = m.vacuum_database(store)
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return
    if report["backend"] == "local":
        click.echo(
            f"VACUUM complete: {report['pages_before']} -> {report['pages_after']} pages"
            f" ({report['bytes_reclaimed']} bytes reclaimed)."
        )
    else:
        click.echo(
            "VACUUM (ANALYZE) complete on: " + ", ".join(report["vacuumed_tables"])
        )


@db_group.command("integrity")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def db_integrity(obj, as_json) -> None:
    """Check structural integrity and referential consistency (read-only)."""
    store, m = _get_store(obj)
    report = m.check_integrity(store)
    if as_json:
        click.echo(json.dumps(report, indent=2))
        if not report["ok"]:
            sys.exit(1)
        return
    status = "ok" if report["ok"] else "FAILED"
    click.echo(f"Integrity: {status}")
    for line in report["integrity_check"]:
        if line != "ok":
            click.echo(f"  {line}")
    for violation in report["foreign_key_violations"]:
        click.echo(f"  fk violation: {violation}")
    counts = ", ".join(f"{t}={n}" for t, n in report["table_counts"].items())
    click.echo(f"Rows: {counts}")
    if not report["ok"]:
        sys.exit(1)


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


def _collect_next_work_explained_payload(
    *,
    conn,
    sprint: dict,
    ready_items: list[dict],
    now: datetime,
    m=None,
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
        "sprintctl claim resume --json",
    ]
    claimed_item_refs = m.list_refs_for_items(
        conn, [claim["work_item_id"] for claim in context["active_claims"]]
    )
    claim_recovery = {
        "current_identity": {
            "runtime_session_id": current_runtime_session_id,
            "instance_id": current_instance_id,
        },
        "active_claims": [
            {
                **_claim_recovery_status(
                    claim,
                    current_runtime_session_id=current_runtime_session_id,
                    current_instance_id=current_instance_id,
                ),
                "refs": claimed_item_refs.get(claim["work_item_id"], []),
            }
            for claim in context["active_claims"]
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
        "claim_recovery": claim_recovery,
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
    claim_recovery = payload.get("claim_recovery", {})
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
    lines.append("Claim recovery:")
    recovery_claims = claim_recovery.get("active_claims", [])
    if not recovery_claims:
        lines.append("  (no active claims)")
    else:
        for claim in recovery_claims:
            lines.append(
                f"  Claim #{claim['claim_id']} item #{claim['work_item_id']}: "
                f"local_token={'yes' if claim['recovery_token_exists'] else 'no'} "
                f"identity_match={'yes' if claim['plausible_identity_match'] else 'no'}"
            )
            lines.append(f"    path: {claim['recovery_token_path']}")
            refs = claim.get("refs", [])
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


def _recommended_commands_for_next_action(*, sprint_id: int, next_action: dict) -> list[str]:
    kind = next_action.get("kind")
    item_id = next_action.get("item_id")
    claim_id = next_action.get("claim_id")
    blocker_id = next_action.get("blocker_item_id")

    if kind == "resolve-claim-identity":
        commands = [
            f"sprintctl claim resume --sprint-id {sprint_id} --json",
        ]
        if claim_id is not None:
            commands.append(
                f"sprintctl claim handoff --id {claim_id} --actor <name> --mode rotate --allow-legacy-adopt --json"
            )
        return commands

    if kind == "refresh-claim":
        commands = []
        if claim_id is not None:
            commands.append(
                f"sprintctl claim heartbeat --id {claim_id} --claim-token <token> --ttl 600 --actor <name>"
            )
            commands.append(
                f"sprintctl claim handoff --id {claim_id} --claim-token <token> --actor <next-agent> --mode rotate --json"
            )
        return commands

    if kind in {"unblock-dependent-work", "resolve-blocker"}:
        commands = []
        if blocker_id is not None:
            commands.append(f"sprintctl item show --id {blocker_id}")
        if item_id is not None:
            commands.append(f"sprintctl item show --id {item_id}")
        commands.append(f"sprintctl next-work --sprint-id {sprint_id} --json --explain")
        return commands

    if kind == "inspect-active-claim":
        commands = []
        if item_id is not None:
            commands.append(f"sprintctl item show --id {item_id}")
        if claim_id is not None:
            commands.append(
                f"sprintctl claim heartbeat --id {claim_id} --claim-token <token> --ttl 600 --actor <name>"
            )
            commands.append(
                f"sprintctl claim handoff --id {claim_id} --claim-token <token> --actor <next-agent> --mode rotate --json"
            )
        return commands

    if kind == "resume-unclaimed-active-item":
        commands = []
        if item_id is not None:
            commands.extend(
                [
                    f"sprintctl claim start --item-id {item_id} --actor <name> --ttl 600 --json",
                    f"sprintctl item show --id {item_id}",
                ]
            )
        return commands

    if kind == "start-ready-item":
        commands = []
        if item_id is not None:
            commands.extend(
                [
                    f"sprintctl claim start --item-id {item_id} --actor <name> --ttl 600 --json",
                    f"sprintctl item show --id {item_id}",
                ]
            )
        return commands

    if kind in {"triage-blocked-item", "refresh-stale-item"}:
        if item_id is None:
            return []
        return [f"sprintctl item show --id {item_id}"]

    if kind == "no-action":
        return [
            f"sprintctl usage --context --sprint-id {sprint_id} --json",
            f"sprintctl next-work --sprint-id {sprint_id} --json --explain",
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
    if command.startswith("sprintctl claim start"):
        return "claim-start"
    if command.startswith("sprintctl claim resume"):
        return "claim-resume"
    if command.startswith("sprintctl claim heartbeat"):
        return "claim-heartbeat"
    if command.startswith("sprintctl claim handoff"):
        return "claim-handoff"
    if command.startswith("sprintctl item show"):
        return "item-show"
    if command.startswith("sprintctl usage --context"):
        return "usage-context"
    if command.startswith("sprintctl next-work"):
        return "next-work"
    return "other"


def _collect_context_contract(conn, sprint: dict, now: datetime, *, m=None) -> dict:
    m = m or _db
    active_claims = m.list_claims_by_sprint(conn, sprint["id"], active_only=True)
    report = _maintain.check(conn, sprint["id"], now, _m=m)
    stale_items = [
        {
            "id": item["id"],
            "title": item["title"],
            "status": item["status"],
            "track": item["track_name"],
            "idle_seconds": item["idle_seconds"],
        }
        for item in report["stale_items"]
    ]

    all_items = m.list_work_items(conn, sprint_id=sprint["id"])
    blocked_items = [
        {"id": item["id"], "title": item["title"], "track": item["track_name"]}
        for item in all_items
        if item["status"] == "blocked"
    ]
    active_items = [
        {"id": item["id"], "title": item["title"], "track": item["track_name"]}
        for item in all_items
        if item["status"] == "active"
    ]
    active_unclaimed_items = _active_items_without_claims(active_items, active_claims)
    ready_items = [
        {
            "id": item["id"],
            "title": item["title"],
            "track": item["track_name"],
        }
        for item in m.get_ready_items(conn, sprint["id"])
    ]
    dependency_waiting_items = _dependency_waiting_items(conn, sprint["id"], m=m)
    knowledge = m.list_knowledge_candidates(conn, sprint["id"])
    recent_decisions = [_summarize_event(event) for event in reversed(knowledge[-5:])]
    conflicts = _derive_conflicts(
        active_claims=active_claims,
        active_unclaimed_items=active_unclaimed_items,
        blocked_items=blocked_items,
        stale_items=stale_items,
        dependency_waiting_items=dependency_waiting_items,
        now=now,
    )
    conflicts.extend(
        finding
        for finding in report["findings"]
        if finding["reason_code"] != "active-item-without-live-claim"
    )
    next_action = _derive_next_action(
        active_claims=active_claims,
        active_unclaimed_items=active_unclaimed_items,
        conflicts=conflicts,
        ready_items=ready_items,
        blocked_items=blocked_items,
        stale_items=stale_items,
        dependency_waiting_items=dependency_waiting_items,
    )

    done_items = [item for item in all_items if item["status"] == "done"]
    pending_items = [item for item in all_items if item["status"] == "pending"]

    return _contracts.ContextContract(
        sprint={
            "id": sprint["id"],
            "name": sprint["name"],
            "goal": sprint["goal"],
            "status": sprint["status"],
            "start_date": sprint.get("start_date"),
            "end_date": sprint.get("end_date"),
        },
        summary={
            "total": len(all_items),
            "done": len(done_items),
            "active": len(active_items),
            "pending": len(pending_items),
            "blocked": len(blocked_items),
            "stale": len(stale_items),
            "ready": len(ready_items),
            "waiting_on_dependencies": len(dependency_waiting_items),
            "active_claims": len(active_claims),
            "active_unclaimed": len(active_unclaimed_items),
        },
        active_claims=active_claims,
        active_unclaimed_items=active_unclaimed_items,
        conflicts=conflicts,
        ready_items=ready_items,
        blocked_items=blocked_items,
        stale_items=stale_items,
        recent_decisions=recent_decisions,
        next_action=next_action,
    ).to_dict()


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

    active_claims = snapshot["active_claims"]
    lines.append(f"Active claims ({len(active_claims)}):")
    if active_claims:
        for claim in active_claims:
            item_title = claim.get("item_title") or f"item #{claim['work_item_id']}"
            lines.append(
                f"  claim #{claim['claim_id']}  [{claim['actor']}]  {item_title}  "
                f"expires: {claim['expires_at']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    active_unclaimed_items = snapshot["active_unclaimed_items"]
    lines.append(f"Active items without claims ({len(active_unclaimed_items)}):")
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
    m = m or _db
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    context = _collect_context_contract(conn, sprint, now, m=m)
    items = m.list_work_items(conn, sprint_id=sprint["id"])
    items_with_refs = []
    for item in items:
        enriched = {**item}
        refs = m.list_refs(conn, item["id"])
        if refs:
            enriched["refs"] = refs
        items_with_refs.append(enriched)

    recent_events = m.list_events_limited(conn, sprint["id"], limit=events_limit)
    all_events = m.list_events(conn, sprint["id"])
    active_items = [
        {"id": item["id"], "title": item["title"], "track": item["track_name"]}
        for item in items
        if item["status"] == "active"
    ]
    previous_handoff = _previous_handoff_generated(conn, sprint["id"], m=m)
    git_context = _detect_git_context()

    return _contracts.HandoffBundle(
        sprintctl_version=__version__,
        generated_at=generated_at,
        generated_from={
            "command": "sprintctl handoff",
            "events_limit": events_limit,
        },
        sprint=dict(sprint),
        summary=context["summary"],
        active_claims=context["active_claims"],
        conflicts=context["conflicts"],
        work={
            "active_items": active_items,
            "active_unclaimed_items": context["active_unclaimed_items"],
            "ready_items": context["ready_items"],
            "blocked_items": context["blocked_items"],
            "stale_items": context["stale_items"],
        },
        recent_decisions=context["recent_decisions"],
        recent_events=[_summarize_event(event) for event in recent_events],
        next_action=context["next_action"],
        delta_since_last_handoff=_build_delta_since_last_handoff(
            previous_handoff=previous_handoff,
            items=items_with_refs,
            all_events=all_events,
            active_claims=context["active_claims"],
        ),
        freshness={
            "generated_at": generated_at,
            "previous_handoff_at": previous_handoff["created_at"] if previous_handoff else None,
            "stale_item_count": len(context["stale_items"]),
            "active_claim_count": len(context["active_claims"]),
            "dirty_file_count": len(git_context["dirty_files"]) if git_context else 0,
        },
        evidence={
            "dirty_files": git_context["dirty_files"] if git_context else [],
            "items_with_refs": sum(1 for item in items_with_refs if item.get("refs")),
            "total_refs": sum(len(item.get("refs", [])) for item in items_with_refs),
            "recent_event_count": len(recent_events),
            "recent_decision_count": len(context["recent_decisions"]),
            "validation_outcomes": [],
        },
        git_context=git_context,
        claim_identity_model={
            "ownership_proof": "claim_id+claim_token",
            "claim_tokens_included": False,
            "ambiguous_identity_visible": True,
            "explicit_claim_handoff_command": "sprintctl claim handoff",
        },
        resume_instructions=[
            "Read this handoff bundle first.",
            "Refresh live state with 'sprintctl usage --context --json'.",
            "Inspect the target item with 'sprintctl item show --id <id> --json' if more detail is needed.",
            "Use 'sprintctl claim resume' to locate transferred claims before claiming new work.",
        ],
        agent_shutdown_protocol={
            "required_before_termination": [
                "For each active claim you own: run 'sprintctl claim handoff --id <id> --claim-token <token> --actor <next-agent> --mode rotate' to pass ownership to the incoming session.",
                "If no incoming session: run 'sprintctl claim release --id <id> --claim-token <token>' to free each claim.",
                "If handing off the sprint: run 'sprintctl handoff' to produce a new bundle for the next agent.",
            ],
            "resumption_hint": (
                "Incoming agents: use 'sprintctl claim resume --instance-id <id>' or "
                "'--runtime-session-id <id>' to locate claims transferred to you."
            ),
        },
        items=items_with_refs,
        events=recent_events,
    ).to_dict()


def _record_handoff_generated(conn, sprint_id: int, bundle: dict, *, m=None) -> None:
    m = m or _db
    m.create_event(
        conn,
        sprint_id=sprint_id,
        actor="handoff",
        event_type="handoff-generated",
        source_type="system",
        payload={
            "summary": f"Handoff bundle generated for sprint #{sprint_id}",
            "detail": "Generated a working-memory handoff bundle for the next session.",
            "bundle_version": bundle["bundle_version"],
            "events_limit": bundle["generated_from"]["events_limit"],
        },
    )


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
# render
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

@cli.command("export")
@click.option("--sprint-id", type=int, required=True, help="Sprint ID to export")
@click.option("--output", "output_path", default=None, help="Output file path (default: sprint-N.json)")
@click.pass_obj
def export_cmd(obj, sprint_id, output_path) -> None:
    """Export a sprint (sprint, tracks, items, events) to a JSON file."""
    conn = _get_conn(obj)
    sprint = _db.get_sprint(conn, sprint_id)
    if sprint is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    tracks = _db.list_tracks(conn, sprint_id)
    items = _db.list_work_items(conn, sprint_id=sprint_id)
    events = _db.list_events(conn, sprint_id)
    refs_by_item: dict[int, list[dict]] = {}
    for it in items:
        item_refs = _db.list_refs(conn, it["id"])
        if item_refs:
            refs_by_item[it["id"]] = item_refs
    envelope = {
        "sprintctl_version": __version__,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sprint": dict(sprint),
        "tracks": [dict(t) for t in tracks],
        "items": [dict(it) for it in items],
        "events": [dict(e) for e in events],
        "refs": refs_by_item,
    }
    dest = output_path or f"sprint-{sprint_id}.json"
    with open(dest, "w") as fh:
        json.dump(envelope, fh, indent=2)
    click.echo(f"Exported sprint #{sprint_id} to {dest}")


@cli.command("import")
@click.option("--file", "input_path", required=True, help="Path to exported sprint JSON file")
@click.pass_obj
def import_cmd(obj, input_path) -> None:
    """Import a sprint from a JSON export file (re-sequences all IDs)."""
    conn = _get_conn(obj)
    try:
        with open(input_path) as fh:
            envelope = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"Failed to read {input_path}: {e}", err=True)
        sys.exit(1)

    src_sprint = envelope["sprint"]

    # Pre-flight: all track_ids referenced by items must be present in tracks list.
    # Validate before writing anything so the DB is never left in a partial state.
    exported_track_ids = {t["id"] for t in envelope.get("tracks", [])}
    missing: list[str] = []
    for it in envelope.get("items", []):
        if it["track_id"] not in exported_track_ids:
            missing.append(f"  item '{it['title']}' references track_id {it['track_id']} not found in export")
    if missing:
        click.echo("Import aborted — items reference tracks missing from the export file:", err=True)
        for m in missing:
            click.echo(m, err=True)
        sys.exit(1)

    # Validate and prepare every event before creating any rows. Local-authority
    # events are retained under explicit imported event types rather than replayed.
    prepared_events: list[dict] = []
    imported_history_count = 0
    try:
        for ev in envelope.get("events", []):
            try:
                payload = json.loads(ev.get("payload", "{}"))
            except (json.JSONDecodeError, TypeError):
                payload = {}
            event_type = ev["event_type"]
            source_event_id = ev["id"]
            archive_only = _contracts.requires_archive_import_handling(event_type)
            if archive_only:
                _contracts.canonicalize_event_for_archive_import(
                    event_type,
                    payload,
                    source_event_id,
                )
                imported_history_count += 1
            else:
                payload["source_id"] = source_event_id
                payload = _contracts.canonicalize_event_payload(event_type, payload)
            prepared_events.append({
                "event": ev,
                "payload": payload,
                "archive_only": archive_only,
            })
    except (KeyError, TypeError, ValueError) as exc:
        click.echo(f"Import aborted — invalid event history: {exc}", err=True)
        sys.exit(1)

    new_sprint_id = _db.create_sprint(
        conn,
        name=src_sprint["name"],
        goal=src_sprint.get("goal", ""),
        start_date=src_sprint.get("start_date"),
        end_date=src_sprint.get("end_date"),
        status=src_sprint.get("status", "planned"),
        kind=src_sprint.get("kind", "active_sprint"),
    )

    # Map old track IDs → new track IDs
    track_id_map: dict[int, int] = {}
    for t in envelope.get("tracks", []):
        new_tid = _db.get_or_create_track(conn, new_sprint_id, t["name"], t.get("description", ""))
        track_id_map[t["id"]] = new_tid

    # Map old item IDs → new item IDs
    item_id_map: dict[int, int] = {}
    for it in envelope.get("items", []):
        new_track_id = track_id_map[it["track_id"]]  # guaranteed present after pre-flight
        new_iid = _db.create_work_item(
            conn,
            new_sprint_id,
            new_track_id,
            it["title"],
            description=it.get("description", ""),
            assignee=it.get("assignee"),
        )
        # Restore status via raw update (bypasses transition guard for import)
        imported_status = it.get("status", "pending")
        if imported_status != "pending":
            conn.execute(
                "UPDATE work_item SET status = ?, updated_at = ? WHERE id = ?",
                (imported_status, it.get("updated_at", it.get("created_at")), new_iid),
            )
        item_id_map[it["id"]] = new_iid

    # Re-insert generic events with source_id. Reserved typed events use an
    # explicit non-authoritative imported representation with an exact wrapper.
    for prepared in prepared_events:
        ev = prepared["event"]
        old_item_id = ev.get("work_item_id")
        new_item_id = item_id_map.get(old_item_id) if old_item_id is not None else None
        if prepared["archive_only"]:
            _db.create_archive_import_event(
                conn,
                new_sprint_id,
                actor=ev["actor"],
                event_type=ev["event_type"],
                source_event_id=ev["id"],
                work_item_id=new_item_id,
                payload=prepared["payload"],
            )
        else:
            _db.create_event(
                conn,
                new_sprint_id,
                actor=ev["actor"],
                event_type=ev["event_type"],
                source_type=ev.get("source_type", "system"),
                work_item_id=new_item_id,
                payload=prepared["payload"],
            )

    # Re-insert refs, remapping old item IDs to new item IDs
    refs_by_item = envelope.get("refs", {})
    for old_item_id_str, item_refs in refs_by_item.items():
        new_item_id = item_id_map.get(int(old_item_id_str))
        if new_item_id is None:
            continue
        for r in item_refs:
            _db.add_ref(conn, new_item_id, r["ref_type"], r["url"], r.get("label", ""))

    conn.commit()
    click.echo(
        f"Imported sprint '{src_sprint['name']}' as #{new_sprint_id} "
        f"({len(item_id_map)} items, {len(envelope.get('events', []))} events)"
    )
    if imported_history_count:
        click.echo(
            f"Retained {imported_history_count} reserved typed event(s) as "
            "non-authoritative imported history."
        )


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

@cli.group()
def claim() -> None:
    """Manage agent claims on work items."""


@claim.command("create")
@click.option("--item-id", type=int, required=True, help="Work item ID to claim")
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
    item_id,
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


@claim.command("start")
@click.option("--item-id", type=int, required=True, help="Work item ID to claim and move to active")
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
    item_id,
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


@claim.command("release")
@click.option("--id", "claim_id", type=int, required=True, help="Claim ID")
@click.option("--claim-token", required=True, help="Claim token returned when the claim was created")
@click.option("--actor", "--agent", "actor", default=None, help="Actor identifier (advisory metadata only)")
@click.pass_obj
def claim_release(obj, claim_id, claim_token, actor) -> None:
    """Release (delete) a claim."""
    store, m = _get_store(obj)
    try:
        m.release_claim(store, claim_id, claim_token, actor=actor)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    _remove_claim_recovery_record(claim_id)
    click.echo(f"Claim #{claim_id} released.")


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
@click.option("--item-id", type=int, required=True, help="Work item ID")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired claims")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_list(obj, item_id, show_all, as_json) -> None:
    """List claims on a work item."""
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
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired claims")
@click.option(
    "--expiring-within", "expiring_within", type=int, default=None,
    help="Only show claims expiring within N seconds",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_list_sprint(obj, sprint_id, show_all, expiring_within, as_json) -> None:
    """List all claims across a sprint, optionally filtered by expiry window."""
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
@click.option("--claim-token", required=True, help="Claim token (proves ownership; re-displays the token)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_show(obj, claim_id, claim_token, as_json) -> None:
    """Show a claim and re-display its token (useful after context loss).

    Requires the current claim_token to prove ownership before revealing it again.
    """
    store, m = _get_store(obj)
    claim = m.get_claim(store, claim_id, include_secret=True)
    if claim is None:
        click.echo(f"Error: Claim #{claim_id} not found", err=True)
        sys.exit(1)
    try:
        from .db import _require_claim_proof
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
@click.option("--item-id", type=int, default=None, help="Filter results to a specific work item")
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


@claim.command("recover")
@click.option("--id", "claim_id", type=int, default=None, help="Claim ID to recover")
@click.option("--item-id", type=int, default=None, help="Recover the only active claim for a work item")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def claim_recover(obj, claim_id, item_id, as_json) -> None:
    """Recover a claim token from sprintctl's local recovery record."""
    if (claim_id is None) == (item_id is None):
        click.echo("Error: Provide exactly one of --id or --item-id", err=True)
        sys.exit(1)
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
    claims = bundle["active_claims"]
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

    if claims:
        lines.append(f"ACTIVE CLAIMS ({len(claims)}):")
        for c in claims:
            excl = "exclusive" if c["exclusive"] else "shared"
            lines.append(
                f"  #{c['claim_id']}  item #{c['work_item_id']} ({c.get('item_title', '')})  "
                f"{c['actor']}  [{c['claim_type']}]  {excl}  expires={c['expires_at']}"
            )
        lines.append("")
        lines.append("NOTE: Incoming agent must claim handoff or release each active claim.")
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


@cli.command("handoff")
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


@cli.command("agent-protocol")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def agent_protocol_cmd(as_json) -> None:
    """Print the claim lifecycle protocol for agent consumption.

    Outputs a structured summary of how agents should interact with sprintctl
    claims: startup, heartbeat, handoff, and shutdown steps. Suitable for
    injecting into an agent system prompt or reading programmatically.
    """
    protocol = {
        "sprintctl_agent_protocol_version": "1",
        "claim_model": {
            "ownership_proof": (
                "claim_id + claim_token (both required for claim operations; sprintctl can also "
                "persist a local recovery copy of the token for context-loss recovery)"
            ),
            "ttl_seconds_default": 300,
            "claim_types": {
                "execute": "Exclusive. Agent is implementing work on the item.",
                "inspect": "Exclusive. Agent is reading item state.",
                "review": "Exclusive. Agent is reviewing completed work.",
                "coordinate": "Exclusive. Orchestrator managing sub-agents. Sub-agents may claim execute under it.",
            },
        },
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
            "proof_note": "Takeup has no TTL, heartbeat, or claim token. Claims remain the exclusive ownership mechanism.",
        },
        "lifecycle": {
            "1_startup": {
                "description": "Claim the item before beginning work.",
                "command": (
                    "sprintctl claim start --item-id <id> --actor <name> "
                    "[--ttl <seconds>] [--runtime-session-id <env-session-id>] "
                    "[--instance-id <stable-per-process-uuid>] [--branch <branch>] --json"
                ),
                "store": (
                    "Save claim_id for the session. sprintctl also writes a local recovery token file "
                    "next to the active database so 'claim recover' can restore the secret after context loss. "
                    "Treat claim_token as a secret."
                ),
                "coordinator_note": (
                    "If acting as an orchestrator, use "
                    "'sprintctl claim create --item-id <id> --actor <name> --type coordinate --json' first, "
                    "then spawn sub-agents "
                    "that call 'claim create' with --coordinate-claim-id and --coordinate-claim-token."
                ),
            },
            "2_heartbeat": {
                "description": "Refresh the claim TTL periodically (every ~half the TTL).",
                "command": (
                    "sprintctl claim heartbeat --id <claim_id> --claim-token <token> "
                    "[--ttl <seconds>] [--actor <name>]"
                ),
                "frequency": "Every 120s if TTL=300s. Increase --ttl for long-running tasks.",
            },
            "3_status_transition": {
                "description": "Transition item status. Claim proof is required.",
                "command": (
                    "sprintctl item status --id <item_id> --status active|done|blocked "
                    "--actor <name> --claim-id <claim_id> --claim-token <token>"
                ),
            },
            "4_handoff": {
                "description": "Pass claim ownership to an incoming agent session (required on shutdown if work continues).",
                "command": (
                    "sprintctl claim handoff --id <claim_id> --claim-token <token> "
                    "--actor <next-agent-name> --mode rotate "
                    "[--runtime-session-id <next-session-id>] [--instance-id <next-instance-id>] --json"
                ),
                "note": "The returned claim_token is the new agent's secret. The old token is invalidated.",
            },
            "5_release": {
                "description": "Release the claim when work is complete and no handoff is needed.",
                "command": "sprintctl claim release --id <claim_id> --claim-token <token> --actor <name>",
            },
        },
        "session_resumption": {
            "description": "If context is lost, locate your claims by identity before re-claiming.",
            "command": (
                "sprintctl claim resume --instance-id <your-instance-id> "
                "[--runtime-session-id <id>] [--hostname <host> --pid <pid>] --json"
            ),
            "recovery": (
                "Use 'claim recover --id <id>' or '--item-id <id>' to restore a token from sprintctl's local "
                "recovery file. If no local recovery file exists and the claim is legacy/ambiguous, use "
                "'claim handoff --allow-legacy-adopt' to mint a fresh proof."
            ),
        },
        "shutdown_checklist": [
            "For each owned claim: handoff to next agent OR release.",
            "Run 'sprintctl handoff' to write a bundle for the incoming session.",
        ],
        "environment_hints": {
            "SPRINTCTL_RUNTIME_SESSION_ID": "Set to your runtime session ID (auto-detected from CODEX_THREAD_ID).",
            "SPRINTCTL_INSTANCE_ID": "Set to a stable per-process UUID; persisted across heartbeats.",
            "SPRINTCTL_DB": "Override the database path (default: ~/.sprintctl/sprintctl.db).",
        },
    }
    if as_json:
        click.echo(json.dumps(protocol, indent=2))
        return

    click.echo("=== sprintctl Agent Claim Protocol ===\n")
    click.echo(f"Ownership proof: {protocol['claim_model']['ownership_proof']}\n")
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


@cli.command("next-work")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
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


@cli.command("context-candidates")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option(
    "--item-id",
    "explicit_item_id",
    type=int,
    default=None,
    help="Explicit item reference (rank 1). Only this rank is ever claim_eligible.",
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

    explicit_item = None
    if explicit_item_id is not None:
        explicit_item = m.get_work_item(store, explicit_item_id)

    # next-work-style read: current item/dependency state is never mirrored
    # into the cached projection (only observation events are), so ranking
    # itself always reads backend directly; only freshness disclosure is
    # flag-gated, matching next_work_cmd's rationale above.
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


@cli.group()
def session() -> None:
    """Session lifecycle helpers."""


@session.command("resume")
@click.option("--sprint-id", type=int, default=None, help="Sprint ID (defaults to active)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def session_resume_cmd(obj, sprint_id, as_json) -> None:
    """Show a combined resume surface (context, next-work explain, and git context)."""
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


@cli.command("usage")
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
                "active_claims",
                "active_unclaimed",
            )
            union_payload = {
                "contract_version": "project-1",
                "project": project.summary(),
                "summary": {
                    key: sum(snapshot["summary"][key] for snapshot in snapshots)
                    for key in summary_keys
                },
                "sprints": [snapshot["sprint"] for snapshot in snapshots],
                "active_claims": [
                    value for snapshot in snapshots for value in snapshot["active_claims"]
                ],
                "active_unclaimed_items": [
                    value
                    for snapshot in snapshots
                    for value in snapshot["active_unclaimed_items"]
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
        "                 [--claim-id N --claim-token TOKEN]",
        "  item done-from-claim [--id ID] --claim-id N --claim-token TOKEN [--actor NAME]",
        "                 [--keep-claim] [--json]",
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
        "CLAIM",
        "  claim start    --item-id ID --actor NAME [--ttl N] [--branch B] [--worktree PATH]",
        "                 [--commit-sha SHA] [--pr-ref REF] [--runtime-session-id ID]",
        "                 [--instance-id ID] [--json]",
        "  claim create   --item-id ID --actor NAME [--type execute|inspect|review|coordinate]",
        "                 [--ttl N] [--non-exclusive] [--branch B] [--worktree PATH]",
        "                 [--commit-sha SHA] [--pr-ref REF] [--runtime-session-id ID]",
        "                 [--instance-id ID] [--coordinate-claim-id N --coordinate-claim-token T]",
        "                 [--json]",
        "  claim heartbeat --id N --claim-token TOKEN [--ttl N] [--actor NAME] [--json]",
        "  claim release  --id N --claim-token TOKEN [--actor NAME]",
        "  claim handoff  --id N --claim-token TOKEN --actor NAME [--mode transfer|rotate]",
        "                 [--ttl N] [--note TEXT] [--allow-legacy-adopt] [--output PATH] [--json]",
        "  claim list     --item-id ID [--all] [--json]",
        "  claim list-sprint [--sprint-id ID] [--all] [--expiring-within N] [--json]",
        "  claim show     --id N --claim-token TOKEN [--json]",
        "  claim resume   [--item-id ID] [--instance-id ID] [--runtime-session-id ID]",
        "                 [--hostname H --pid N] [--json]",
        "  claim recover  (--id N | --item-id ID) [--json]",
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


@cli.command("git-context")
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

@cli.command("render")
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
# remote-schema — deployment migration and service compatibility
# ---------------------------------------------------------------------------

@cli.group("remote-schema")
def remote_schema() -> None:
    """Check or migrate the shared PostgreSQL work schema."""


def _remote_schema_store(pg_url: str | None):
    from . import pg as _pg  # noqa: PLC0415

    url = pg_url or os.environ.get("SPRINTCTL_URL")
    if not url:
        raise click.ClickException(
            "Postgres URL required. Pass --url or set SPRINTCTL_URL."
        )
    try:
        return _pg.get_connection(url)
    except Exception as exc:
        detail = _redacted_postgres_error(exc, url)
        raise click.ClickException(
            f"could not connect to PostgreSQL for remote schema operation: {detail}"
        ) from exc


@remote_schema.command("check")
@click.option("--url", "pg_url", default=None, help="Postgres URL (default: $SPRINTCTL_URL)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the compatibility handshake as JSON")
def remote_schema_check_cmd(pg_url: str | None, as_json: bool) -> None:
    """Read the work API/schema handshake without attempting DDL."""
    from . import pg_migrations as _pg_migrations  # noqa: PLC0415

    store = _remote_schema_store(pg_url)
    try:
        handshake = _pg_migrations.compatibility_handshake(store)
        store.conn.rollback()
    finally:
        store.conn.close()
    if as_json:
        click.echo(json.dumps(handshake, indent=2, sort_keys=True))
    else:
        actual = handshake["remote_schema"]["actual"]
        click.echo(
            f"work_api={handshake['work_api_version']} "
            f"remote_schema={actual if actual is not None else 'missing'} "
            f"compatible={'yes' if handshake['compatible'] else 'no'}"
        )
    if not handshake["compatible"]:
        raise click.exceptions.Exit(1)


@remote_schema.command("migrate")
@click.option("--url", "pg_url", default=None, help="Migration-role Postgres URL (default: $SPRINTCTL_URL)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the migration result as JSON")
def remote_schema_migrate_cmd(pg_url: str | None, as_json: bool) -> None:
    """Apply serialized idempotent migrations with a migration-role credential."""
    from . import pg_migrations as _pg_migrations  # noqa: PLC0415

    store = _remote_schema_store(pg_url)
    try:
        result = _pg_migrations.migrate_schema(store)
    finally:
        store.conn.close()
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        versions = ",".join(str(value) for value in result["applied_versions"])
        click.echo(
            f"remote schema {result['from_version']} -> {result['to_version']}; "
            f"applied={versions or 'none'}"
        )


# ---------------------------------------------------------------------------
# migrate-to-remote — explicit SQLite-to-PostgreSQL state transfer
# ---------------------------------------------------------------------------

@cli.command("migrate-to-remote")
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
    from . import pg as _pg  # noqa: PLC0415

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
# repo — remote repo management
# ---------------------------------------------------------------------------

@cli.group()
def repo() -> None:
    """Manage repositories in remote postgres."""


@repo.command("list")
@click.pass_obj
def repo_list(obj) -> None:
    """List all repo_ids present in remote postgres."""
    store, _m = _get_store(obj)
    if not hasattr(store, "conn"):
        click.echo("Error: repo list requires remote backend (SPRINTCTL_BACKEND=remote)", err=True)
        sys.exit(1)
    from . import pg as _pg  # noqa: PLC0415
    repo_ids = _pg.list_repos(store.conn)
    for rid in repo_ids:
        click.echo(rid)


@repo.command("delete")
@click.argument("repo_id")
@click.option("--yes", "skip_confirm", is_flag=True, default=False, help="Skip confirmation prompt")
@click.pass_obj
def repo_delete(obj, repo_id, skip_confirm) -> None:
    """Delete all data for a repository from remote postgres."""
    store, _m = _get_store(obj)
    if not hasattr(store, "conn"):
        click.echo("Error: repo delete requires remote backend (SPRINTCTL_BACKEND=remote)", err=True)
        sys.exit(1)
    from . import pg as _pg  # noqa: PLC0415

    with store.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM sprint WHERE repo_id = %s", (repo_id,))
        row = cur.fetchone()
        sprint_count = row["cnt"] if row else 0

    if sprint_count == 0:
        click.echo(f"No data found for repo_id '{repo_id}'.", err=True)
        sys.exit(1)

    if not skip_confirm:
        click.confirm(
            f"Delete all data for '{repo_id}' ({sprint_count} sprint(s))? This cannot be undone.",
            abort=True,
        )

    counts = _pg.delete_repo(store.conn, repo_id)
    total = sum(counts.values())
    deleted = {t: n for t, n in counts.items() if n > 0}
    click.echo(f"Deleted {total} rows for '{repo_id}': {deleted}")
