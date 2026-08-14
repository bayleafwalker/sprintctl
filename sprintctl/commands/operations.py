"""Event, authority, pilot, and projection-read command groups.

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


def _append_sync_observation(event: dict, *, repo_id: str) -> dict:
    """Durably append a post-commit observation to normal sync state.

    A mirror failure never rolls back or hides the already committed authority
    event.  The structured outcome is instead returned to the operator so a
    pilot defect is observable and retryable without changing normal writes.
    """
    try:
        paths = _sync.repository_sync_paths(cwd=Path.cwd())
    except ValueError as exc:
        return {"status": "unavailable", "detail": str(exc)}
    _sync.migrate_legacy_sync_state(paths)
    envelope = _shadow_observation_envelope(event, repo_id)
    if envelope is None:
        return {"status": "unsupported", "event_type": event["event_type"]}
    producer = _outbox.open_outbox(paths.outbox_path)
    try:
        record = _outbox.append_observation(
            producer,
            event_type=envelope.record_type,
            actor=envelope.actor,
            payload=envelope.to_dict(),
            runtime_session_id=None,
            basis_revision=envelope.basis_revision,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.causation_id,
            occurred_at=envelope.authored_at,
            event_id=envelope.event_id,
        )
    except Exception as exc:  # Authority write already committed; surface, do not undo it.
        return {"status": "error", "detail": str(exc)}
    finally:
        producer.close()
    return {
        "status": "mirrored",
        "event_id": record.event_id,
        "event_type": record.event_type,
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

@click.group()
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


def _item_evidence_sync_paths() -> _sync.RepositorySyncPaths:
    """Return the normal durable observation/projection locations.

    Evidence is part of normal work memory, not an opt-in migration lane.
    Resolving the fixed repository paths here also makes offline appends
    possible before a remote backend is available.
    """
    try:
        paths = _sync.repository_sync_paths(cwd=Path.cwd())
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _sync.migrate_legacy_sync_state(paths)
    return paths


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
    paths = _item_evidence_sync_paths()
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
    producer = _outbox.open_outbox(paths.outbox_path)
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
        "outbox_path": str(paths.outbox_path),
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
    paths = _item_evidence_sync_paths()
    records_by_id: dict[str, dict] = {}

    if paths.outbox_path.exists():
        producer = _outbox.open_outbox(paths.outbox_path)
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
    if paths.projection_path.exists():
        cache = _projection.open_cached_projection(paths.projection_path)
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
    sprint_id: str,
    event_type: str,
    actor: str,
    work_item_id: str | None,
    source_type: str,
    payload: str | None,
    as_json: bool,
) -> None:
    sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    if work_item_id is not None:
        work_item_id = _apply_scoped_id(obj, work_item_id, field="item")
    payload_dict: dict | None = None
    if payload:
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError as e:
            click.echo(f"Invalid JSON payload: {e}", err=True)
            sys.exit(1)
    config = _served_config_or_none(obj)
    if config is not None:
        context = _resolved_context(config)
        result = _run_served(
            "event add", _served.event_add, config.served_profile,
            repo_id=config.repo_id, sprint_id=sprint_id, event_type=event_type,
            work_item_id=work_item_id, source_type=source_type, payload=payload_dict,
            resolved_context=context,
        )
        if as_json:
            click.echo(json.dumps({"operation": "event_add", **result}, indent=2))
            return
        click.echo(f"Recorded event #{result['event_id']}: {result['type']}  (actor: {result['actor']})")
        click.echo(_render_resolved_context(context))
        return
    if not actor:
        click.echo("Error: --actor is required for local event writes.", err=True)
        sys.exit(1)
    store, m = _get_store(obj)
    if m.get_sprint(store, sprint_id) is None:
        click.echo(f"Sprint #{sprint_id} not found.", err=True)
        sys.exit(1)
    if work_item_id is not None and m.get_work_item(store, work_item_id) is None:
        click.echo(f"Work item #{work_item_id} not found.", err=True)
        sys.exit(1)
    try:
        backend_config = obj.get("backend_config")
        expected_project = (
            backend_config.repo_id
            if backend_config is not None
            and (backend_config.mode != "local" or backend_config.marker is not None)
            else None
        )
        eid = m.create_event(
            store, sprint_id, actor, event_type,
            source_type=source_type, work_item_id=work_item_id, payload=payload_dict,
            expected_project=expected_project,
        )
    except (TypeError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    backend_config = obj.get("backend_config")
    repo_id = backend_config.repo_id if backend_config is not None else Path.cwd().name
    persisted = next((event for event in m.list_events(store, sprint_id) if event["id"] == eid), None)
    sync_result = (
        _append_sync_observation(persisted, repo_id=repo_id)
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
            "synchronization": sync_result,
        }, indent=2))
        return
    click.echo(f"Recorded event #{eid}: {event_type}  (actor: {actor})")
    if sync_result["status"] not in {"unsupported"}:
        click.echo(f"Synchronization: {sync_result['status']}")


@event.command("add")
@click.option("--sprint-id", type=str, required=True, help="Sprint ID or repo#id")
@click.option("--type", "--event-type", "event_type", required=True, help="Event type")
@click.option(
    "--actor",
    default=None,
    help="Actor name for local/remote writes; served mode uses the authenticated server actor",
)
@click.option("--item-id", "work_item_id", type=str, default=None, help="Work item ID or repo#id")
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
def event_add(obj, sprint_id: str, event_type, actor, work_item_id: str | None, source_type, payload, as_json) -> None:
    """Record an event."""
    _event_add_impl(obj, sprint_id, event_type, actor, work_item_id, source_type, payload, as_json)


@event.command("log")
@click.option("--sprint-id", type=str, required=True, help="Sprint ID or repo#id")
@click.option("--type", "--event-type", "event_type", required=True, help="Event type")
@click.option(
    "--actor",
    default=None,
    help="Actor name for local/remote writes; served mode uses the authenticated server actor",
)
@click.option("--item-id", "work_item_id", type=str, default=None, help="Work item ID or repo#id")
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
def event_log(obj, sprint_id: str, event_type, actor, work_item_id: str | None, source_type, payload, as_json) -> None:
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
    manifests = sorted(repo_root.glob("*.dispatch.json"))
    try:
        if len(manifests) != 1:
            raise ValueError("expected exactly one root dispatch manifest")
        raw = json.loads(manifests[0].read_text(encoding="utf-8"))
        repository_identity = raw.get("authority_repo_uuid", raw["repo_id"])
        return str(uuid.UUID(str(repository_identity)))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _authority_config.AuthorityCommandConfigError(
            "authority commands require exactly one root *.dispatch.json with a "
            "committed UUID authority_repo_uuid (legacy UUID repo_id is also accepted)"
        ) from exc


def _served_claim_authority_repo_uuid(context: dict[str, object], repo_root: Path) -> object:
    """Use the server's authority UUID when supplied, otherwise the local manifest.

    Served composition intentionally has no authority-repo UUID registry, so
    ``work.claim.context`` can return ``null`` for this compatibility field.
    The local dispatch manifest is the canonical source already used by other
    served authority-command callers.
    """

    authority_repo_uuid = context.get("authority_repo_uuid")
    if authority_repo_uuid is not None:
        return authority_repo_uuid
    return _authority_repo_uuid(repo_root)


def _find_pending_served_item_status_record(
    outbox_path: Path,
    *,
    record_type: str,
    item_id: int,
    to_status: str,
    aggregate_uuid: str | None = None,
    basis_revision: str | None = None,
) -> _outbox.OutboxRecord | None:
    """Find an earlier durable request for the exact unchanged transition.

    A direct served invocation can fail after append but before the authority
    admits the record.  Re-minting creates a later origin sequence and can
    only deepen that gap.  Keep the check deliberately conservative: callers
    must use the ordered outbox replay path to resolve the prior request.
    """

    producer = _outbox.open_outbox(outbox_path)
    try:
        for record in _outbox.list_records(producer):
            if (
                record.record_class != _outbox.AUTHORITY_COMMAND
                or record.event_type != record_type
            ):
                continue
            try:
                command = _contracts.record_from_dict(record.payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(command, _contracts.AuthorityCommand):
                continue
            paths = _authority_config.authority_command_paths(cwd=Path.cwd())
            if _authority_config.is_terminal_authority_decision(paths, event_id=record.event_id):
                continue
            if (
                command.refs.get("aggregate_id") == item_id
                and (aggregate_uuid is None or command.refs.get("aggregate_uuid") == aggregate_uuid)
                and command.payload.get("to_status") == to_status
                and (basis_revision is None or command.basis_revision == basis_revision)
            ):
                return record
    finally:
        producer.close()
    return None


def _find_pending_served_claim_acquire_record(
    outbox_path: Path, *, item_id: int, aggregate_uuid: str
) -> _outbox.OutboxRecord | None:
    """Return the one unresolved immutable served claim-acquire request.

    Claim creation is not safe to re-mint after an unknown outcome.  The
    durable request plus its private credential sidecar is the retry identity.
    Refuse ambiguity rather than selecting among multiple pending requests.
    """
    producer = _outbox.open_outbox(outbox_path)
    try:
        matches: list[_outbox.OutboxRecord] = []
        for record in _outbox.list_records(producer):
            if record.record_class != _outbox.AUTHORITY_COMMAND or record.event_type != "claim.acquire":
                continue
            try:
                command = _contracts.record_from_dict(record.payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(command, _contracts.AuthorityCommand):
                continue
            paths = _authority_config.authority_command_paths(cwd=Path.cwd())
            if _authority_config.is_terminal_authority_decision(paths, event_id=record.event_id):
                continue
            if (
                command.refs.get("aggregate_id") == item_id
                and command.refs.get("aggregate_uuid") == aggregate_uuid
            ):
                matches.append(record)
        if len(matches) > 1:
            raise click.ClickException(
                f"multiple pending claim.acquire requests exist for item #{item_id}; "
                "reconcile them before retrying claim create"
            )
        return matches[0] if matches else None
    finally:
        producer.close()


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


def _mint_authority_command_record(
    *,
    record_type: str,
    actor: str,
    refs: dict[str, object],
    payload: dict,
    basis_revision: str | None,
    outbox_path: Path,
    event_id: str | None = None,
    correlation_id: str | None = None,
    runtime_session_id: str | None = None,
) -> _outbox.OutboxRecord:
    """Build one immutable ``_contracts.AuthorityCommand`` envelope and
    durably append it to the local producer outbox (``.sprintctl/authority-
    command-outbox.db``), returning the appended durable ``OutboxRecord``.

    That record's origin_stream_id/origin_seq/schema_version/payload_sha256/
    created_at (assigned by the outbox append itself, not fabricated here) are
    exactly the shape a served operation's ``record`` argument requires --
    see ``_RECORD_DEFINITION`` in :mod:`sprintctl.vuoro_adapter`.

    Pure extraction of the record-construction step ``authority submit`` has
    always performed when minting a brand-new command (not its idempotent-
    retry path, which looks up a pre-existing durable record by event_id
    instead of minting one). Shared by ``authority submit`` and any other
    command path that needs to mint one durable authority-command envelope,
    e.g. served-mode item/sprint status transitions routed through
    ``work.lifecycle.arbitrate``.
    """

    request = _contracts.AuthorityCommand(
        event_id=event_id or str(uuid.uuid4()),
        record_type=record_type,
        schema_version="1",
        actor=actor,
        authored_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        refs=refs,
        payload=payload,
        basis_revision=basis_revision,
        correlation_id=(
            correlation_id if correlation_id is not None else (event_id or str(uuid.uuid4()))
        ),
    )
    producer = _outbox.open_outbox(outbox_path)
    try:
        runtime_detect = (
            __runtime_source.get("_detect_runtime_session_id", _detect_runtime_session_id)
            if __runtime_source is not None
            else _detect_runtime_session_id
        )
        return _outbox.append_authority_command(
            producer,
            request,
            runtime_session_id=(
                runtime_session_id
                if runtime_session_id is not None
                else runtime_detect(None)
            ),
        )
    finally:
        producer.close()


def _served_record_argument(durable: _outbox.OutboxRecord) -> dict[str, object]:
    """Shape a durable ``OutboxRecord`` into the plain JSON dict a served
    operation's ``record`` input expects (``_RECORD_DEFINITION`` in
    :mod:`sprintctl.vuoro_adapter`).

    Deliberately a small local duplicate of
    ``sprintctl.application.record_to_dict`` rather than a reuse of it:
    ``sprintctl.application`` imports ``sprintctl.cutover``, which chains
    through ``sprintctl.doctor`` to ``sprintctl.pg_migrations``, and served-
    mode call sites must stay free of that import (see
    ``tests/test_served.py::test_served_and_its_optional_dependencies_never_import_postgres_modules``).
    cli.py itself already imports pg-touching modules for local/remote-mode
    commands, so this constraint is about served.py's own dependency surface,
    not about cli.py -- but this shaping is kept next to the served-mode call
    sites that need it rather than reaching into ``application`` out of
    habit.
    """

    return {
        "origin_stream_id": durable.origin_stream_id,
        "origin_seq": durable.origin_seq,
        "event_id": durable.event_id,
        "schema_version": durable.schema_version,
        "record_class": durable.record_class,
        "event_type": durable.event_type,
        "actor": durable.actor,
        "runtime_session_id": durable.runtime_session_id,
        "occurred_at": durable.occurred_at,
        "basis_revision": durable.basis_revision,
        "correlation_id": durable.correlation_id,
        "causation_id": durable.causation_id,
        "payload": json.loads(json.dumps(durable.payload)),
        "payload_sha256": durable.payload_sha256,
        "created_at": durable.created_at,
    }


@click.group("authority")
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
    payload["pending_records"] = []
    if status.paths.outbox_path.exists():
        producer = _outbox.open_outbox(status.paths.outbox_path)
        try:
            records = _outbox.list_records(producer)
            payload["outbox_records"] = len(records)
            payload["pending_records"] = [
                {
                    "event_id": record.event_id,
                    "origin_stream_id": record.origin_stream_id,
                    "origin_seq": record.origin_seq,
                    "record_class": record.record_class,
                    "event_type": record.event_type,
                }
                for record in records
                if not (
                    record.record_class == _outbox.AUTHORITY_COMMAND
                    and _authority_config.is_terminal_authority_decision(
                        status.paths, event_id=record.event_id
                    )
                )
            ]
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
        click.echo(f"Pending producer records: {len(payload['pending_records'])}")
        for record in payload["pending_records"]:
            click.echo(
                "  "
                f"{record['origin_stream_id']}#{record['origin_seq']} "
                f"{record['event_type']} ({record['event_id']})"
            )
        click.echo(f"Pending proof sidecars: {payload['pending_credentials']}")


def _served_authority_pages(
    read_page, *, page_size: int = 250, offset_key: str = "ingest_offset",
) -> list[dict[str, object]]:
    """Read an offset-paginated served authority stream to completion."""
    after = 0
    values: list[dict[str, object]] = []
    while True:
        page = read_page(after, page_size)
        if not isinstance(page, list):
            raise click.ClickException("served authority audit returned an invalid page")
        values.extend(page)
        if len(page) < page_size:
            return values
        last = page[-1]
        try:
            after = int(last[offset_key])
        except (KeyError, TypeError, ValueError) as exc:
            raise click.ClickException("served authority audit returned an invalid offset") from exc


@authority_commands.command("reconcile")
@click.option("--apply", "apply_changes", is_flag=True, default=False,
              help="Write local receipts from the served ledger after a clean audit.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def authority_reconcile(obj, apply_changes: bool, as_json: bool) -> None:
    """Audit the local outbox against the authoritative served ledger.

    This is deliberately served-led.  It never replays a record, changes a
    served cursor, or reconstructs a decision.  ``--apply`` writes only local
    receipts: served decisions settle matching commands; an old local sequence
    below the served stream high-water but absent from that ledger is marked as
    absent, so it cannot block newer served work forever.
    """
    config = _served_config_or_none(obj)
    if config is None:
        raise click.ClickException("authority reconcile requires a served backend")
    paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    producer = _outbox.open_outbox(paths.outbox_path)
    try:
        # Terminal receipts are local dispositions, not retry candidates.  Keep
        # immutable command rows for audit, but never report a quarantined or
        # served-confirmed row as pending on a later reconciliation.
        local = [
            r for r in _outbox.list_records(producer)
            if r.record_class == _outbox.AUTHORITY_COMMAND
            and not _authority_config.is_terminal_authority_decision(
                paths, event_id=r.event_id
            )
        ]
    finally:
        producer.close()

    remote_stream_high_water: dict[str, int] = {}

    def read_record_page(after: int, limit: int) -> object:
        response = _served.read_records(
            config.served_profile, repo_id=config.repo_id,
            after_offset=after, limit=limit,
        )
        cursors = response.get("stream_high_water", {})
        if not isinstance(cursors, dict):
            raise click.ClickException("served authority audit returned invalid stream cursors")
        for stream_id, high_water in cursors.items():
            if not isinstance(stream_id, str) or isinstance(high_water, bool):
                raise click.ClickException("served authority audit returned invalid stream cursors")
            try:
                parsed_high_water = int(high_water)
            except (TypeError, ValueError) as exc:
                raise click.ClickException("served authority audit returned invalid stream cursors") from exc
            if parsed_high_water < 0:
                raise click.ClickException("served authority audit returned invalid stream cursors")
            remote_stream_high_water[stream_id] = max(
                remote_stream_high_water.get(stream_id, 0), parsed_high_water
            )
        return response.get("records")

    remote_entries = _served_authority_pages(read_record_page)
    decisions = _served_authority_pages(
        lambda after, limit: _served.read_decisions(
            config.served_profile, repo_id=config.repo_id,
            after_offset=after, limit=limit,
        ).get("decisions"), offset_key="decision_ingest_offset",
    )
    remote_by_event: dict[str, tuple[_outbox.OutboxRecord, int]] = {}
    remote_high_water: dict[str, int] = {}
    for entry in remote_entries:
        try:
            record_data = entry["record"]
            if not isinstance(record_data, dict):
                raise TypeError("record is not an object")
            record = _outbox.OutboxRecord(**record_data)
            offset = int(entry["ingest_offset"])
        except (KeyError, TypeError, ValueError) as exc:
            raise click.ClickException("served authority audit returned an invalid record") from exc
        remote_by_event[record.event_id] = (record, offset)
        remote_high_water[record.origin_stream_id] = max(
            remote_high_water.get(record.origin_stream_id, 0), record.origin_seq
        )
    for stream_id, high_water in remote_stream_high_water.items():
        remote_high_water[stream_id] = max(remote_high_water.get(stream_id, 0), high_water)
    decisions_by_request = {
        str(value["request_event_id"]): value for value in decisions
        if isinstance(value.get("request_event_id"), str)
    }

    allowed = frozenset({_outbox.OBSERVATION, _outbox.AUTHORITY_COMMAND})
    confirmed: list[tuple[_outbox.OutboxRecord, dict[str, object]]] = []
    absent: list[_outbox.OutboxRecord] = []
    conflicts: list[dict[str, object]] = []
    pending: list[_outbox.OutboxRecord] = []
    for record in local:
        remote = remote_by_event.get(record.event_id)
        if remote is None:
            if record.origin_seq <= remote_high_water.get(record.origin_stream_id, 0):
                absent.append(record)
            else:
                pending.append(record)
            continue
        remote_record, offset = remote
        local_hash = _pg._prepare_ingest_record(record, allowed_classes=allowed).record_sha256
        remote_hash = _pg._prepare_ingest_record(remote_record, allowed_classes=allowed).record_sha256
        if local_hash != remote_hash:
            conflicts.append({"event_id": record.event_id, "origin_seq": record.origin_seq,
                              "served_ingest_offset": offset, "reason": "semantic-record-mismatch"})
            continue
        decision = decisions_by_request.get(record.event_id)
        if decision is None:
            conflicts.append({"event_id": record.event_id, "origin_seq": record.origin_seq,
                              "served_ingest_offset": offset, "reason": "served-decision-missing"})
            continue
        outcome = decision.get("outcome")
        if outcome not in {"accepted", "rejected"}:
            conflicts.append({"event_id": record.event_id, "origin_seq": record.origin_seq,
                              "served_ingest_offset": offset, "reason": "served-decision-invalid"})
            continue
        confirmed.append((record, decision))

    if apply_changes and conflicts:
        raise click.ClickException("served authority reconciliation has conflicts; no local receipts were written")
    applied_confirmed = 0
    applied_absent = 0
    if apply_changes:
        for record, decision in confirmed:
            _authority_config.mark_terminal_authority_decision(
                paths, event_id=record.event_id, outcome=str(decision["outcome"]),
                served_decision=decision,
            )
            _authority_config.remove_pending_authority_credential(paths, event_id=record.event_id)
            applied_confirmed += 1
        for record in absent:
            _authority_config.mark_terminal_authority_decision(
                paths, event_id=record.event_id, outcome="absent-from-served-ledger",
            )
            _authority_config.remove_pending_authority_credential(paths, event_id=record.event_id)
            applied_absent += 1
    payload = {
        "served_authoritative": True,
        "remote_record_count": len(remote_entries),
        "remote_stream_high_water": remote_high_water,
        "confirmed": [{"event_id": r.event_id, "origin_seq": r.origin_seq,
                       "outcome": d["outcome"]} for r, d in confirmed],
        "absent_from_served_ledger": [{"event_id": r.event_id, "origin_seq": r.origin_seq}
                                       for r in absent],
        "pending_after_served_high_water": [{"event_id": r.event_id, "origin_seq": r.origin_seq}
                                              for r in pending],
        "conflicts": conflicts,
        "applied_confirmed": applied_confirmed,
        "applied_absent": applied_absent,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo("Served-authoritative reconciliation: "
                   f"{len(confirmed)} confirmed, {len(absent)} absent, "
                   f"{len(pending)} pending, {len(conflicts)} conflicts.")


@authority_commands.command("quarantine")
@click.option("--stream-id", required=True, help="Origin stream UUID to close locally.")
@click.option("--reason", required=True, help="Auditable reason the stream cannot be reconciled.")
@click.option("--apply", "apply_changes", is_flag=True, default=False,
              help="Write local quarantine receipts; without it, only audit the target.")
@click.option("--json", "as_json", is_flag=True, default=False)
def authority_quarantine(stream_id: str, reason: str, apply_changes: bool, as_json: bool) -> None:
    """Quarantine one irreconcilable local authority stream without replaying it.

    This is intentionally local-only.  It neither reads nor writes served
    state, leaves immutable outbox rows untouched, and requires an explicit
    rationale recorded beside every terminal receipt.  Use only after a
    served-led reconciliation audit cannot establish a safe outcome.
    """
    try:
        canonical_stream_id = str(uuid.UUID(stream_id))
    except (TypeError, ValueError) as exc:
        raise click.ClickException("stream-id must be a UUID") from exc
    if canonical_stream_id != stream_id:
        raise click.ClickException("stream-id must be a canonical UUID")
    if not reason.strip():
        raise click.ClickException("reason must be non-empty")
    paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    producer = _outbox.open_outbox(paths.outbox_path)
    try:
        records = [
            record for record in _outbox.list_records(producer)
            if record.record_class == _outbox.AUTHORITY_COMMAND
            and record.origin_stream_id == canonical_stream_id
            and not _authority_config.is_terminal_authority_decision(paths, event_id=record.event_id)
        ]
    finally:
        producer.close()
    if not records:
        raise click.ClickException("no pending authority commands found for stream-id")
    if apply_changes:
        for record in records:
            _authority_config.mark_terminal_authority_decision(
                paths, event_id=record.event_id, outcome="quarantined-divergent-stream",
                quarantine_reason=reason,
            )
            _authority_config.remove_pending_authority_credential(paths, event_id=record.event_id)
    payload = {
        "local_only": True,
        "stream_id": canonical_stream_id,
        "reason": reason.strip(),
        "records": [
            {"event_id": record.event_id, "origin_seq": record.origin_seq,
             "event_type": record.event_type}
            for record in records
        ],
        "applied": apply_changes,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        action = "Quarantined" if apply_changes else "Would quarantine"
        click.echo(f"{action} {len(records)} local authority command(s) in stream {canonical_stream_id}.")


@authority_commands.command("rollover")
@click.option("--reason", required=True, help="Auditable reason the terminal stream is being retired.")
@click.option("--apply", "apply_changes", is_flag=True, default=False,
              help="Archive the terminal local outbox; without it, only audit rollover fitness.")
@click.option("--json", "as_json", is_flag=True, default=False)
def authority_rollover(reason: str, apply_changes: bool, as_json: bool) -> None:
    """Start a fresh producer stream after every command in the old one is terminal.

    The old SQLite outbox is retained byte-for-byte under ``.sprintctl``.  This
    is the only local recovery for a quarantined stream whose next sequence
    cannot be admitted by the served cursor; it never replays or mutates the
    old stream.
    """
    if not reason.strip():
        raise click.ClickException("reason must be non-empty")
    paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    producer = _outbox.open_outbox(paths.outbox_path)
    try:
        records = [record for record in _outbox.list_records(producer)
                   if record.record_class == _outbox.AUTHORITY_COMMAND]
        origin_stream_id = _outbox.get_origin_stream_id(producer)
    finally:
        producer.close()
    if origin_stream_id is None or not records:
        raise click.ClickException("authority outbox has no command stream to roll over")
    streams = {record.origin_stream_id for record in records}
    if streams != {origin_stream_id}:
        raise click.ClickException("authority outbox contains multiple command streams; manual recovery required")
    pending = [record for record in records if not _authority_config.is_terminal_authority_decision(
        paths, event_id=record.event_id
    )]
    if pending:
        raise click.ClickException("authority stream has pending commands; reconcile or quarantine them first")
    archive = paths.state_dir / f"authority-command-outbox.{origin_stream_id}.quarantined.db"
    if apply_changes:
        try:
            archive = _authority_config.archive_terminal_authority_outbox(
                paths, origin_stream_id=origin_stream_id, reason=reason,
            )
        except _authority_config.AuthorityCommandConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        fresh = _outbox.open_outbox(paths.outbox_path)
        fresh.close()
    payload = {
        "local_only": True,
        "origin_stream_id": origin_stream_id,
        "reason": reason.strip(),
        "terminal_command_count": len(records),
        "archive_path": str(archive),
        "fresh_outbox_path": str(paths.outbox_path),
        "applied": apply_changes,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        action = "Rolled over" if apply_changes else "Would roll over"
        click.echo(f"{action} terminal authority stream {origin_stream_id}.")


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
    """Append one local shadow authority command.

    The former ``enforce`` implementation arbitrated through Sprintctl's
    retired normal direct-PostgreSQL backend.  It is deliberately unavailable
    here: ordinary served lifecycle and claim commands mint and submit their
    own catalog-authorized records, while ``authority sync`` is the retry
    surface for records already retained locally.
    """
    rollout = _authority_rollout_status()
    if rollout.mode is _authority_config.AuthorityCommandMode.OFF:
        raise click.ClickException(
            "authority command mode is off; use 'sprintctl authority mode --set shadow|enforce'"
        )
    if rollout.mode is _authority_config.AuthorityCommandMode.ENFORCE:
        raise click.ClickException(
            "authority submit enforce is retired with the direct PostgreSQL client; "
            "use the corresponding served work command, then use 'authority sync' "
            "only to retry an already-recorded served request"
        )
    store, m = _get_store(obj)
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
            durable = _mint_authority_command_record(
                record_type=record_type,
                actor=actor,
                refs=refs,
                payload=command_payload,
                basis_revision=basis_revision,
                outbox_path=rollout.paths.outbox_path,
                event_id=event_id,
            )
        except (TypeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        request = _contracts.record_from_dict(durable.payload)

        if credentials:
            _authority_config.store_pending_authority_credentials(
                rollout.paths,
                event_id=request.event_id,
                credentials=credentials,
                recovery_credential_ref=generated_ref,
            )

    result: dict[str, object] = {
        "request_event_id": durable.event_id,
        "origin_stream_id": durable.origin_stream_id,
        "origin_seq": durable.origin_seq,
        "mode": rollout.mode.value,
        "status": "pending-shadow",
    }
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


def _served_authority_sync(config, batch_size: int, as_json: bool) -> None:
    """Served-mode ``authority sync``: flushes durable outbox records through
    one ``work.batch.apply`` call per ``--batch-size`` chunk.

    ``WorkApplication.apply_records`` (application.py:612-644) is the entire
    served sync mechanism: it already self-routes a mixed batch of
    OBSERVATION and AUTHORITY_COMMAND records by ``record_class`` -- runs of
    observations are ingested together and each authority command is
    arbitrated individually against one running ``transient_credentials``
    map -- so unlike the local/remote path (``_sync.synchronize_outbox``,
    which also rebuilds a local SQLite projection cache), there is nothing
    else to route here: served mode keeps no local projection at all, every
    served read already goes live to the server.

    Two things are deliberately excluded from every outgoing chunk, and
    reported rather than silently dropped:

    - A command whose payload references a ``...credential_ref`` with no
      matching pending proof sidecar blocks that record *and every record
      after it* for this pass -- this mirrors ``synchronize_outbox``'s own
      stop-at-first-gap semantics exactly (a later record may have been
      minted assuming an earlier one already landed, so nothing after a gap
      is speculatively sent ahead of it). Reported under
      ``pending_command_event_ids``.
    - A ``capability-receipt.accept`` record: the server's
      ``SUPPORTED_BATCH_TYPES`` (application.py:29-42) excludes it, so
      sending one would abort its *entire chunk* with a confusing
      ``record-type-not-allowed`` rejection rather than just that one
      record. It is skipped -- without stopping anything after it, since
      unlike a credential gap, no future retry ever makes it sendable over
      this operation -- and reported under
      ``unsupported_command_event_ids``.

    Note on actor identity: the server rejects any record -- observation or
    command -- whose ``actor`` does not match the authenticated served
    identity (``_validate_record`` in application.py). An observation
    durably recorded via ``event observation add --actor`` under a mismatched
    actor is therefore permanently unflushable through served sync: every
    retry hits the same ``actor-mismatch`` rejection forever. This is known,
    accepted behavior for #1195 Group C -- not something this sync path
    attempts to detect or repair.
    """
    resolved_context = _resolved_context(config)
    rollout_paths = _authority_config.authority_command_paths(cwd=Path.cwd())
    producer = _outbox.open_outbox(rollout_paths.outbox_path)
    try:
        records = _outbox.list_records(producer)
    finally:
        producer.close()

    included: list[_outbox.OutboxRecord] = []
    pending_event_ids: list[str] = []
    unsupported_event_ids: list[str] = []
    transient_credentials: dict[str, str] = {}

    for index, record in enumerate(records):
        if record.record_class == _outbox.OBSERVATION:
            included.append(record)
            continue
        if record.event_type == "capability-receipt.accept":
            unsupported_event_ids.append(record.event_id)
            continue
        if _authority_config.is_terminal_authority_decision(
            rollout_paths, event_id=record.event_id
        ):
            continue
        envelope = _contracts.record_from_dict(record.payload)
        required_refs = {
            value
            for key, value in envelope.payload.items()
            if key.endswith("credential_ref") and isinstance(value, str)
        }
        pending = _authority_config.load_pending_authority_credential(
            rollout_paths,
            event_id=record.event_id,
        )
        available = (not required_refs) if pending is None else (
            required_refs <= set(pending.credentials)
        )
        if not available:
            pending_event_ids.extend(
                blocked.event_id
                for blocked in records[index:]
                if blocked.record_class == _outbox.AUTHORITY_COMMAND
            )
            break
        if pending is not None:
            transient_credentials.update(pending.credentials)
        included.append(record)

    commands_by_event_id = {
        record.event_id: record
        for record in included
        if record.record_class == _outbox.AUTHORITY_COMMAND
    }

    uploaded_observation_count = 0
    decisions: list[dict[str, object]] = []
    for start in range(0, len(included), batch_size):
        chunk = included[start : start + batch_size]
        if not chunk:
            continue
        key = _application.batch_idempotency_key(chunk)
        result = _run_served(
            "authority sync",
            _served.batch_apply,
            config.served_profile,
            repo_id=config.repo_id,
            records=[_served_record_argument(r) for r in chunk],
            idempotency_key=key,
            transient_credentials=transient_credentials,
            resolved_context=resolved_context,
        )
        for item in result.get("results", []):
            if item.get("kind") == "decision":
                decisions.append(item)
            else:
                uploaded_observation_count += 1

    for decision in decisions:
        event_id = decision.get("event_id")
        record = commands_by_event_id.get(event_id)
        if record is None:
            raise click.ClickException(
                "served authority sync returned a decision for a record that was not sent"
            )
        _authority_config.mark_terminal_authority_decision(
            rollout_paths,
            event_id=record.event_id,
            outcome=decision.get("outcome"),
        )
        keep_for_recovery = (
            decision.get("outcome") == "accepted"
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
                rollout_paths, event_id=event_id
            )

    payload = {
        "uploaded_observation_count": uploaded_observation_count,
        "decisions": decisions,
        "pending_command_event_ids": pending_event_ids,
        "unsupported_command_event_ids": unsupported_event_ids,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(
            f"Authority sync: {uploaded_observation_count} observations uploaded, "
            f"{len(decisions)} decisions, {len(pending_event_ids)} pending, "
            f"{len(unsupported_event_ids)} unsupported."
        )
        if unsupported_event_ids:
            click.echo(
                "capability-receipt.accept is not supported over the served batch "
                f"operation; unsupported event ids: {', '.join(unsupported_event_ids)}",
                err=True,
            )
        click.echo(_render_resolved_context(resolved_context))


@authority_commands.command("sync")
@click.option("--batch-size", default=100, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def authority_sync(obj, batch_size: int, as_json: bool) -> None:
    """Retry durable commands whose local proof sidecar is available."""
    config = _served_config_or_none(obj)
    if config is not None:
        _served_authority_sync(config, batch_size, as_json)
        return
    rollout = _authority_rollout_status()
    if rollout.mode is not _authority_config.AuthorityCommandMode.ENFORCE:
        raise click.ClickException("authority sync requires enforce mode")
    raise click.ClickException(
        "local authority sync through the retired direct PostgreSQL client is unavailable; "
        "configure served mode and retry through the Vuoro authority"
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

@click.group()
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
    try:
        result = _sync.synchronize_repository(
            store,
            outbox_path=status.paths.outbox_path,
            projection_path=status.paths.projection_path,
            batch_size=batch_size,
        )
    except (TypeError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
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


@click.command("sync")
@click.option("--batch-size", default=100, type=int, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def sync_cmd(obj, batch_size: int, as_json: bool) -> None:
    """Synchronize durable local observations and pull projections."""
    store, _m = _get_store(obj)
    if obj["backend_config"].mode != "remote":
        raise click.ClickException("normal synchronization requires a remote sprintctl backend")
    try:
        paths = _sync.repository_sync_paths(cwd=Path.cwd())
        result = _sync.synchronize_repository(
            store,
            outbox_path=paths.outbox_path,
            projection_path=paths.projection_path,
            batch_size=batch_size,
        )
    except (TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "uploaded": len(result.uploaded),
        "duplicates": sum(outcome.duplicate for outcome in result.uploaded),
        "applied_count": result.applied_count,
        "watermark": result.watermark.ingest_offset,
        "decision_applied_count": result.decision_applied_count,
    }
    click.echo(json.dumps(payload, indent=2) if as_json else (
        f"Synchronized {payload['uploaded']} records; watermark {payload['watermark']}."
    ))


def _emit_cutover_evidence_text(payload: dict) -> None:
    """Shared text rendering for ``pilot cutover-evidence``'s local and served
    paths -- both call the exact same ``cutover.build_cutover_evidence``
    contract (locally or over ``work.pilot.cutover-evidence``), so both
    produce this same payload shape."""
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


def _served_cutover_evidence(
    config,
    sprint_id,
    skip_parity,
    max_watermark_age_seconds,
    skip_rollback_rehearsal,
    as_json,
) -> None:
    """Served-mode ``pilot cutover-evidence``: routes to
    ``work.pilot.cutover-evidence``, the same ``cutover.build_cutover_evidence``
    call the local path makes, just invoked over the served transport.

    Local mode computes ``parity`` itself by comparing the pilot's local
    shadow-observation outbox against this repo's *authoritative* event
    table, read directly off the local store via ``m.list_events(store,
    sprint_id)`` (see the local branch of ``pilot_cutover_evidence`` below).
    There is no served-catalog read operation that exposes that sprint-wide
    authoritative event log: ``work.read.item`` only returns one item's
    events (see ``WorkApplication._read_item``), and no
    sprint-scoped-events / ``work.read.events``-shaped operation is
    registered in ``served_routes.py`` or ``vuoro_adapter.py``. So unlike
    ``item status``/``sprint status`` (which have a served read this facade
    can reuse), there is no served-mode equivalent to source real parity
    from -- inventing a new server-side operation for it is out of scope
    here. This fails closed only in the one case that would actually need
    that missing data (the pilot enabled and a real parity computation
    requested); it otherwise matches local mode's own no-op exactly: when
    the pilot was never enabled, local mode leaves ``parity`` as ``None``
    without erroring, and this does too.
    """
    resolved_context = _resolved_context(config)
    parity_payload = None
    if not skip_parity:
        try:
            status = _pilot.shadow_pilot_status(cwd=Path.cwd())
        except _pilot.ShadowPilotConfigError as exc:
            click.echo(
                f"Error: {exc}\n{_render_resolved_context(resolved_context)}",
                err=True,
            )
            sys.exit(1)
        if status.enabled:
            click.echo(
                "Error: served pilot cutover-evidence cannot compute parity: no served "
                "read operation exposes a sprint's authoritative event history "
                "(work.read.item only returns one item's events, not the sprint-wide "
                "event log parity computation needs); pass --skip-parity, or use "
                "SPRINTCTL_BACKEND=local for a full parity computation.\n"
                f"{_render_resolved_context(resolved_context)}",
                err=True,
            )
            sys.exit(1)
        # Pilot disabled: parity stays None, matching local mode's own no-op
        # (build_cutover_evidence reports "parity-not-evaluated" either way).

    payload = _run_served(
        "pilot cutover-evidence",
        _served.cutover_evidence,
        config.served_profile,
        repo_id=config.repo_id,
        parity=parity_payload,
        max_watermark_age_seconds=max_watermark_age_seconds,
        rehearse=not skip_rollback_rehearsal,
        resolved_context=resolved_context,
    )

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    _emit_cutover_evidence_text(payload)
    click.echo(_render_resolved_context(resolved_context))


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
    default=300,
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
    config = _served_config_or_none(obj)
    if config is not None:
        _served_cutover_evidence(
            config, sprint_id, skip_parity, max_watermark_age_seconds, skip_rollback_rehearsal, as_json
        )
        return
    parity_payload = None
    if not skip_parity:
        try:
            paths = _sync.repository_sync_paths(cwd=Path.cwd())
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        _sync.migrate_legacy_sync_state(paths)
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
            producer = _outbox.open_outbox(paths.outbox_path)
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

@click.group("projection-reads")
def projection_reads_group() -> None:
    """Operate the opt-in, guarded projection-backed read path.

    When enabled, some CLI read surfaces (currently `item show`'s event
    history) are served from the cached projection populated by
    `sprintctl sync` instead of backend, with explicit freshness
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
@click.option("--sprint-id", type=str, required=True, help="Sprint ID or repo#id")
@click.option("--item-id", "work_item_id", type=str, default=None, help="Filter by work item ID or repo#id")
@click.option("--type", "event_type", default=None, help="Filter by event type")
@click.option("--knowledge", "knowledge_only", is_flag=True, default=False,
              help="Show only knowledge candidate events (decision, pattern-noted, lesson-learned, risk-accepted)")
