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
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from . import contracts, cutover, outbox


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


class InvocationContext(Protocol):
    identity: InvocationIdentity
    request_id: str
    basis_revision: str | None
    catalog_revision: str
    idempotency_requirement: str
    idempotency_key: str | None


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
        """Compose the served application from sprintctl's PostgreSQL authority."""

        from . import authority, pg  # Lazy: standalone SQLite needs no psycopg.

        return cls(
            repo_id=store.repo_id,
            store=store,
            backend=pg,
            ingest_records=lambda records: pg.ingest_records(store, records),
            arbitrate_command=lambda record, credentials: authority.arbitrate_command(
                store, record, credentials=credentials
            ),
            list_records=lambda after, limit: pg.list_ingested_records(
                store, after_offset=after, limit=limit
            ),
            list_decisions=lambda after, limit: authority.list_authority_decisions(
                store, after_offset=after, limit=limit
            ),
            credential_resolver=credential_resolver,
            repo_root=repo_root,
        )

    def invoke(
        self, operation: str, arguments: Mapping[str, Any], context: InvocationContext
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ApplicationRejection(
                "invalid-arguments", "operation arguments must be an object", 422
            )
        handlers = {
            "work.read.sprints": self._read_sprints,
            "work.read.item": self._read_item,
            "work.read.next-work": self._read_next_work,
            "work.read.records": self._read_records,
            "work.read.decisions": self._read_decisions,
            "work.claim.arbitrate": self._claim_arbitrate,
            "work.lifecycle.arbitrate": self._lifecycle_arbitrate,
            "work.evidence.ingest": self._evidence_ingest,
            "work.batch.apply": self._batch_apply,
            "work.pilot.cutover-evidence": self._cutover_evidence,
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
        self._validate_record(record, context, allowed_types)
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
        for record in records:
            self._validate_record(record, context, allowed_types)
        return records

    def _validate_record(
        self,
        record: outbox.OutboxRecord,
        context: InvocationContext,
        allowed_types: frozenset[str],
    ) -> None:
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
        if record.actor != context.identity.actor:
            raise ApplicationRejection(
                "actor-mismatch",
                "record actor must match the authenticated identity",
                403,
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
        results = []
        for origin_repo, records in units:
            application = by_repo[origin_repo]
            for record in records:
                application._validate_record(record, context, SUPPORTED_BATCH_TYPES)
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


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ApplicationRejection(
            "invalid-arguments", f"{field} must be an object", 422
        )
    return value


__all__ = [
    "ApplicationRejection",
    "CLAIM_COMMAND_TYPES",
    "LIFECYCLE_COMMAND_TYPES",
    "OBSERVATION_TYPES",
    "ProjectMemberApplication",
    "ProjectWorkApplication",
    "SUPPORTED_BATCH_TYPES",
    "WorkApplication",
    "batch_idempotency_key",
    "project_batch_idempotency_key",
    "record_from_dict",
    "record_to_dict",
]
