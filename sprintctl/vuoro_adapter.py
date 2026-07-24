"""Vuoro protocol-v1 catalog adapter for the sprintctl work domain.

The module has no import-time dependency on ``vuoro-service``.  Service
composition calls :func:`register_work_catalog` after installing pinned domain
and service releases; standalone sprintctl and its legacy CLI remain usable
without the service distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .application import (
    ApplicationRejection,
    ProjectWorkApplication,
    WorkApplication,
)


WORK_API_VERSION = "work-api/v1"
WORK_SCHEMA_VERSION = "work-schema/v1"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True, slots=True)
class WorkOperationContract:
    name: str
    input_schema: dict[str, Any]
    result_schema: dict[str, Any]
    required_authority: str | None
    execution_semantics: Literal["read", "write", "enqueue", "admin"]
    idempotency: Literal["not-allowed", "optional", "required"]
    required_client_schema_features: tuple[str, ...] = ("json-schema-draft-2020-12",)


def _object_schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
    definitions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    if definitions:
        schema["$defs"] = definitions
    return schema


def _result_schema(
    required: tuple[str, ...], properties: dict[str, Any]
) -> dict[str, Any]:
    return _object_schema(properties, required=required)


_RECORD_DEFINITION: dict[str, Any] = {
    "type": "object",
    "required": [
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
    ],
    "properties": {
        "origin_stream_id": {"type": "string", "minLength": 1},
        "origin_seq": {"type": "integer", "minimum": 1},
        "event_id": {"type": "string", "minLength": 1},
        "schema_version": {"type": "integer", "minimum": 1},
        "record_class": {
            "enum": ["observation", "authority-command"],
        },
        "event_type": {"type": "string", "minLength": 1},
        "actor": {"type": "string", "minLength": 1},
        "runtime_session_id": {"type": ["string", "null"]},
        "occurred_at": {"type": "string", "minLength": 1},
        "basis_revision": {"type": ["string", "null"]},
        "correlation_id": {"type": ["string", "null"]},
        "causation_id": {"type": ["string", "null"]},
        "payload": {"type": "object"},
        "payload_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "created_at": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

_RECORD_INPUT = _object_schema(
    {"record": {"$ref": "#/$defs/record"}},
    required=("record",),
    definitions={"record": _RECORD_DEFINITION},
)
_BATCH_INPUT = _object_schema(
    {
        "records": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/record"},
        }
    },
    required=("records",),
    definitions={"record": _RECORD_DEFINITION},
)

_DECISION_RESULT = _result_schema(
    (
        "request_event_id",
        "decision_event_id",
        "decision_ingest_offset",
        "decision_type",
        "outcome",
        "reason_code",
        "reason_detail",
        "effect",
        "duplicate",
    ),
    {
        "request_event_id": {"type": "string"},
        "decision_event_id": {"type": "string"},
        "decision_ingest_offset": {"type": "integer", "minimum": 1},
        "decision_type": {"type": "string"},
        "outcome": {"enum": ["accepted", "rejected"]},
        "reason_code": {"type": ["string", "null"]},
        "reason_detail": {"type": ["string", "null"]},
        "effect": {"type": "object"},
        "duplicate": {"type": "boolean"},
    },
)

_REPO_RESULTS = _result_schema(
    ("repo_id", "results"),
    {
        "repo_id": {"type": "string"},
        "results": {"type": "array", "items": {"type": "object"}},
    },
)


WORK_OPERATION_CONTRACTS: tuple[WorkOperationContract, ...] = (
    WorkOperationContract(
        "work.read.sprints",
        _object_schema(
            {
                "include_backlog": {"type": "boolean", "default": False},
                "include_archive": {"type": "boolean", "default": False},
                "active_only": {"type": "boolean", "default": False},
            }
        ),
        _result_schema(
            ("repo_id", "sprints"),
            {
                "repo_id": {"type": "string"},
                "sprints": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.item",
        _object_schema(
            {"item_id": {"type": "integer", "minimum": 1}}, required=("item_id",)
        ),
        _result_schema(
            ("repo_id", "item", "events", "active_claims", "refs", "deps"),
            {
                "repo_id": {"type": "string"},
                "item": {"type": "object"},
                "events": {"type": "array", "items": {"type": "object"}},
                "active_claims": {"type": "array", "items": {"type": "object"}},
                "refs": {"type": "array", "items": {"type": "object"}},
                "deps": {"type": "object"},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.next-work",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("repo_id", "sprint", "ready_items"),
            {
                "repo_id": {"type": "string"},
                "sprint": {"type": "object"},
                "ready_items": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    *(
        WorkOperationContract(
            name,
            _object_schema(
                {
                    "after_offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": ["integer", "null"], "minimum": 1},
                }
            ),
            _result_schema(
                ("repo_id", result_key),
                {
                    "repo_id": {"type": "string"},
                    result_key: {"type": "array", "items": {"type": "object"}},
                },
            ),
            "work:read",
            "read",
            "not-allowed",
        )
        for name, result_key in (
            ("work.read.records", "records"),
            ("work.read.decisions", "decisions"),
        )
    ),
    WorkOperationContract(
        "work.claim.start",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "ttl_seconds": {"type": "integer", "minimum": 1, "default": 300},
                "branch": {"type": ["string", "null"], "minLength": 1},
                "worktree_path": {"type": ["string", "null"], "minLength": 1},
                "commit_sha": {"type": ["string", "null"], "minLength": 1},
                "pr_ref": {"type": ["string", "null"], "minLength": 1},
                "runtime_session_id": {"type": ["string", "null"], "minLength": 1},
                "instance_id": {"type": ["string", "null"], "minLength": 1},
                "hostname": {"type": ["string", "null"], "minLength": 1},
                "pid": {"type": ["integer", "null"], "minimum": 1},
            },
            required=("item_id",),
        ),
        _result_schema(
            (
                "operation",
                "claim_id",
                "claim_token",
                "claim",
                "item_id",
                "item_status_before",
                "item_status_after",
                "status_transition_applied",
                "refs",
            ),
            {
                "operation": {"const": "claim_start"},
                "claim_id": {"type": "integer", "minimum": 1},
                "claim_token": {"type": "string", "minLength": 1},
                "claim": {"type": "object"},
                "item_id": {"type": "integer", "minimum": 1},
                "item_status_before": {"type": "string"},
                "item_status_after": {"type": "string"},
                "status_transition_applied": {"type": "boolean"},
                "refs": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:claim",
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.claim.context",
        _object_schema(
            {"claim_id": {"type": "integer", "minimum": 1}}, required=("claim_id",)
        ),
        _result_schema(
            (
                "repo_id",
                "authority_repo_uuid",
                "actor",
                "claim",
                "claim_revision",
            ),
            {
                "repo_id": {"type": "string"},
                "authority_repo_uuid": {"type": ["string", "null"]},
                "actor": {"type": "string"},
                "claim": {"type": "object"},
                "claim_revision": {"type": "string"},
            },
        ),
        "work:claim",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.claim.arbitrate",
        _RECORD_INPUT,
        _DECISION_RESULT,
        "work:claim",
        "write",
        "required",
        ("json-schema-draft-2020-12", "local-defs-ref"),
    ),
    WorkOperationContract(
        "work.lifecycle.arbitrate",
        _RECORD_INPUT,
        _DECISION_RESULT,
        "work:lifecycle",
        "write",
        "required",
        ("json-schema-draft-2020-12", "local-defs-ref"),
    ),
    WorkOperationContract(
        "work.evidence.ingest",
        _BATCH_INPUT,
        _REPO_RESULTS,
        "work:evidence",
        "enqueue",
        "required",
        ("json-schema-draft-2020-12", "local-defs-ref"),
    ),
    WorkOperationContract(
        "work.item.note",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "note_type": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1},
                "detail": {"type": ["string", "null"]},
                "tags": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "minLength": 1},
                },
                "evidence_item_id": {"type": ["integer", "null"], "minimum": 1},
                "evidence_event_id": {"type": ["integer", "null"], "minimum": 1},
                "git_branch": {"type": ["string", "null"]},
                "git_sha": {"type": ["string", "null"]},
                "git_worktree": {"type": ["string", "null"]},
            },
            required=("item_id", "note_type", "summary"),
        ),
        _result_schema(
            ("event_id", "item_id", "note_type", "summary"),
            {
                "event_id": {"type": "integer", "minimum": 1},
                "item_id": {"type": "integer", "minimum": 1},
                "note_type": {"type": "string"},
                "summary": {"type": "string"},
            },
        ),
        "work:evidence",
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.batch.apply",
        _BATCH_INPUT,
        _REPO_RESULTS,
        "work:batch",
        "write",
        "required",
        ("json-schema-draft-2020-12", "local-defs-ref"),
    ),
    WorkOperationContract(
        "work.project.next-work",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("contract_version", "project_id", "ready_items", "repositories"),
            {
                "contract_version": {"const": "project-1"},
                "project_id": {"type": "string"},
                "ready_items": {"type": "array", "items": {"type": "object"}},
                "repositories": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:project-read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.project.batch",
        _object_schema(
            {
                "units": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["origin_repo", "records"],
                        "properties": {
                            "origin_repo": {"type": "string", "minLength": 1},
                            "records": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"$ref": "#/$defs/record"},
                            },
                        },
                        "additionalProperties": False,
                    },
                }
            },
            required=("units",),
            definitions={"record": _RECORD_DEFINITION},
        ),
        _result_schema(
            ("contract_version", "project_id", "results"),
            {
                "contract_version": {"const": "project-batch-1"},
                "project_id": {"type": "string"},
                "results": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:project-write",
        "write",
        "required",
        ("json-schema-draft-2020-12", "local-defs-ref"),
    ),
    WorkOperationContract(
        "work.pilot.cutover-evidence",
        _object_schema(
            {
                "parity": {"type": ["object", "null"]},
                "max_watermark_age_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 300,
                },
                "rehearse": {"type": "boolean", "default": True},
            }
        ),
        _result_schema(
            (
                "contract_version",
                "config",
                "parity",
                "watermark",
                "stale_tools",
                "rollback_rehearsal",
                "promotable",
                "blockers",
            ),
            {
                "contract_version": {"type": "string"},
                "config": {"type": "object"},
                "parity": {"type": ["object", "null"]},
                "watermark": {"type": "object"},
                "stale_tools": {"type": "object"},
                "rollback_rehearsal": {"type": ["object", "null"]},
                "promotable": {"type": "boolean"},
                "blockers": {"type": "array", "items": {"type": "string"}},
            },
        ),
        "work:pilot-read",
        "read",
        "not-allowed",
    ),
)


LEGACY_REMOTE_COMMAND_PARITY: tuple[dict[str, str], ...] = (
    {"legacy": "sprintctl sprint list --json", "operation": "work.read.sprints"},
    {"legacy": "sprintctl item show --id ID --json", "operation": "work.read.item"},
    {"legacy": "sprintctl next-work --json", "operation": "work.read.next-work"},
    {
        "legacy": "sprintctl claim start",
        "operation": "work.claim.start",
    },
    {
        "legacy": "sprintctl claim heartbeat|handoff|release",
        "operation": "work.claim.arbitrate",
    },
    {
        "legacy": "sprintctl item status / sprint status",
        "operation": "work.lifecycle.arbitrate",
    },
    {"legacy": "sprintctl authority sync", "operation": "work.batch.apply"},
    {"legacy": "sprintctl event observation add", "operation": "work.evidence.ingest"},
    {"legacy": "sprintctl item note", "operation": "work.item.note"},
    {"legacy": "sprintctl next-work --project", "operation": "work.project.next-work"},
    {"legacy": "project dispatch batching", "operation": "work.project.batch"},
    {
        "legacy": "sprintctl pilot cutover-evidence",
        "operation": "work.pilot.cutover-evidence",
    },
)


def register_work_catalog(
    registry: Any,
    application: WorkApplication,
    *,
    project_application: ProjectWorkApplication | None = None,
) -> None:
    """Register the complete work operation catalog in a Vuoro registry."""

    from vuoro_service.catalog import OperationRejectedError
    from vuoro_service.contracts import OperationDefinition

    for contract in WORK_OPERATION_CONTRACTS:
        # work.project.* operations aggregate across a project's member
        # repos using an origin_repo field inside their own arguments (see
        # ProjectWorkApplication) -- they have no single repo_id to scope
        # to, so they stay outside the envelope-level repo_id/authorization
        # gate that every other work.* operation requires.
        definition = OperationDefinition(
            name=contract.name,
            owning_domain="work",
            input_schema=contract.input_schema,
            result_schema=contract.result_schema,
            required_authority=contract.required_authority,
            execution_semantics=contract.execution_semantics,
            idempotency=contract.idempotency,
            repo_scoped=not contract.name.startswith("work.project."),
            required_client_schema_features=list(
                contract.required_client_schema_features
            ),
        )

        def handler(
            arguments: Any, context: Any, *, operation: str = contract.name
        ) -> Any:
            try:
                if operation.startswith("work.project."):
                    if project_application is None:
                        raise ApplicationRejection(
                            "project-unavailable",
                            "no project application is configured",
                            503,
                        )
                    return project_application.invoke(operation, arguments, context)
                return application.invoke(operation, arguments, context)
            except ApplicationRejection as error:
                raise OperationRejectedError(
                    error.code, error.message, http_status=error.http_status
                ) from error

        registry.register(definition, handler)


__all__ = [
    "LEGACY_REMOTE_COMMAND_PARITY",
    "SCHEMA_DIALECT",
    "WORK_API_VERSION",
    "WORK_OPERATION_CONTRACTS",
    "WORK_SCHEMA_VERSION",
    "WorkOperationContract",
    "register_work_catalog",
]
