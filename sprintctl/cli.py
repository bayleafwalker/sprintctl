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

from . import __version__
from . import application as _application
from . import backend as _backend
from . import authority as _authority
from . import authority_config as _authority_config
from . import commands as _commands
from . import context_candidates as _context_candidates
from . import context_contract as _context_contract
from . import contracts as _contracts
from . import cutover as _cutover
from . import db as _db
from . import doctor as _doctor
from . import dualwrite as _dualwrite
from . import maintain as _maintain
from . import observations as _observations
from . import outbox as _outbox
from . import pg as _pg
from . import pilot as _pilot
from . import project as _project
from . import projection as _projection
from . import projection_reads as _projection_reads
from . import served as _served
from . import served_routes as _served_routes
from . import shadow as _shadow
from . import sync as _sync
from .cli_support import _redacted_postgres_error
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


def _apply_scoped_id(obj: dict, value: str | int, *, field: str = "id") -> int:
    """Resolve a dual-form ``repo#id`` option into an ID and repo scope.

    A reference prefix is equivalent to the global ``--repo-id`` for this
    invocation.  Conflicting explicit scopes fail before any backend read.
    """
    try:
        reference_repo_id, identifier = _backend.parse_scoped_id(value, field=field)
    except _backend.ReferenceParseError as exc:
        raise click.ClickException(str(exc)) from exc
    if reference_repo_id is not None:
        explicit_repo_id = obj.get("explicit_repo_id")
        if explicit_repo_id is not None and explicit_repo_id != reference_repo_id:
            raise click.ClickException(
                f"Error: repo scope mismatch: --repo-id='{explicit_repo_id}' "
                f"but {field} reference selects '{reference_repo_id}'."
            )
        obj["explicit_repo_id"] = reference_repo_id
    return identifier


def _backend_target(config) -> str:
    if config.mode == "served":
        assert config.served_profile is not None
        return config.served_profile.endpoint
    if config.mode == "remote" and config.url:
        parsed = urlsplit(config.url)
        host = parsed.hostname or "<unresolved>"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{host}{port}{parsed.path or '/'}"
    return "local SQLite"


def _resolved_context(config) -> dict[str, str | None]:
    return {
        "repo_id": config.repo_id,
        "repo_source": config.repo_source,
        "backend": config.mode,
        "target": _backend_target(config),
    }


def _render_resolved_context(context: dict[str, str | None]) -> str:
    return (
        "Context: "
        f"repo={context['repo_id']} (source={context['repo_source']}) "
        f"backend={context['backend']} target={context['target']}"
    )


def _get_store(obj: dict):
    """Return a normal local store only; served calls dispatch before this.

    ``load_backend_config`` rejects legacy direct-remote configuration before
    this function can import the PostgreSQL module.  Keep the remote branch
    below solely as a defensive invariant for explicitly authorized internal
    callers that may inject a prevalidated config during recovery work.
    """
    try:
        config = _backend.load_backend_config(
            explicit_repo_id=obj.get("explicit_repo_id"),
            allow_markerless_nonlocal=obj.get("allow_markerless_nonlocal", False),
        )
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
            tombstone_message = _pg.superseded_marker_message(store)
            if tombstone_message is not None:
                raise RuntimeError(
                    "remote backend is superseded: " + tombstone_message
                )
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


