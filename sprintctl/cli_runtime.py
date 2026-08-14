"""Shared runtime seams for extracted Sprintctl Click command modules.

This module owns backend selection, rendering helpers, and compatibility
functions. Command modules receive these names through the composition root.
"""

from __future__ import annotations

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
from . import db as _db
from . import maintain as _maintain
from . import observations as _observations
from . import outbox as _outbox
from . import pg as _pg
from . import project as _project
from . import projection as _projection
from . import projection_reads as _projection_reads
from . import served as _served
from . import served_routes as _served_routes
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


def _get_project_stores(
    obj: dict,
    project_value: str | Path,
    *,
    get_store=None,
):
    """Return a validated project plus one read-only store per backlog member."""
    try:
        project_path = _project.resolve_project_path(project_value)
        project = _project.load_project(project_path)
    except _project.ProjectConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    store, m = (get_store or _get_store)(obj)
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
_SERVED_ITEM_STATUS_ROUTE = _served_routes.routes_for("item.status")[0]
_SERVED_SPRINT_STATUS_ROUTE = _served_routes.routes_for("sprint.status")[0]
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
assert _SERVED_ITEM_STATUS_ROUTE.operation == "work.lifecycle.arbitrate"
assert _SERVED_SPRINT_STATUS_ROUTE.operation == "work.lifecycle.arbitrate"
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
    return _served_routes.disposition_for(command_path)


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
        "active_reservations",
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
        "active_reservations",
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
