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