# The exact served-mode allowlist entries #1195 wires up. Indexing them here
# (rather than hard-coding operation name strings at each call site) means a
# mismatch between this file and sprintctl/served_routes.py's table raises
# immediately at import time instead of silently drifting.
_SERVED_SPRINT_LIST_ROUTE = _served_routes.routes_for("sprint.list")[0]
_SERVED_SPRINT_CREATE_ROUTE = _served_routes.routes_for("sprint.create")[0]
_SERVED_ITEM_SHOW_ROUTE = _served_routes.routes_for("item.show")[0]
_SERVED_EVENT_LIST_ROUTE = _served_routes.routes_for("event.list")[0]
_SERVED_EVENT_ADD_ROUTE = _served_routes.routes_for("event.add")[0]
_SERVED_ITEM_ADD_ROUTE = _served_routes.routes_for("item.add")[0]
_SERVED_ITEM_EDIT_ROUTE = _served_routes.routes_for("item.edit")[0]
_SERVED_SPRINT_SHOW_ROUTE = _served_routes.routes_for("sprint.show")[0]
_SERVED_CLAIM_START_ROUTE = _served_routes.routes_for("claim.start")[0]
_SERVED_ITEM_STATUS_ROUTE = _served_routes.routes_for("item.status")[0]
_SERVED_SPRINT_STATUS_ROUTE = _served_routes.routes_for("sprint.status")[0]
_SERVED_CLAIM_HEARTBEAT_ROUTE = _served_routes.routes_for("claim.heartbeat")[0]
_SERVED_CLAIM_RELEASE_ROUTE = _served_routes.routes_for("claim.release")[0]
_SERVED_NEXT_WORK_ROUTES = {
    route.operation: route for route in _served_routes.routes_for("next-work")
}
assert _SERVED_SPRINT_LIST_ROUTE.operation == "work.read.sprints"
assert _SERVED_SPRINT_CREATE_ROUTE.operation == "work.sprint.create"
assert _SERVED_ITEM_SHOW_ROUTE.operation == "work.read.item"
assert _SERVED_EVENT_LIST_ROUTE.operation == "work.read.events"
assert _SERVED_EVENT_ADD_ROUTE.operation == "work.event.add"
assert _SERVED_ITEM_ADD_ROUTE.operation == "work.item.create"
assert _SERVED_ITEM_EDIT_ROUTE.operation == "work.item.edit"
assert _SERVED_SPRINT_SHOW_ROUTE.operation == "work.read.sprint"
assert _SERVED_CLAIM_START_ROUTE.operation == "work.claim.start"
assert _SERVED_ITEM_STATUS_ROUTE.operation == "work.lifecycle.arbitrate"
assert _SERVED_SPRINT_STATUS_ROUTE.operation == "work.lifecycle.arbitrate"
assert _SERVED_CLAIM_HEARTBEAT_ROUTE.operation == "work.claim.arbitrate"
assert _SERVED_CLAIM_RELEASE_ROUTE.operation == "work.claim.arbitrate"
assert set(_SERVED_NEXT_WORK_ROUTES) == {"work.read.next-work", "work.project.next-work"}


def _served_config_or_none(obj: dict):
    """Return the active backend.ServedProfile-carrying config when
    SPRINTCTL_BACKEND=served, else None. Populates obj["backend_config"] the
    same way _get_store does, so served and store-backed command paths share
    one source of truth for the resolved backend mode."""
    try:
        config = _backend.load_backend_config(
            explicit_repo_id=obj.get("explicit_repo_id"),
            allow_markerless_nonlocal=obj.get("allow_markerless_nonlocal", False),
        )
    except _backend.BackendConfigError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    obj["backend_config"] = config
    if config.mode != "served":
        return None
    return config


def _served_operation_unavailable(command: str, *, replacement: str | None = None) -> None:
    """Fail closed for a command the served catalog cannot yet perform.

    This guard must run before ``_get_store``.  In particular, a missing
    catalog route must never turn into an attempt to import the direct
    PostgreSQL backend (which used to produce a misleading install-psycopg
    suggestion for a perfectly valid served invocation).
    """
    message = (
        f"Error: served-operation-unavailable: '{command}' is not available "
        "through the Vuoro served catalog yet."
    )
    if replacement:
        message += f" {replacement}"
    else:
        message += " Use local SQLite or an explicitly authorized recovery command."
    click.echo(message, err=True)
    sys.exit(1)


def _served_disposition(command_path: str, params: dict[str, object]) -> _served_routes.ServedDisposition:
    """Return the explicit served-mode disposition for one Click leaf.

    ``usage`` has two intentionally different surfaces: static command help is
    local and backend-free, while ``usage --context`` is a catalog read.  All
    other option-sensitive served limitations remain in their catalog-backed
    callbacks, where they can give a precise option-level diagnostic.
    """
    if command_path == "usage" and params.get("as_context"):
        return "catalog"
    return _served_routes.SERVED_COMMAND_DISPOSITIONS[command_path]


