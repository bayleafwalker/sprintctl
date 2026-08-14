"""Click-independent served-work application handlers.

The legacy CLI and this module deliberately share the domain-owned backend,
record, and authority-command implementations.  This layer only translates a
transport invocation into those canonical operations and returns JSON-safe
results with stable rejection codes.

Shared-authority writes are expressed as immutable producer records.  Their
``event_id`` / stream position is the durable idempotency identity already
owned by :mod:`sprintctl.pg` and :mod:`sprintctl.authority`; this module does
not add a second request ledger or a second state machine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from . import context_candidates, context_contract, contracts, db, handoff, maintain, outbox, sprint_detail
from .maintenance_capability import (
    MaintenanceCapabilityError,
    PostgresMaintenanceCapabilityStore,
    SQLiteMaintenanceCapabilityStore,
    StaleCapabilityRevision,
)
from .maintenance_resource import CursorExpired, MaintenanceResourceStore, ResourceNotFound


CLAIM_COMMAND_TYPES = frozenset(
    {"claim.acquire", "claim.renew", "claim.handoff", "claim.release"}
)
LIFECYCLE_COMMAND_TYPES = frozenset(
    {"item.transition", "item.done", "item.done-from-claim", "sprint.activate", "sprint.close"}
)
OBSERVATION_TYPES = frozenset(
    record_type
    for record_type, record_class in contracts.SPRINTCTL_RECORD_TYPE_CLASSES.items()
    if record_class is contracts.RecordClass.OBSERVATION
)
SUPPORTED_BATCH_TYPES = (
    CLAIM_COMMAND_TYPES | LIFECYCLE_COMMAND_TYPES | OBSERVATION_TYPES
)

# A connection termination can arrive after PostgreSQL has accepted a command
# but before the service receives its result.  Only this explicit subset has a
# durable idempotency identity owned by the domain authority; ordinary writes
# such as item edits, notes, and claim start must never be replayed here.
_ADMIN_SHUTDOWN_IDEMPOTENT_OPERATIONS = frozenset(
    {
        "work.claim.arbitrate",
        "work.lifecycle.arbitrate",
        "work.evidence.ingest",
        "work.batch.apply",
        "work.maintenance.prepare",
        "work.maintenance.transition",
        "work.maintenance.recovery-record",
        "work.maintenance.resource.prepare",
    }
)
_ADMIN_SHUTDOWN_READ_OPERATIONS = frozenset(
    {
        "work.identity.current",
        "work.claim.context",
        "work.maintain.check",
        "work.maintenance.resource.get",
        "work.maintenance.resource.changes",
    }
)
_POSTGRES_ADMIN_SHUTDOWN_SQLSTATE = "57P01"


class InvocationIdentity(Protocol):
    actor: str
    environment: str
    authorities: frozenset[str]


class TransientCredentialCarrier(Protocol):
    """Duck-typed shape of Vuoro's ``invocation/v2`` transient-proof carrier.

    Matches ``vuoro_service.identity.TransientCredentials``: bindings are
    keyed by non-secret ``sha256:<64-lowercase-hex>`` references and are only
    ever readable through ``reveal`` -- never iterated, logged, or cached as
    a plain mapping.
    """

    def reveal(self, key: str) -> str | None: ...


class InvocationContext(Protocol):
    identity: InvocationIdentity
    request_id: str
    basis_revision: str | None
    catalog_revision: str
    idempotency_requirement: str
    idempotency_key: str | None
    # Client-supplied repository scope for this one call (the server has
    # already authorized it against the identity before invoke() runs --
    # see vuoro_service.app._dispatch). None on every existing
    # protocol-v1-only test double that predates the envelope field.
    repo_id: str | None
    # Present on a v2 invocation; absent (or empty) on v1 and on every
    # existing protocol-v1-only test double.  Composition wiring is what
    # supplies a real carrier -- see ``make_transient_credential_resolver``.
    transient_credentials: TransientCredentialCarrier | None


@dataclass(frozen=True, slots=True)
class ApplicationRejection(Exception):
    """A stable caller-visible rejection, not an infrastructure failure."""

    code: str
    message: str
    http_status: int = 409

    def __str__(self) -> str:
        return self.message


CredentialResolver = Callable[
    [InvocationContext, outbox.OutboxRecord], Mapping[str, str] | None
]
RecordIngestor = Callable[[list[outbox.OutboxRecord]], Sequence[Any]]
CommandArbiter = Callable[[outbox.OutboxRecord, Mapping[str, str], str | None], Any]
RecordReader = Callable[[int, int | None], Sequence[Any]]
DecisionReader = Callable[[int, int | None], Sequence[Any]]


_OUTBOX_FIELDS = frozenset(
    {
        "origin_stream_id",
        "origin_seq",
        "event_id",
        "schema_version",
        "record_class",
        "event_type",
        "actor",
        "runtime_session_id",
        "occurred_at",
        "basis_revision",
        "correlation_id",
        "causation_id",
        "payload",
        "payload_sha256",
        "created_at",
    }
)


def record_from_dict(value: Mapping[str, Any]) -> outbox.OutboxRecord:
    """Parse the strict portable producer-record shape used by served work."""

    if not isinstance(value, Mapping):
        raise ApplicationRejection("invalid-record", "record must be an object", 422)
    unknown = sorted(set(value) - _OUTBOX_FIELDS)
    missing = sorted(_OUTBOX_FIELDS - set(value))
    if unknown:
        raise ApplicationRejection(
            "invalid-record", "record has unknown fields: " + ", ".join(unknown), 422
        )
    if missing:
        raise ApplicationRejection(
            "invalid-record", "record is missing fields: " + ", ".join(missing), 422
        )
    try:
        record = outbox.OutboxRecord(**dict(value))
    except TypeError as exc:
        raise ApplicationRejection(
            "invalid-record", "record shape is invalid", 422
        ) from exc
    if isinstance(record.origin_seq, bool) or not isinstance(record.origin_seq, int):
        raise ApplicationRejection(
            "invalid-record", "record origin_seq must be a positive integer", 422
        )
    if record.origin_seq < 1:
        raise ApplicationRejection(
            "invalid-record", "record origin_seq must be a positive integer", 422
        )
    if not isinstance(record.payload, dict):
        raise ApplicationRejection(
            "invalid-record", "record payload must be an object", 422
        )
    return record


def record_to_dict(record: outbox.OutboxRecord) -> dict[str, Any]:
    return {
        "origin_stream_id": record.origin_stream_id,
        "origin_seq": record.origin_seq,
        "event_id": record.event_id,
        "schema_version": record.schema_version,
        "record_class": record.record_class,
        "event_type": record.event_type,
        "actor": record.actor,
        "runtime_session_id": record.runtime_session_id,
        "occurred_at": record.occurred_at,
        "basis_revision": record.basis_revision,
        "correlation_id": record.correlation_id,
        "causation_id": record.causation_id,
        "payload": json.loads(json.dumps(record.payload)),
        "payload_sha256": record.payload_sha256,
        "created_at": record.created_at,
    }


def batch_idempotency_key(records: Sequence[outbox.OutboxRecord]) -> str:
    """Return the content-bound key required for a record batch invocation."""

    canonical = [record_to_dict(record) for record in records]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def project_batch_idempotency_key(
    units: Sequence[tuple[str, Sequence[outbox.OutboxRecord]]],
) -> str:
    canonical = [
        {
            "origin_repo": origin_repo,
            "records": [record_to_dict(record) for record in records],
        }
        for origin_repo, records in units
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "record") and hasattr(value, "ingest_offset"):
        return {
            "record": record_to_dict(value.record),
            "ingest_offset": int(value.ingest_offset),
        }
    return json.loads(json.dumps(value))


def _ingest_result(value: Any) -> dict[str, Any]:
    record = value.record
    return {
        "kind": "record",
        "event_id": record.event_id,
        "event_type": record.event_type,
        "ingest_offset": int(value.ingest_offset),
        "duplicate": bool(value.duplicate),
    }


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _dependency_waiting_items(backend: Any, store: Any, sprint_id: int) -> list[dict]:
    waiting: list[dict] = []
    for item in backend.list_work_items(store, sprint_id=sprint_id, status="pending"):
        unresolved = [
            blocker for blocker in backend.list_deps_blocking(store, item["id"])
            if blocker["blocker_status"] != "done"
        ]
        if unresolved:
            waiting.append({
                "id": item["id"], "title": item["title"], "track": item["track_name"],
                "assignee": item.get("assignee"), "unresolved_blockers": len(unresolved),
                "unresolved_blocker_ids": [row["item_id"] for row in unresolved],
                "unresolved_blocker_titles": [row["blocker_title"] for row in unresolved],
            })
    return waiting


def _derive_next_work_conflicts(
    active_claims: list[dict], active_unclaimed: list[dict], waiting: list[dict], now: datetime
) -> list[dict]:
    conflicts: list[dict] = []
    legacy = [claim for claim in active_claims if claim.get("identity_status") != "proven"]
    if legacy:
        conflicts.append({"kind": "claim-identity", "severity": "warning", "summary": f"{len(legacy)} active claim(s) have ambiguous ownership proof and require explicit adoption or expiry.", "claim_ids": [claim["claim_id"] for claim in legacy], "item_ids": [claim["work_item_id"] for claim in legacy]})
    expiring = [claim for claim in active_claims if (expires := _parse_utc_timestamp(claim.get("expires_at"))) is not None and (expires - now).total_seconds() <= 120]
    if expiring:
        conflicts.append({"kind": "claim-expiry", "severity": "warning", "summary": f"{len(expiring)} active claim(s) expire within 120 seconds and may need heartbeat or handoff.", "claim_ids": [claim["claim_id"] for claim in expiring], "item_ids": [claim["work_item_id"] for claim in expiring]})
    if active_unclaimed:
        conflicts.append({"kind": "unclaimed-active-work", "reason_code": "active-item-without-live-claim", "severity": "warning", "summary": f"{len(active_unclaimed)} active item(s) have no live claim and need resume, handoff, or status triage.", "item_ids": [item["id"] for item in active_unclaimed]})
    if waiting:
        conflicts.append({"kind": "dependency-blocked", "severity": "warning", "summary": f"{len(waiting)} pending item(s) are waiting on unresolved blockers.", "item_ids": [item["id"] for item in waiting], "blocker_ids": sorted({blocker for item in waiting for blocker in item["unresolved_blocker_ids"]})})
    return conflicts


def _next_work_action(active_claims: list[dict], active_unclaimed: list[dict], conflicts: list[dict], ready: list[dict], waiting: list[dict]) -> dict:
    if conflicts:
        first = conflicts[0]
        if first["kind"] == "claim-identity":
            return {"kind": "resolve-claim-identity", "summary": "Resolve ambiguous active claim ownership before resuming or starting new work.", "claim_id": first["claim_ids"][0], "item_id": first["item_ids"][0], "reason": first["summary"]}
        if first["kind"] == "claim-expiry":
            return {"kind": "refresh-claim", "summary": "Heartbeat or hand off the next expiring claim before it lapses.", "claim_id": first["claim_ids"][0], "item_id": first["item_ids"][0], "reason": first["summary"]}
        if first["kind"] == "unclaimed-active-work":
            item = active_unclaimed[0]
            return {"kind": "resume-unclaimed-active-item", "summary": f"Resume or triage active item #{item['id']} because it has no live claim.", "item_id": item["id"], "reason": first["summary"]}
        waiting_item = waiting[0]
        return {"kind": "unblock-dependent-work", "summary": f"Resolve blocker #{waiting_item['unresolved_blocker_ids'][0]} to unblock item #{waiting_item['id']}.", "item_id": waiting_item["id"], "blocker_item_id": waiting_item["unresolved_blocker_ids"][0], "reason": first["summary"]}
    if active_claims:
        claim = active_claims[0]
        return {"kind": "inspect-active-claim", "summary": f"Inspect claimed item #{claim['work_item_id']} before starting new work.", "claim_id": claim["claim_id"], "item_id": claim["work_item_id"], "reason": "Active claimed work already exists in this sprint."}
    if ready:
        item = ready[0]
        return {"kind": "start-ready-item", "summary": f"Start ready item #{item['id']} because it is unblocked and no active claims are open.", "item_id": item["id"], "reason": "Ready work is available now."}
    if waiting:
        item = waiting[0]
        return {"kind": "resolve-blocker", "summary": f"Resolve blocker #{item['unresolved_blocker_ids'][0]} to unblock item #{item['id']}.", "item_id": item["id"], "blocker_item_id": item["unresolved_blocker_ids"][0], "reason": "All pending work is currently waiting on dependencies."}
    return {"kind": "no-action", "summary": "No immediate action is suggested from current sprint state.", "reason": "There is no ready, active, blocked, or stale work to prioritize."}


def _scoped_ref(repo_id: str | None, identifier: int) -> str:
    return f"{repo_id}#{identifier}" if repo_id else str(identifier)


def _next_work_commands(sprint_id: int, action: dict, repo_id: str | None) -> list[str]:
    kind, item_id, claim_id, blocker_id = (action.get(key) for key in ("kind", "item_id", "claim_id", "blocker_item_id"))
    item_ref = lambda value: _scoped_ref(repo_id, value)
    if kind == "resolve-claim-identity":
        return ["sprintctl claim resume --json", *([f"sprintctl claim handoff --id {claim_id} --actor <name> --mode rotate --allow-legacy-adopt --json"] if claim_id is not None else [])]
    if kind == "refresh-claim":
        return [] if claim_id is None else [f"sprintctl claim heartbeat --id {claim_id} --claim-token <token> --ttl 600 --actor <name>", f"sprintctl claim handoff --id {claim_id} --claim-token <token> --actor <next-agent> --mode rotate --json"]
    if kind in {"unblock-dependent-work", "resolve-blocker"}:
        commands = ([f"sprintctl item show --id {item_ref(blocker_id)}"] if blocker_id is not None else []) + ([f"sprintctl item show --id {item_ref(item_id)}"] if item_id is not None else [])
        return [*commands, f"sprintctl next-work --sprint-id {_scoped_ref(repo_id, sprint_id)} --json --explain"]
    if kind == "inspect-active-claim":
        return ([f"sprintctl item show --id {item_ref(item_id)}"] if item_id is not None else []) + ([] if claim_id is None else [f"sprintctl claim heartbeat --id {claim_id} --claim-token <token> --ttl 600 --actor <name>", f"sprintctl claim handoff --id {claim_id} --claim-token <token> --actor <next-agent> --mode rotate --json"])
    if kind in {"resume-unclaimed-active-item", "start-ready-item"}:
        return [] if item_id is None else [f"sprintctl claim start --item-id {item_ref(item_id)} --actor <name> --ttl 600 --json", f"sprintctl item show --id {item_ref(item_id)}"]
    if kind == "no-action":
        sprint_ref = _scoped_ref(repo_id, sprint_id)
        return [f"sprintctl usage --context --sprint-id {sprint_ref} --json", f"sprintctl next-work --sprint-id {sprint_ref} --json --explain"]
    return []


def _command_step_kind(command: str) -> str:
    for prefix, kind in (("sprintctl claim start", "claim-start"), ("sprintctl claim resume", "claim-resume"), ("sprintctl claim heartbeat", "claim-heartbeat"), ("sprintctl claim handoff", "claim-handoff"), ("sprintctl item show", "item-show"), ("sprintctl usage --context", "usage-context"), ("sprintctl next-work", "next-work")):
        if command.startswith(prefix): return kind
    return "other"


def _next_work_explain_contract(backend: Any, store: Any, sprint: dict, *, repo_id: str | None, now: datetime) -> dict:
    ready = backend.get_ready_items(store, sprint["id"])
    waiting = _dependency_waiting_items(backend, store, sprint["id"])
    active_claims = backend.list_claims_by_sprint(store, sprint["id"], active_only=True)
    active_items = [{"id": item["id"], "title": item["title"], "track": item["track_name"]} for item in backend.list_work_items(store, sprint_id=sprint["id"], status="active")]
    claimed_ids = {claim["work_item_id"] for claim in active_claims}
    active_unclaimed = [item for item in active_items if item["id"] not in claimed_ids]
    conflicts = _derive_next_work_conflicts(active_claims, active_unclaimed, waiting, now)
    action = _next_work_action(active_claims, active_unclaimed, conflicts, ready, waiting)
    commands = _next_work_commands(sprint["id"], action, repo_id)
    refs = backend.list_refs_for_items(store, [item["id"] for item in ready])
    return {"contract_version": "1", "sprint": {key: sprint[key] for key in ("id", "name", "status")}, "summary": {"pending_total": len(ready) + len(waiting), "ready": len(ready), "waiting_on_dependencies": len(waiting), "active_claims": len(active_claims), "active_unclaimed": len(active_unclaimed)}, "ready_items": [{**item, "reason_code": "ready-unblocked", "reason": "No unresolved blocking dependencies.", "refs": refs.get(item["id"], [])} for item in ready], "dependency_waiting_items": [{**item, "reason_code": "waiting-on-dependencies", "reason": "One or more blocking dependencies are not done."} for item in waiting], "active_claims": [{key: claim.get(key) for key in ("claim_id", "work_item_id", "agent", "claim_type", "expires_at", "identity_status")} for claim in active_claims], "active_unclaimed_items": active_unclaimed, "conflicts": conflicts, "next_action": action, "recommended_commands": commands, "recommended_command_bundle": {"bundle_version": "1", "next_action_kind": action.get("kind"), "steps": [{"step": index, "kind": _command_step_kind(command), "command": command, "placeholders": re.findall(r"<[^>\n]+>", command), "requires_input": bool(re.findall(r"<[^>\n]+>", command)), "is_executable": not bool(re.findall(r"<[^>\n]+>", command))} for index, command in enumerate(commands, 1)]}}

def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApplicationRejection(
            "invalid-arguments", f"{field} must be a positive integer", 422
        )
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApplicationRejection(
            "invalid-arguments", f"{field} must be a non-negative integer", 422
        )
    return value


def _pagination(arguments: Mapping[str, Any]) -> tuple[int, int | None]:
    after = _non_negative_int(arguments.get("after_offset", 0), "after_offset")
    raw_limit = arguments.get("limit")
    limit = None if raw_limit is None else _positive_int(raw_limit, "limit")
    return after, limit


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApplicationRejection(
            "invalid-arguments", f"{field} must be a non-empty string or null", 422
        )
    return value


def _required_text(value: Any, field: str) -> str:
    result = _optional_text(value, field)
    if result is None:
        raise ApplicationRejection(
            "invalid-arguments", f"{field} must be a non-empty string", 422
        )
    return result


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApplicationRejection(
            "invalid-arguments", f"{field} must be an object", 422
        )
    return value


_CLAIM_CREDENTIAL_REF_FIELDS: tuple[str, ...] = (
    "credential_ref",
    "proposed_credential_ref",
    "coordinate_credential_ref",
)


def make_transient_credential_resolver() -> CredentialResolver:
    """Compose Sprintctl's credential resolver over a v2 transient-proof carrier.

    Per the Vuoro claim-proof transport clarification's approved transport
    contract: "service composition supplies Sprintctl's credential resolver,
    which returns only bindings referenced by the validated immutable
    command." The returned callable reads ``context.transient_credentials``
    (a duck-typed :class:`TransientCredentialCarrier` -- satisfied today by
    ``vuoro_service.identity.TransientCredentials`` on a real ``invocation/v2``
    request) and reveals only the ``sha256:<64-lowercase-hex>`` refs the
    record's own payload actually names, through ``credential_ref`` /
    ``proposed_credential_ref`` / ``coordinate_credential_ref``.

    The rehash-and-compare that turns a revealed proof into an accepted or
    rejected effect is left exactly where it already lives --
    ``authority._resolve_credential`` / ``authority._verify_claim_secret``,
    invoked downstream by ``arbitrate_command``.  This resolver only ever
    hands back what the payload already asked for; it does not verify,
    cache, log, or otherwise widen access to a revealed proof.

    This module has no import-time or call-time dependency on anything
    Vuoro-owned: it only assumes the ``reveal(key) -> str | None`` duck type
    documented on :class:`TransientCredentialCarrier`.  A context without a
    transient carrier -- a v1 invocation, or any existing test double built
    before v2 -- resolves to no credentials, i.e. today's no-resolver
    behaviour.
    """

    def resolve(
        context: InvocationContext, record: outbox.OutboxRecord
    ) -> Mapping[str, str] | None:
        carrier = getattr(context, "transient_credentials", None)
        if carrier is None:
            return None
        payload: Any = record.payload
        if record.record_class == contracts.RecordClass.AUTHORITY_COMMAND.value:
            inner = payload.get("payload") if isinstance(payload, Mapping) else None
            if isinstance(inner, Mapping):
                payload = inner
        if not isinstance(payload, Mapping):
            return None
        resolved: dict[str, str] = {}
        for field in _CLAIM_CREDENTIAL_REF_FIELDS:
            ref = payload.get(field)
            if not isinstance(ref, str):
                continue
            proof = carrier.reveal(ref)
            if proof is not None:
                resolved[ref] = proof
        return resolved

    return resolve


# Export shared names (including private compatibility helpers) to the
# service modules that compose on top of this layer.
__all__ = [name for name in globals() if not name.startswith("__")]