@click.option("--limit", default=None, type=int, help="Maximum number of events to return (most recent)")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.pass_obj
def event_list(obj, sprint_id, work_item_id, event_type, knowledge_only, limit, as_json) -> None:
    """List events for a sprint."""
    sprint_id = _apply_scoped_id(obj, sprint_id, field="sprint")
    if work_item_id is not None:
        work_item_id = _apply_scoped_id(obj, work_item_id, field="item")
    if knowledge_only and event_type is not None:
        click.echo("Error: --knowledge and --type are mutually exclusive.", err=True)
        sys.exit(1)
    config = _served_config_or_none(obj)
    if config is not None:
        # ``event list --limit`` means the most recent N events, whereas the
        # catalog's pagination limit selects from the beginning of its ordered
        # result. Fetch the complete sprint stream and apply the CLI's filters
        # below to preserve the established flag semantics.
        result = _run_served(
            "event list",
            _served.read_events,
            config.served_profile,
            repo_id=config.repo_id,
            sprint_id=sprint_id,
            work_item_id=work_item_id,
            after_offset=0,
            limit=None,
            resolved_context=_resolved_context(config),
        )
        events = result["events"]
        if knowledge_only:
            events = [e for e in events if e.get("event_type") in _db.KNOWLEDGE_EVENT_TYPES]
            # The store-backed knowledge query deserializes payloads. The
            # read operation intentionally returns ordinary event rows, so
            # normalize this one flag's established JSON output here.
            events = [{**e, "payload": _event_payload(e)} for e in events]
    else:
        store, m = _get_store(obj)
        if m.get_sprint(store, sprint_id) is None:
            click.echo(f"Sprint #{sprint_id} not found.", err=True)
            sys.exit(1)
        if knowledge_only:
            events = m.list_knowledge_candidates(store, sprint_id)
        else:
            events = m.list_events(store, sprint_id)

    if work_item_id is not None:
        events = [e for e in events if e.get("work_item_id") == work_item_id]
    if not knowledge_only and event_type is not None:
        events = [e for e in events if e.get("event_type") == event_type]
    if limit is not None:
        events = events[-limit:]
    if as_json:
        click.echo(json.dumps(events, indent=2))
        return
    if not events:
        click.echo("No events found.")
        if config is not None:
            click.echo(_render_resolved_context(_resolved_context(config)))
        return
    for e in events:
        item_label = f"  item #{e['work_item_id']}" if e.get("work_item_id") else ""
        click.echo(
            f"#{e['id']}  [{e['event_type']}]  {e['actor']}  "
            f"{e['created_at']}{item_label}"
        )
    if config is not None:
        click.echo(_render_resolved_context(_resolved_context(config)))


# ---------------------------------------------------------------------------



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
    """Attach event and rollout command groups with live runtime seams."""
    global __runtime_source
    __runtime_source = runtime
    _RUNTIME.clear()
    _RUNTIME.update({name: value for name, value in runtime.items() if not name.startswith("__")})
    _sync_runtime()
    for command in (event, authority_commands, projection_reads_group, sync_cmd):
        root.add_command(command)
        _wrap_runtime_callbacks(command)