def _guard_served_command(command_path: str, params: dict[str, object]) -> None:
    """Fail unavailable served commands before their callback can open a store."""
    # This guard is installed around every leaf, including the deliberately
    # explicit schema/migration/recovery administration commands.  They must
    # not resolve a retired normal-client configuration merely to determine a
    # served disposition.
    if os.environ.get("SPRINTCTL_BACKEND") != "served":
        return
    disposition = _served_disposition(command_path, params)
    if disposition == "local":
        return
    config = _served_config_or_none(click.get_current_context().find_root().obj)
    if config is None or disposition == "catalog":
        return
    replacements = {
        "claim create": (
            "Use served 'claim start' for a single execute claim; "
            "coordinator/subclaim creation is not yet catalogued."
        ),
        "session resume": "The combined session-resume contract is not yet served.",
    }
    _served_operation_unavailable(command_path, replacement=replacements.get(command_path))


def _run_served(operation_label: str, func, *args, resolved_context: dict[str, str | None] | None = None, **kwargs):
    """Invoke a sprintctl.served facade function, translating any failure
    (transport, catalog validation, or an operation rejection) into the same
    'Error: ...' + exit(1) convention the local/remote store paths use."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - surface any served-mode failure uniformly
        message = f"Error: served {operation_label} failed: {exc}"
        if resolved_context is not None:
            message = f"{message}\n{_render_resolved_context(resolved_context)}"
        click.echo(message, err=True)
        sys.exit(1)


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
        return config.mode in ("local", "served")
    except _backend.BackendConfigError:
        return False


def _claim_recovery_dir() -> Path:
    return _db.get_db_path().parent / "claim-recovery"


def _claim_recovery_path(claim_id: int) -> Path:
    return _claim_recovery_dir() / f"claim-{claim_id}.json"


def _secure_claim_recovery_dir(*, create: bool) -> Path:
    """Return the private recovery directory, refusing unsafe local paths."""
    directory = _claim_recovery_dir()
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (info.st_mode & 0o777) != 0o700:
        raise OSError("claim recovery directory is not a private owner-controlled directory")
    return directory


def _claim_recovery_file_is_safe(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and (info.st_mode & 0o777) == 0o600
    )


def _write_claim_recovery_record(claim: dict) -> Path | None:
    if not _local_recovery_available():
        return None
    claim_id = claim.get("claim_id")
    claim_token = claim.get("claim_token")
    if claim_id is None or not claim_token:
        return None
    path = _claim_recovery_path(int(claim_id))
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
    try:
        directory = _secure_claim_recovery_dir(create=True)
        temporary = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError:
        return None
    return path


def _served_claim_recovery_projection(
    effect: Mapping[str, Any],
    *,
    item_id: int,
    actor: str,
    claim_type: str,
    claim_token: str,
) -> dict[str, Any] | None:
    """Normalize an accepted claim effect for the private recovery writer.

    Authority releases originally returned the canonical ``claim_id`` / ``actor``
    effect.  Deployed adapters can return the public claim-row representation
    (``id`` / ``agent``), either directly or below ``claim``.  Accept those
    equivalent representations, but never guess across disagreeing shapes: a
    malformed or mismatched accepted effect must retain its pending command and
    credential for an exact replay instead of writing proof for the wrong claim.
    """

    candidates: list[Mapping[str, Any]] = [effect]
    nested = effect.get("claim")
    if nested is not None:
        if not isinstance(nested, Mapping):
            return None
        candidates.append(nested)

    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        identity_keys = {
            "claim_id", "id", "work_item_id", "actor", "agent", "claim_type",
        }
        if not identity_keys.intersection(candidate):
            continue
        claim_ids = [candidate[key] for key in ("claim_id", "id") if key in candidate]
        actors = [candidate[key] for key in ("actor", "agent") if key in candidate]
        if (
            not claim_ids
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in claim_ids
            )
            or len(set(claim_ids)) != 1
            or not actors
            or any(not isinstance(value, str) or not value for value in actors)
            or len(set(actors)) != 1
        ):
            return None
        claim_id = claim_ids[0]
        work_item_id = candidate.get("work_item_id")
        candidate_actor = actors[0]
        candidate_type = candidate.get("claim_type")
        if (
            not isinstance(work_item_id, int)
            or isinstance(work_item_id, bool)
            or work_item_id <= 0
            or not isinstance(candidate_type, str)
            or not candidate_type
        ):
            return None
        normalized.append({
            **dict(candidate),
            "claim_id": claim_id,
            "work_item_id": work_item_id,
            "actor": candidate_actor,
            "claim_type": candidate_type,
        })

    if not normalized:
        return None
    identity = {
        (
            candidate["claim_id"], candidate["work_item_id"],
            candidate["actor"], candidate["claim_type"],
        )
        for candidate in normalized
    }
    if len(identity) != 1:
        return None
    claim = normalized[-1]
    if (
        claim["work_item_id"] != item_id
        or claim["actor"] != actor
        or claim["claim_type"] != claim_type
    ):
        return None
    return {**claim, "claim_token": claim_token}


def _remove_claim_recovery_record(claim_id: int) -> None:
    if not _local_recovery_available():
        return
    path = _claim_recovery_path(claim_id)
    try:
        directory = _secure_claim_recovery_dir(create=False)
        if not _claim_recovery_file_is_safe(path):
            return
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.unlink(path.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        return


def _load_claim_recovery_record(claim_id: int) -> dict | None:
    path = _claim_recovery_path(claim_id)
    try:
        _secure_claim_recovery_dir(create=False)
        if not _claim_recovery_file_is_safe(path):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (info.st_mode & 0o777) != 0o600:
                return None
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                return json.load(handle)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
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
    if s.get("aggregate_uuid"):
        out["status_revision"] = m.sprint_status_revision(s)
    if not detail:
        return out
    from . import sprint_detail
    return sprint_detail.build_sprint_show_detail(conn, s, backend=m)


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


def _recommended_commands_for_next_action(
    *, sprint_id: int, next_action: dict, repo_id: str | None = None
) -> list[str]:
    kind = next_action.get("kind")
    item_id = next_action.get("item_id")
    claim_id = next_action.get("claim_id")
    blocker_id = next_action.get("blocker_item_id")
    sprint_ref = _render_repo_reference(repo_id, sprint_id)
    item_ref = lambda identifier: _render_repo_reference(repo_id, identifier)

    if kind == "resolve-claim-identity":
        commands = [
            "sprintctl claim resume --json",
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
            commands.append(f"sprintctl item show --id {item_ref(blocker_id)}")
        if item_id is not None:
            commands.append(f"sprintctl item show --id {item_ref(item_id)}")
        commands.append(f"sprintctl next-work --sprint-id {sprint_ref} --json --explain")
        return commands

    if kind == "inspect-active-claim":
        commands = []
        if item_id is not None:
            commands.append(f"sprintctl item show --id {item_ref(item_id)}")
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
                    f"sprintctl claim start --item-id {item_ref(item_id)} --actor <name> --ttl 600 --json",
                    f"sprintctl item show --id {item_ref(item_id)}",
                ]
            )
        return commands

    if kind == "start-ready-item":
        commands = []
        if item_id is not None:
            commands.extend(
                [
                    f"sprintctl claim start --item-id {item_ref(item_id)} --actor <name> --ttl 600 --json",
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
    from . import handoff
    return handoff.build_handoff_bundle(conn, sprint, events_limit, backend=m or _db, version=__version__, git_context=_detect_git_context())


def _record_handoff_generated(conn, sprint_id: int, bundle: dict, *, m=None) -> None:
    from . import handoff
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


# Database maintenance historically registered here, between ``maintain`` and
# the export/import commands. Keep that insertion point stable while the
# command implementation lives in ``commands.db``.
_commands.register_db_commands(cli, get_store=lambda obj: _get_store(obj))
db_group = _commands.db_group
db_vacuum = _commands.db_vacuum
db_integrity = _commands.db_integrity
db_recover_from_remote = _commands.db_recover_from_remote


# ---------------------------------------------------------------------------
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

@cli.group()
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


@cli.command("context-candidates")
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


@cli.group()
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

@cli.command("remote-backfill")
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
    from . import pg as _pg  # noqa: PLC0415

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
