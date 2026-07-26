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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from typing import Any, Protocol
from uuid import uuid4

from . import context_contract, contracts, cutover, db, handoff, outbox


CLAIM_COMMAND_TYPES = frozenset(
    {"claim.acquire", "claim.renew", "claim.handoff", "claim.release"}
)
LIFECYCLE_COMMAND_TYPES = frozenset(
    {"item.transition", "item.done", "sprint.activate", "sprint.close"}
)
OBSERVATION_TYPES = frozenset(
    record_type
    for record_type, record_class in contracts.SPRINTCTL_RECORD_TYPE_CLASSES.items()
    if record_class is contracts.RecordClass.OBSERVATION
)
SUPPORTED_BATCH_TYPES = (
    CLAIM_COMMAND_TYPES | LIFECYCLE_COMMAND_TYPES | OBSERVATION_TYPES
)


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
CommandArbiter = Callable[[outbox.OutboxRecord, Mapping[str, str]], Any]
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


@dataclass(slots=True)
class WorkApplication:
    """One repository-scoped work authority application."""

    repo_id: str
    store: Any
    backend: Any
    ingest_records: RecordIngestor
    arbitrate_command: CommandArbiter
    list_records: RecordReader
    list_decisions: DecisionReader
    credential_resolver: CredentialResolver | None = None
    repo_root: Path | None = None

    @classmethod
    def postgres(
        cls,
        store: Any,
        *,
        credential_resolver: CredentialResolver | None = None,
        repo_root: Path | None = None,
    ) -> WorkApplication:
        """Compose the served application from sprintctl's PostgreSQL authority.

        ``store.repo_id`` seeds the instance returned here, but every served
        invocation re-scopes to the calling identity's ``repo_id`` (see
        :meth:`invoke` and :meth:`_scoped_for`); one running application can
        serve every repository tenant a bound identity is authorized for.
        """

        from . import pg  # Lazy: standalone SQLite needs no psycopg.

        return cls(
            repo_id=store.repo_id,
            store=store,
            backend=pg,
            **cls._store_bound_callables(store),
            credential_resolver=credential_resolver,
            repo_root=repo_root,
        )

    @staticmethod
    def _store_bound_callables(store: Any) -> dict[str, Any]:
        from . import authority, pg  # Lazy: standalone SQLite needs no psycopg.

        return {
            "ingest_records": lambda records: pg.ingest_records(store, records),
            "arbitrate_command": lambda record, credentials: authority.arbitrate_command(
                store, record, credentials=credentials
            ),
            "list_records": lambda after, limit: pg.list_ingested_records(
                store, after_offset=after, limit=limit
            ),
            "list_decisions": lambda after, limit: authority.list_authority_decisions(
                store, after_offset=after, limit=limit
            ),
        }

    def _scoped_for(self, repo_id: str) -> WorkApplication:
        """Return a copy of this application bound to ``repo_id`` for one call.

        The underlying connection (``store.conn``) is shared, unchanged from
        today's single-tenant behavior; only the repository scope is
        request-local. When ``store`` is a real :class:`~sprintctl.pg.PgStore`
        (the only backend production composition uses), this rebuilds
        ``store``, ``ingest_records``, ``arbitrate_command``, ``list_records``,
        and ``list_decisions`` so every backend call this copy makes resolves
        against ``repo_id``. Test doubles that pass a bare connection or no
        store at all (``WorkApplication`` also backs local-SQLite and
        unit-test call sites that have no concept of a repo-scoped store) are
        left exactly as constructed; only the ``repo_id`` field is updated for
        them.
        """

        from dataclasses import fields, is_dataclass, replace

        store = self.store
        if is_dataclass(store) and any(field.name == "repo_id" for field in fields(store)):
            scoped_store = replace(store, repo_id=repo_id)
            return replace(
                self,
                repo_id=repo_id,
                store=scoped_store,
                **self._store_bound_callables(scoped_store),
            )
        return replace(self, repo_id=repo_id)

    def invoke(
        self, operation: str, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ApplicationRejection(
                "invalid-arguments", "operation arguments must be an object", 422
            )
        # The server has already authorized context.repo_id against the
        # caller's identity before invoke() runs (vuoro_service.app._dispatch
        # + Identity.authorizes_repo) -- this only needs a value to scope to.
        # A context with no repo_id at all (every existing protocol-v1-only
        # test double, and any caller built before the envelope field
        # existed) falls back to the application's own construction-time
        # repo_id, preserving today's single-tenant behavior exactly.
        requested_repo_id = getattr(context, "repo_id", None) or self.repo_id
        if not requested_repo_id:
            raise ApplicationRejection(
                "repo-id-required",
                "identity is not bound to a repository",
                403,
            )
        target = self._scoped_for(requested_repo_id)
        handlers = {
            "work.read.sprints": target._read_sprints,
            "work.read.item": target._read_item,
            "work.read.items": target._read_items,
            "work.read.claims": target._read_claims,
            "work.read.claim": target._read_claim,
            "work.read.context": target._read_context,
            "work.read.handoff": target._read_handoff,
            "work.read.next-work": target._read_next_work,
            "work.read.next-work-explain": target._read_next_work_explain,
            "work.read.records": target._read_records,
            "work.read.decisions": target._read_decisions,
            "work.read.events": target._read_events,
            "work.read.sprint": target._read_sprint,
            "work.event.add": target._event_add,
            "work.handoff.record": target._handoff_record,
            "work.item.create": target._item_create,
            "work.item.ref.add": target._item_ref_add,
            "work.item.ref.remove": target._item_ref_remove,
            "work.item.dep.add": target._item_dep_add,
            "work.item.dep.remove": target._item_dep_remove,
            "work.claim.start": target._claim_start,
            "work.claim.context": target._claim_context,
            "work.claim.arbitrate": target._claim_arbitrate,
            "work.lifecycle.arbitrate": target._lifecycle_arbitrate,
            "work.evidence.ingest": target._evidence_ingest,
            "work.item.note": target._item_note,
            "work.batch.apply": target._batch_apply,
            "work.pilot.cutover-evidence": target._cutover_evidence,
        }
        try:
            handler = handlers[operation]
        except KeyError as exc:
            raise ApplicationRejection(
                "unknown-work-operation", f"unknown work operation: {operation}", 404
            ) from exc
        try:
            return handler(dict(arguments), context)
        except ApplicationRejection:
            raise
        except ValueError as exc:
            raise ApplicationRejection("validation-failed", str(exc), 422) from exc

    def _read_sprints(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        active_only = bool(arguments.get("active_only", False))
        rows = (
            self.backend.list_active_sprints(self.store)
            if active_only
            else self.backend.list_sprints(self.store)
        )
        if not active_only:
            kinds = {"active_sprint"}
            if arguments.get("include_backlog", False):
                kinds.add("backlog")
            if arguments.get("include_archive", False):
                kinds.add("archive")
            rows = [row for row in rows if row.get("kind", "active_sprint") in kinds]
        return {"repo_id": self.repo_id, "sprints": rows}

    def _read_item(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )
        return {
            "repo_id": self.repo_id,
            "item": item,
            "events": [
                event
                for event in self.backend.list_events(self.store, item["sprint_id"])
                if event.get("work_item_id") == item_id
            ],
            "active_claims": self.backend.list_claims(
                self.store, item_id, active_only=True
            ),
            "refs": self.backend.list_refs(self.store, item_id),
            "deps": {
                "blocked_by": self.backend.list_deps_blocking(self.store, item_id),
                "blocks": self.backend.list_deps_blocked_by(self.store, item_id),
            },
        }

    def _read_items(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        sprint_id = _optional_positive_int(arguments.get("sprint_id"), "sprint_id")
        track_name = _optional_text(arguments.get("track_name"), "track_name")
        status = _optional_text(arguments.get("status"), "status")
        if status is not None and status not in {"pending", "active", "done", "blocked"}:
            raise ApplicationRejection("invalid-arguments", "status must be pending, active, done, or blocked", 422)
        return {"repo_id": self.repo_id, "items": self.backend.list_work_items(
            self.store, sprint_id=sprint_id, track_name=track_name, status=status
        )}

    def _read_claims(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _optional_positive_int(arguments.get("item_id"), "item_id")
        sprint_id = _optional_positive_int(arguments.get("sprint_id"), "sprint_id")
        if item_id is not None and sprint_id is not None:
            raise ApplicationRejection("invalid-arguments", "provide at most one of item_id or sprint_id", 422)
        instance_id = _optional_text(arguments.get("instance_id"), "instance_id")
        runtime_session_id = _optional_text(arguments.get("runtime_session_id"), "runtime_session_id")
        hostname = _optional_text(arguments.get("hostname"), "hostname")
        pid = _optional_positive_int(arguments.get("pid"), "pid")
        if hostname is None and pid is not None:
            raise ApplicationRejection("invalid-arguments", "pid requires hostname", 422)
        active_only = bool(arguments.get("active_only", True))
        identity_query = instance_id or runtime_session_id or hostname
        if identity_query:
            # Domain backend owns canonical (AND-composed) identity matching
            # and intentionally searches the entire repository for resume.
            claims = self.backend.find_claim_by_identity(
                self.store, instance_id=instance_id, runtime_session_id=runtime_session_id,
                hostname=hostname, pid=pid, active_only=active_only,
            )
            if item_id is not None:
                claims = [claim for claim in claims if claim["work_item_id"] == item_id]
            if sprint_id is not None:
                if self.backend.get_sprint(self.store, sprint_id) is None:
                    raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
                item_ids = {item["id"] for item in self.backend.list_work_items(self.store, sprint_id=sprint_id)}
                claims = [claim for claim in claims if claim["work_item_id"] in item_ids]
        elif item_id is not None:
            claims = self.backend.list_claims(self.store, item_id, active_only=active_only)
        elif sprint_id is not None:
            if self.backend.get_sprint(self.store, sprint_id) is None:
                raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
            claims = self.backend.list_claims_by_sprint(self.store, sprint_id, active_only=active_only)
        else:
            sprint = self._resolve_sprint(None)
            claims = self.backend.list_claims_by_sprint(self.store, sprint["id"], active_only=active_only)
        return {"repo_id": self.repo_id, "claims": claims}

    def _read_claim(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Return one claim's inspectable state, never its bearer proof."""
        claim_id = _positive_int(arguments.get("claim_id"), "claim_id")
        claim = self.backend.get_claim(self.store, claim_id, include_secret=False)
        if claim is None:
            raise ApplicationRejection("claim-not-found", f"Claim #{claim_id} not found", 404)
        # Backends must honour include_secret=False; keep this defensive
        # boundary so a serialization regression cannot publish a token.
        claim.pop("claim_token", None)
        return {"repo_id": self.repo_id, "claim": claim}

    def _read_context(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Return ContextContract v1 from one repeatable-read server snapshot.

        This intentionally returns the contract itself, with no transport
        envelope fields: ``usage --context --json`` has a frozen top-level
        shape.  PostgreSQL is the production served backend; the transaction
        makes the several domain reads that feed the aggregate observe one
        point in time instead of exposing a client-composed partial result.
        """
        now = datetime.now(timezone.utc)
        snapshot = getattr(self.backend, "repeatable_read_snapshot", None)
        if callable(snapshot):
            # Never alter the service's shared connection: a prior invocation
            # may have started its implicit non-autocommit transaction.
            with snapshot(self.store) as snapshot_store:
                snapshot_app = replace(self, store=snapshot_store)
                return context_contract.build_context_contract(
                    snapshot_store,
                    snapshot_app._resolve_sprint(arguments.get("sprint_id")),
                    now,
                    backend=self.backend,
                )
        return context_contract.build_context_contract(
            self.store, self._resolve_sprint(arguments.get("sprint_id")), now,
            backend=self.backend,
        )

    def _read_handoff(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        events_limit = _positive_int(arguments.get("events_limit"), "events_limit")
        if events_limit > 500:
            raise ApplicationRejection("invalid-arguments", "events_limit must be at most 500", 422)
        git_context = arguments.get("git_context")
        if git_context is not None and not isinstance(git_context, dict):
            raise ApplicationRejection("invalid-arguments", "git_context must be an object or null", 422)
        sprint = self._resolve_sprint(arguments.get("sprint_id"))
        return handoff.build_handoff_bundle(self.store, sprint, events_limit, backend=self.backend, version=__import__("sprintctl").__version__, git_context=git_context)

    def _handoff_record(self, arguments: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        if self.backend.get_sprint(self.store, sprint_id) is None:
            raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
        bundle = arguments.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("bundle_type") != "handoff" or bundle.get("bundle_version") != "1":
            raise ApplicationRejection("invalid-arguments", "bundle must be a HandoffBundle v1", 422)
        if bundle.get("sprint", {}).get("id") != sprint_id:
            raise ApplicationRejection("invalid-arguments", "bundle sprint must match sprint_id", 422)
        event_id = handoff.record_handoff_generated(self.store, sprint_id, bundle, backend=self.backend, actor=context.identity.actor)
        return {"event_id": event_id, "sprint_id": sprint_id, "actor": context.identity.actor}

    def _item_ref_add(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        ref_type = _optional_text(arguments.get("ref_type"), "ref_type")
        url = _optional_text(arguments.get("url"), "url")
        label = arguments.get("label", "")
        if not ref_type or not url or not isinstance(label, str):
            raise ApplicationRejection("invalid-arguments", "item_id, ref_type, url, and string label are required", 422)
        try:
            ref_id = self.backend.add_ref(self.store, item_id, ref_type, url, label)
        except ValueError as exc:
            raise ApplicationRejection("ref-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "ref_id": ref_id}

    def _item_ref_remove(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        ref_id = _positive_int(arguments.get("ref_id"), "ref_id")
        try:
            self.backend.remove_ref(self.store, ref_id, item_id)
        except ValueError as exc:
            raise ApplicationRejection("ref-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "ref_id": ref_id}

    def _item_dep_add(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        blocked_item_id = _positive_int(arguments.get("blocked_item_id"), "blocked_item_id")
        try:
            dep_id = self.backend.add_dep(self.store, item_id, blocked_item_id)
        except ValueError as exc:
            raise ApplicationRejection("dependency-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "dep_id": dep_id}

    def _item_dep_remove(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        item_id = _positive_int(arguments.get("item_id"), "item_id")
        dep_id = _positive_int(arguments.get("dep_id"), "dep_id")
        try:
            self.backend.remove_dep(self.store, dep_id, item_id)
        except ValueError as exc:
            raise ApplicationRejection("dependency-rejected", str(exc), 422) from exc
        return {"repo_id": self.repo_id, "item_id": item_id, "dep_id": dep_id}

    def _read_events(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        sprint = self.backend.get_sprint(self.store, sprint_id)
        if sprint is None:
            raise ApplicationRejection(
                "sprint-not-found", f"Sprint #{sprint_id} not found", 404
            )
        work_item_id = _optional_positive_int(
            arguments.get("work_item_id"), "work_item_id"
        )
        events = self.backend.list_events(self.store, sprint_id)
        if work_item_id is not None:
            events = [
                event for event in events if event.get("work_item_id") == work_item_id
            ]
        after, limit = _pagination(arguments)
        if after:
            events = events[after:]
        if limit is not None:
            events = events[:limit]
        return {"repo_id": self.repo_id, "events": events}

    def _read_sprint(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        return {"repo_id": self.repo_id, "sprint": self._resolve_sprint(arguments.get("sprint_id"))}

    def _event_add(self, arguments: dict[str, Any], context: InvocationContext) -> dict[str, Any]:
        """Synchronously create a generic event as the authenticated actor."""
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        event_type = _optional_text(arguments.get("event_type"), "event_type")
        if not event_type:
            raise ApplicationRejection("invalid-arguments", "event_type is required", 422)
        if self.backend.get_sprint(self.store, sprint_id) is None:
            raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
        work_item_id = _optional_positive_int(arguments.get("work_item_id"), "work_item_id")
        if work_item_id is not None and self.backend.get_work_item(self.store, work_item_id) is None:
            raise ApplicationRejection("item-not-found", f"Work item #{work_item_id} not found", 404)
        source_type = arguments.get("source_type", "actor")
        if source_type not in {"actor", "daemon", "system"}:
            raise ApplicationRejection("invalid-arguments", "source_type must be actor, daemon, or system", 422)
        payload = arguments.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise ApplicationRejection("invalid-arguments", "payload must be an object or null", 422)
        try:
            event_id = self.backend.create_event(
                self.store, sprint_id, actor=context.identity.actor, event_type=event_type,
                source_type=source_type, work_item_id=work_item_id, payload=payload,
                expected_project=self.repo_id,
            )
        except ValueError as exc:
            raise ApplicationRejection("event-rejected", str(exc)) from exc
        return {"event_id": event_id, "sprint_id": sprint_id, "item_id": work_item_id,
                "type": event_type, "actor": context.identity.actor, "source": source_type}

    def _item_create(self, arguments: dict[str, Any], _context: InvocationContext) -> dict[str, Any]:
        """Create an item and resolve its track in the server-side repository scope."""
        sprint_id = _positive_int(arguments.get("sprint_id"), "sprint_id")
        track_name = _optional_text(arguments.get("track_name"), "track_name")
        title = _optional_text(arguments.get("title"), "title")
        if not track_name or not title:
            raise ApplicationRejection("invalid-arguments", "track_name and title are required", 422)
        if self.backend.get_sprint(self.store, sprint_id) is None:
            raise ApplicationRejection("sprint-not-found", f"Sprint #{sprint_id} not found", 404)
        description = arguments.get("description")
        if description is not None:
            try:
                db.validate_work_item_description(description)
            except ValueError as exc:
                raise ApplicationRejection("invalid-arguments", str(exc), 422) from exc
        assignee = arguments.get("assignee")
        if assignee is not None and not isinstance(assignee, str):
            raise ApplicationRejection("invalid-arguments", "assignee must be a string or null", 422)
        priority = arguments.get("priority")
        try:
            db.validate_priority(priority)
            track_id = self.backend.get_or_create_track(self.store, sprint_id, track_name)
            item_id = self.backend.create_work_item(self.store, sprint_id, track_id, title,
                description=description or "", assignee=assignee, priority=priority)
        except ValueError as exc:
            raise ApplicationRejection("item-create-rejected", str(exc)) from exc
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:  # pragma: no cover - backend postcondition
            raise ApplicationRejection("item-create-failed", "created item could not be read back", 500)
        return {"item": item, "track_name": track_name}

    def _resolve_sprint(
        self, requested: Any, *, prefer_backlog: bool = False
    ) -> dict[str, Any]:
        if requested is not None:
            sprint_id = _positive_int(requested, "sprint_id")
            sprint = self.backend.get_sprint(self.store, sprint_id)
            if sprint is None:
                raise ApplicationRejection(
                    "sprint-not-found", f"Sprint #{sprint_id} not found", 404
                )
            return sprint
        if prefer_backlog:
            backlog = [
                row
                for row in self.backend.list_sprints(self.store)
                if row.get("kind") == "backlog" and row.get("status") != "closed"
            ]
            if len(backlog) == 1:
                return backlog[0]
            if len(backlog) > 1:
                raise ApplicationRejection(
                    "ambiguous-sprint", "multiple open backlog sprints are available"
                )
        active = self.backend.get_active_sprint(self.store)
        if active is None:
            raise ApplicationRejection(
                "sprint-not-found", "no active sprint found", 404
            )
        return active

    def _read_next_work(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        return self.next_work(arguments.get("sprint_id"))

    def _read_next_work_explain(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        """Return the complete, server-assembled next-work explain contract.

        This is intentionally one authority operation.  A served CLI must not
        reproduce this aggregate by opening a local store or by making a
        sequence of independently-versioned read calls.
        """
        sprint = self._resolve_sprint(arguments.get("sprint_id"))
        return _next_work_explain_contract(
            self.backend, self.store, sprint, repo_id=self.repo_id,
            now=datetime.now(timezone.utc),
        )

    def next_work(
        self, sprint_id: Any = None, *, prefer_backlog: bool = False
    ) -> dict[str, Any]:
        sprint = self._resolve_sprint(sprint_id, prefer_backlog=prefer_backlog)
        ready = self.backend.get_ready_items(self.store, sprint["id"])
        return {
            "repo_id": self.repo_id,
            "sprint": sprint,
            "ready_items": ready,
        }

    def _read_records(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        after, limit = _pagination(arguments)
        records = self.list_records(after, limit)
        return {
            "repo_id": self.repo_id,
            "records": [
                {
                    "ingest_offset": int(value.ingest_offset),
                    "record": record_to_dict(value.record),
                }
                for value in records
            ],
        }

    def _read_decisions(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        after, limit = _pagination(arguments)
        return {
            "repo_id": self.repo_id,
            "decisions": [
                _json_value(value) for value in self.list_decisions(after, limit)
            ],
        }

    def _claim_arbitrate(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        return self._arbitrate_one(arguments, context, CLAIM_COMMAND_TYPES)

    def _claim_start(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Create an execute claim and activate its item as one served flow.

        This mirrors the legacy ``claim start`` orchestration while remaining
        independent of Click.  The flow is deliberately not retry-safe: the
        catalog forbids an idempotency key, and durable callers should use an
        immutable ``claim.acquire`` command through ``work.claim.arbitrate``.
        """

        item_id = _positive_int(arguments.get("item_id"), "item_id")
        ttl_seconds = _positive_int(arguments.get("ttl_seconds", 300), "ttl_seconds")
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )

        actor = context.identity.actor
        runtime_session_id = (
            _optional_text(arguments.get("runtime_session_id"), "runtime_session_id")
            or os.environ.get("SPRINTCTL_RUNTIME_SESSION_ID")
            or os.environ.get("CODEX_THREAD_ID")
        )
        instance_id = (
            _optional_text(arguments.get("instance_id"), "instance_id")
            or os.environ.get("SPRINTCTL_INSTANCE_ID")
            or str(uuid4())
        )
        hostname = (
            _optional_text(arguments.get("hostname"), "hostname")
            or socket.gethostname()
        )
        pid = _optional_positive_int(arguments.get("pid"), "pid") or os.getpid()
        previous_status = item["status"]

        try:
            claim_id = self.backend.create_claim(
                self.store,
                work_item_id=item_id,
                agent=actor,
                claim_type="execute",
                exclusive=True,
                ttl_seconds=ttl_seconds,
                branch=_optional_text(arguments.get("branch"), "branch"),
                worktree_path=_optional_text(
                    arguments.get("worktree_path"), "worktree_path"
                ),
                commit_sha=_optional_text(arguments.get("commit_sha"), "commit_sha"),
                pr_ref=_optional_text(arguments.get("pr_ref"), "pr_ref"),
                runtime_session_id=runtime_session_id,
                instance_id=instance_id,
                hostname=hostname,
                pid=pid,
            )
        except ValueError as exc:
            raise ApplicationRejection("claim-start-rejected", str(exc)) from exc

        claim = self.backend.get_claim(self.store, claim_id, include_secret=True)
        if claim is None or not claim.get("claim_token"):
            raise ApplicationRejection(
                "claim-start-result-invalid",
                "created claim is unavailable or has no ownership proof",
                500,
            )

        transitioned = False
        if previous_status != "active":
            try:
                self.backend.set_work_item_status(
                    self.store,
                    item_id,
                    "active",
                    actor=actor,
                    claim_id=claim_id,
                    claim_token=claim["claim_token"],
                )
                transitioned = True
            except Exception as transition_error:
                try:
                    self.backend.release_claim(
                        self.store, claim_id, claim["claim_token"], actor=actor
                    )
                except Exception as release_error:
                    raise ApplicationRejection(
                        "claim-start-rollback-failed",
                        "claim was created, activation failed, and automatic release failed",
                        500,
                    ) from release_error
                raise ApplicationRejection(
                    "claim-start-transition-failed",
                    "claim was released after the item could not be moved to active",
                ) from transition_error

        updated_item = self.backend.get_work_item(self.store, item_id)
        if updated_item is None:
            raise ApplicationRejection(
                "claim-start-result-invalid",
                "claimed item is unavailable after claim start",
                500,
            )
        return {
            "operation": "claim_start",
            "claim_id": claim_id,
            "claim_token": claim["claim_token"],
            "claim": claim,
            "item_id": item_id,
            "item_status_before": previous_status,
            "item_status_after": updated_item["status"],
            "status_transition_applied": transitioned,
            "refs": self.backend.list_refs(self.store, item_id),
        }

    def _claim_context(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Non-secret authority context a served client needs to construct a
        canonical claim command without database access (``work:claim``
        read).

        Returns exactly the "Approved authority-context contract" fields:
        the resolved authenticated actor, Sprintctl's ``repo_id`` plus the
        authority repository UUID, the current non-secret claim snapshot
        (including ``work_item_id``), and the canonical current
        ``claim_revision``.  Never a claim token, a proof digest, another
        identity's bearer credential, or a database DSN.  A missing or
        inaccessible claim is rejected before any producer/outbox record
        could be created -- this handler is read-only.
        """

        from . import authority  # Lazy: standalone SQLite needs no psycopg.

        claim_id = _positive_int(arguments.get("claim_id"), "claim_id")
        claim = self.backend.get_claim(self.store, claim_id, include_secret=False)
        if claim is None:
            raise ApplicationRejection(
                "claim-not-found", f"Claim #{claim_id} not found", 404
            )
        return {
            "repo_id": self.repo_id,
            "authority_repo_uuid": getattr(self.store, "authority_repo_uuid", None),
            "actor": context.identity.actor,
            "claim": claim,
            "claim_revision": authority.claim_revision(claim),
        }

    def _lifecycle_arbitrate(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        return self._arbitrate_one(arguments, context, LIFECYCLE_COMMAND_TYPES)

    def _arbitrate_one(
        self,
        arguments: dict[str, Any],
        context: InvocationContext,
        allowed_types: frozenset[str],
    ) -> dict[str, Any]:
        record = record_from_dict(_required_mapping(arguments.get("record"), "record"))
        record = self._validate_record(record, context, allowed_types)
        if context.basis_revision != record.basis_revision:
            raise ApplicationRejection(
                "basis-revision-mismatch",
                "invocation basis revision must equal the command basis revision",
                422,
            )
        if context.idempotency_key != record.event_id:
            raise ApplicationRejection(
                "idempotency-key-mismatch",
                "idempotency key must equal the immutable command event_id",
                422,
            )
        credentials = self._credentials(context, record)
        return _json_value(self.arbitrate_command(record, credentials))

    def _evidence_ingest(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        records = self._records(arguments, context, OBSERVATION_TYPES)
        self._require_batch_key(records, context)
        results = self.ingest_records(records)
        return {
            "repo_id": self.repo_id,
            "results": [_ingest_result(value) for value in results],
        }

    def _item_note(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        """Record a structured note event on a work item (``item note``).

        Unlike ``work.evidence.ingest``, this is a direct, synchronous write
        (mirrors the local CLI's ``create_event`` call) rather than a durable
        outbox-producer record -- ``item note`` has no local outbox/retry
        semantics either, so this does not invent any for the served path.
        The recording actor is always the authenticated identity, never a
        client-supplied argument, matching ``work.claim.start``.
        """

        item_id = _positive_int(arguments.get("item_id"), "item_id")
        note_type = _optional_text(arguments.get("note_type"), "note_type")
        summary = _optional_text(arguments.get("summary"), "summary")
        if not note_type or not summary:
            raise ApplicationRejection(
                "invalid-arguments", "note_type and summary are required", 422
            )
        item = self.backend.get_work_item(self.store, item_id)
        if item is None:
            raise ApplicationRejection(
                "item-not-found", f"Item #{item_id} not found", 404
            )
        payload: dict[str, Any] = {"summary": summary}
        detail = _optional_text(arguments.get("detail"), "detail")
        if detail:
            payload["detail"] = detail
        tags = arguments.get("tags")
        if tags:
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) and tag for tag in tags
            ):
                raise ApplicationRejection(
                    "invalid-arguments", "tags must be an array of non-empty strings", 422
                )
            payload["tags"] = list(tags)
        evidence_item_id = _optional_positive_int(
            arguments.get("evidence_item_id"), "evidence_item_id"
        )
        if evidence_item_id is not None:
            payload["evidence_item_id"] = evidence_item_id
        evidence_event_id = _optional_positive_int(
            arguments.get("evidence_event_id"), "evidence_event_id"
        )
        if evidence_event_id is not None:
            payload["evidence_event_id"] = evidence_event_id
        for field in ("git_branch", "git_sha", "git_worktree"):
            value = _optional_text(arguments.get(field), field)
            if value:
                payload[field] = value
        try:
            event_id = self.backend.create_event(
                self.store,
                item["sprint_id"],
                actor=context.identity.actor,
                event_type=note_type,
                source_type="actor",
                work_item_id=item_id,
                payload=payload,
            )
        except ValueError as exc:
            raise ApplicationRejection("note-rejected", str(exc)) from exc
        return {
            "event_id": event_id,
            "item_id": item_id,
            "note_type": note_type,
            "summary": summary,
        }

    def _batch_apply(
        self, arguments: dict[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        records = self._records(arguments, context, SUPPORTED_BATCH_TYPES)
        self._require_batch_key(records, context)
        return self.apply_records(records, context)

    def apply_records(
        self, records: Sequence[outbox.OutboxRecord], context: InvocationContext
    ) -> dict[str, Any]:
        """Apply records in producer order; identical retries reuse durable results."""

        results: list[dict[str, Any]] = []
        observations: list[outbox.OutboxRecord] = []

        def flush_observations() -> None:
            if not observations:
                return
            results.extend(
                _ingest_result(value) for value in self.ingest_records(observations)
            )
            observations.clear()

        for record in records:
            if record.record_class == contracts.RecordClass.OBSERVATION.value:
                observations.append(record)
                continue
            flush_observations()
            decision = self.arbitrate_command(
                record, self._credentials(context, record)
            )
            results.append(
                {
                    "kind": "decision",
                    "event_id": record.event_id,
                    **_json_value(decision),
                }
            )
        flush_observations()
        return {"repo_id": self.repo_id, "results": results}

    def _records(
        self,
        arguments: dict[str, Any],
        context: InvocationContext,
        allowed_types: frozenset[str],
    ) -> list[outbox.OutboxRecord]:
        raw = arguments.get("records")
        if not isinstance(raw, list) or not raw:
            raise ApplicationRejection(
                "invalid-record-batch", "records must be a non-empty array", 422
            )
        records = [
            record_from_dict(_required_mapping(value, "record")) for value in raw
        ]
        return [
            self._validate_record(record, context, allowed_types) for record in records
        ]

    def _validate_record(
        self,
        record: outbox.OutboxRecord,
        context: InvocationContext,
        allowed_types: frozenset[str],
    ) -> outbox.OutboxRecord:
        if record.event_type not in allowed_types:
            raise ApplicationRejection(
                "record-type-not-allowed",
                f"record type {record.event_type!r} is not allowed by this operation",
                422,
            )
        expected_class = contracts.record_class_for_type(record.event_type).value
        if record.record_class != expected_class:
            raise ApplicationRejection(
                "record-class-mismatch",
                f"record type {record.event_type!r} must use class {expected_class!r}",
                422,
            )
        encoded_payload = json.dumps(
            record.payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if (
            hashlib.sha256(encoded_payload.encode("utf-8")).hexdigest()
            != record.payload_sha256
        ):
            raise ApplicationRejection(
                "payload-digest-mismatch",
                "record payload digest does not match its canonical payload",
                422,
            )
        if record.actor != context.identity.actor:
            raise ApplicationRejection(
                "actor-mismatch",
                "record actor must match the authenticated identity",
                403,
            )
        if record.record_class == contracts.RecordClass.AUTHORITY_COMMAND.value:
            try:
                envelope = contracts.record_from_dict(record.payload)
            except (TypeError, ValueError) as exc:
                raise ApplicationRejection(
                    "invalid-command-envelope",
                    "record payload is not a valid authority-command envelope",
                    422,
                ) from exc
            if not isinstance(envelope, contracts.AuthorityCommand):
                raise ApplicationRejection(
                    "invalid-command-envelope",
                    "record payload must be an authority-command envelope",
                    422,
                )
            if envelope.to_dict() != record.payload:
                raise ApplicationRejection(
                    "noncanonical-command-envelope",
                    "authority-command envelope must use its canonical form",
                    422,
                )
            if envelope.actor != context.identity.actor:
                raise ApplicationRejection(
                    "actor-mismatch",
                    "outer record, command actor, and authenticated identity must match",
                    403,
                )
            if (
                envelope.record_type == "claim.acquire"
                and envelope.payload["agent"] != context.identity.actor
            ):
                raise ApplicationRejection(
                    "claim-agent-mismatch",
                    "claim agent must match the authenticated identity",
                    403,
                )
            if (
                envelope.event_id != record.event_id
                or envelope.record_type != record.event_type
                or envelope.basis_revision != record.basis_revision
                or envelope.correlation_id != record.correlation_id
                or envelope.causation_id != record.causation_id
                or envelope.authored_at != record.occurred_at
            ):
                raise ApplicationRejection(
                    "noncanonical-command-envelope",
                    "authority-command envelope differs from its outer record",
                    422,
                )
        if (
            record.record_class == contracts.RecordClass.AUTHORITY_COMMAND.value
            and context.basis_revision is not None
            and context.basis_revision != record.basis_revision
        ):
            raise ApplicationRejection(
                "basis-revision-mismatch",
                "invocation basis revision must equal each command basis revision",
                422,
            )
        return record

    def _require_batch_key(
        self, records: Sequence[outbox.OutboxRecord], context: InvocationContext
    ) -> None:
        if context.idempotency_key != batch_idempotency_key(records):
            raise ApplicationRejection(
                "idempotency-key-mismatch",
                "idempotency key must equal the canonical batch digest",
                422,
            )

    def _credentials(
        self, context: InvocationContext, record: outbox.OutboxRecord
    ) -> Mapping[str, str]:
        if self.credential_resolver is None:
            return {}
        resolved = self.credential_resolver(context, record)
        return dict(resolved or {})

    def _cutover_evidence(
        self, arguments: dict[str, Any], _context: InvocationContext
    ) -> dict[str, Any]:
        max_age = arguments.get(
            "max_watermark_age_seconds", cutover.DEFAULT_MAX_WATERMARK_AGE_SECONDS
        )
        max_age = _positive_int(max_age, "max_watermark_age_seconds")
        parity = arguments.get("parity")
        if parity is not None and not isinstance(parity, dict):
            raise ApplicationRejection(
                "invalid-parity", "parity must be an object or null", 422
            )
        return cutover.build_cutover_evidence(
            cwd=self.repo_root,
            repo_root=self.repo_root,
            parity=parity,
            max_watermark_age_seconds=max_age,
            rehearse=bool(arguments.get("rehearse", True)),
        )


@dataclass(frozen=True, slots=True)
class ProjectMemberApplication:
    origin_repo: str
    application: WorkApplication


@dataclass(slots=True)
class ProjectWorkApplication:
    """Deterministic multi-repository work reads and ordered batch dispatch."""

    project_id: str
    members: tuple[ProjectMemberApplication, ...]

    def invoke(
        self, operation: str, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        if operation == "work.project.next-work":
            repositories = []
            ready_items = []
            for member in self.members:
                payload = member.application.next_work(
                    arguments.get("sprint_id"), prefer_backlog=True
                )
                tagged = [
                    {**item, "origin_repo": member.origin_repo}
                    for item in payload["ready_items"]
                ]
                ready_items.extend(tagged)
                repositories.append(
                    {
                        "origin_repo": member.origin_repo,
                        "sprint": {
                            **payload["sprint"],
                            "origin_repo": member.origin_repo,
                        },
                        "ready_items": tagged,
                    }
                )
            return {
                "contract_version": "project-1",
                "project_id": self.project_id,
                "ready_items": ready_items,
                "repositories": repositories,
            }
        if operation == "work.project.batch":
            return self._batch(arguments, context)
        raise ApplicationRejection(
            "unknown-work-operation", f"unknown work operation: {operation}", 404
        )

    def _batch(
        self, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        raw_units = arguments.get("units")
        if not isinstance(raw_units, list) or not raw_units:
            raise ApplicationRejection(
                "invalid-project-batch", "units must be a non-empty array", 422
            )
        by_repo = {member.origin_repo: member.application for member in self.members}
        units: list[tuple[str, list[outbox.OutboxRecord]]] = []
        seen: set[str] = set()
        for raw in raw_units:
            unit = _required_mapping(raw, "unit")
            origin_repo = unit.get("origin_repo")
            if not isinstance(origin_repo, str) or origin_repo not in by_repo:
                raise ApplicationRejection(
                    "unknown-project-member",
                    f"unknown project member: {origin_repo!r}",
                    422,
                )
            if origin_repo in seen:
                raise ApplicationRejection(
                    "duplicate-project-member",
                    f"project batch repeats member {origin_repo!r}",
                    422,
                )
            seen.add(origin_repo)
            raw_records = unit.get("records")
            if not isinstance(raw_records, list) or not raw_records:
                raise ApplicationRejection(
                    "invalid-record-batch",
                    "unit records must be a non-empty array",
                    422,
                )
            records = [
                record_from_dict(_required_mapping(value, "record"))
                for value in raw_records
            ]
            units.append((origin_repo, records))
        declared_order = [member.origin_repo for member in self.members]
        supplied_order = [origin_repo for origin_repo, _records in units]
        expected_order = [
            origin_repo for origin_repo in declared_order if origin_repo in seen
        ]
        if supplied_order != expected_order:
            raise ApplicationRejection(
                "project-order-mismatch",
                "project batch units must follow declared member order",
                422,
            )
        if context.idempotency_key != project_batch_idempotency_key(units):
            raise ApplicationRejection(
                "idempotency-key-mismatch",
                "idempotency key must equal the canonical project-batch digest",
                422,
            )
        validated_units = []
        for origin_repo, records in units:
            application = by_repo[origin_repo]
            validated_records = [
                application._validate_record(record, context, SUPPORTED_BATCH_TYPES)
                for record in records
            ]
            validated_units.append((origin_repo, application, validated_records))

        results = []
        for origin_repo, application, records in validated_units:
            results.append(
                {
                    "origin_repo": origin_repo,
                    **application.apply_records(records, context),
                }
            )
        return {
            "contract_version": "project-batch-1",
            "project_id": self.project_id,
            "results": results,
        }


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


__all__ = [
    "ApplicationRejection",
    "CLAIM_COMMAND_TYPES",
    "LIFECYCLE_COMMAND_TYPES",
    "OBSERVATION_TYPES",
    "ProjectMemberApplication",
    "ProjectWorkApplication",
    "SUPPORTED_BATCH_TYPES",
    "TransientCredentialCarrier",
    "WorkApplication",
    "batch_idempotency_key",
    "make_transient_credential_resolver",
    "project_batch_idempotency_key",
    "record_from_dict",
    "record_to_dict",
]
