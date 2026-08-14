"""Vuoro protocol-v1 catalog adapter for the sprintctl work domain.

The module has no import-time dependency on ``vuoro-service``.  Service
composition calls :func:`register_work_catalog` after installing pinned domain
and service releases; standalone sprintctl and its legacy CLI remain usable
without the service distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from vuoro_adapter_kit import (
    SCHEMA_DIALECT as _ADAPTER_SCHEMA_DIALECT,
    SCHEMA_FEATURES as _ADAPTER_SCHEMA_FEATURES,
    object_schema,
    operation_spec,
)

from .application import (
    ApplicationRejection,
    ProjectWorkApplication,
    WorkApplication,
)


WORK_API_VERSION = "work-api/v1"
WORK_SCHEMA_VERSION = "work-schema/v1"
SCHEMA_DIALECT = _ADAPTER_SCHEMA_DIALECT
SCHEMA_FEATURES = _ADAPTER_SCHEMA_FEATURES


@dataclass(frozen=True, slots=True)
class WorkOperationContract:
    name: str
    input_schema: dict[str, Any]
    result_schema: dict[str, Any]
    required_authority: str | None
    execution_semantics: Literal["read", "write", "enqueue", "admin"]
    idempotency: Literal["not-allowed", "optional", "required"]
    required_client_schema_features: tuple[str, ...] = SCHEMA_FEATURES
    result_contract_resource_kind: str | None = None


_object_schema = object_schema


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

_CAPABILITY_ID_SCHEMA = {
    "type": "string",
    "pattern": "^mcap:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
}
_SHA256_REF_SCHEMA = {
    "type": "string",
    "pattern": "^(artifact:)?sha256:[0-9a-f]{64}$",
}
_MAINTENANCE_ENVELOPE_SCHEMA = {
    "type": "object",
    "required": [
        "contract_id", "envelope_id", "plan_ref", "issued_at", "window",
        "operator", "repositories", "command_registry_ref", "command_registry",
        "operations", "jit_fields", "jit_bindings", "start_gate", "steps",
        "abort", "recovery_policy", "audit_reconciliation",
    ],
    "properties": {
        "contract_id": {"const": "maintenance-envelope/v1"},
        "envelope_id": {"type": "string", "minLength": 1},
        "plan_ref": {"type": "string", "pattern": "^artifact:sha256:[0-9a-f]{64}$"},
        "issued_at": {"type": "string", "format": "date-time"},
        "window": {"type": "object"},
        "operator": {"type": "object"},
        "repositories": {"type": "array", "minItems": 1},
        "command_registry_ref": {"type": "string", "pattern": "^artifact:sha256:[0-9a-f]{64}$"},
        "command_registry": {"type": "array", "minItems": 1},
        "operations": {"type": "array", "minItems": 1},
        "jit_fields": {"type": "array", "minItems": 3, "maxItems": 3},
        "jit_bindings": {"type": "array"},
        "start_gate": {"type": "object"},
        "steps": {"type": "array", "minItems": 1},
        "abort": {"type": "object"},
        "recovery_policy": {"type": "object"},
        "audit_reconciliation": {"type": "object"},
    },
    "additionalProperties": False,
}
_MAINTENANCE_EFFECT_RESULT = _result_schema(
    ("repo_id", "capability_id", "request_id", "action", "outcome", "state", "revision", "duplicate"),
    {
        "repo_id": {"type": "string"},
        "capability_id": _CAPABILITY_ID_SCHEMA,
        "request_id": {"type": "string", "format": "uuid"},
        "action": {"enum": ["prepare", "attest", "activate", "observe", "reconcile", "abort", "revoke"]},
        "outcome": {"enum": ["accepted", "rejected", "duplicate", "expired", "aborted", "incomplete"]},
        "state": {"enum": ["prepared", "attested", "active", "observing", "reconciled", "aborted", "revoked", "expired"]},
        "revision": {"type": "string", "minLength": 1},
        "duplicate": {"type": "boolean"},
    },
)
_RESOURCE_REFERENCE_RESULT = _result_schema(
    ("schema_version", "owner", "resource_kind", "reference", "revision"),
    {
        "schema_version": {"const": "resource-reference/v1"},
        "owner": {"const": "work"},
        "resource_kind": {"const": "work.maintenance-capability"},
        "reference": {"type": "string", "pattern": "^smr1_[A-Za-z0-9_-]{43}$"},
        "revision": {"type": "string", "minLength": 1},
    },
)
_RESOURCE_SNAPSHOT_RESULT = _result_schema(
    ("schema_version", "reference", "revision", "cursor", "terminal", "state"),
    {
        "schema_version": {"const": "resource-snapshot/v1"},
        "reference": {"type": "string", "pattern": "^smr1_[A-Za-z0-9_-]{43}$"},
        "revision": {"type": "string", "minLength": 1},
        "cursor": {"type": "string", "minLength": 1},
        "terminal": {"type": "boolean"},
        "state": _object_schema(
            {"state": {"enum": ["prepared", "attested", "active", "observing", "reconciled", "aborted", "revoked", "expired"]}, "not_before": {"type": "string"}, "expires_at": {"type": "string"}, "updated_at": {"type": "string"}},
            required=("state", "not_before", "expires_at", "updated_at"),
        ),
    },
)
_RESOURCE_CHANGES_RESULT = _result_schema(
    ("schema_version", "reference", "next_cursor", "events"),
    {
        "schema_version": {"const": "resource-changes/v1"},
        "reference": {"type": "string", "pattern": "^smr1_[A-Za-z0-9_-]{43}$"},
        "next_cursor": {"type": "string", "minLength": 1},
        "events": {"type": "array", "maxItems": 100, "items": _object_schema(
            {"event_id": {"type": "string", "minLength": 1}, "terminal": {"type": "boolean"}, "data": _object_schema({"state": {"type": "string"}, "updated_at": {"type": "string"}}, required=("state", "updated_at"))},
            required=("event_id", "terminal", "data"),
        )},
    },
)


WORK_OPERATION_CONTRACTS: tuple[WorkOperationContract, ...] = (
    WorkOperationContract(
        "work.identity.current",
        _object_schema({}),
        _result_schema(
            ("repo_id", "actor"),
            {"repo_id": {"type": "string"}, "actor": {"type": "string"}},
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
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
        "work.read.items",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}, "track_name": {"type": ["string", "null"]}, "status": {"type": ["string", "null"]}}),
        _result_schema(("repo_id", "items"), {"repo_id": {"type": "string"}, "items": {"type": "array", "items": {"type": "object"}}}),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.context-candidates",
        _object_schema(
            {
                "sprint_id": {"type": ["integer", "null"], "minimum": 1},
                "item_id": {"type": ["integer", "null"], "minimum": 1},
                "target_paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "default": []},
                "query": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "default": 5},
            }
        ),
        _result_schema(
            ("contract_version", "explicit_target", "bound", "truncated", "watermark", "candidates", "sprint", "projection"),
            {
                "contract_version": {"const": "1"},
                "explicit_target": {"type": ["object", "null"]},
                "bound": {"type": "integer", "minimum": 1},
                "truncated": {"type": "boolean"},
                "watermark": {"type": ["object", "null"]},
                "candidates": {"type": "array", "items": {"type": "object"}},
                "sprint": {"type": "object"},
                "projection": {"type": "object"},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.claims",
        _object_schema({"item_id": {"type": ["integer", "null"], "minimum": 1}, "sprint_id": {"type": ["integer", "null"], "minimum": 1}, "active_only": {"type": "boolean", "default": True}, "instance_id": {"type": ["string", "null"]}, "runtime_session_id": {"type": ["string", "null"]}, "hostname": {"type": ["string", "null"]}, "pid": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(("repo_id", "claims"), {"repo_id": {"type": "string"}, "claims": {"type": "array", "items": {"type": "object"}}}),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.claim",
        _object_schema({"claim_id": {"type": "integer", "minimum": 1}}, required=("claim_id",)),
        _result_schema(("repo_id", "claim"), {"repo_id": {"type": "string"}, "claim": {"type": "object"}}),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.context",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            (
                "contract_version", "sprint", "summary", "active_claims",
                "active_unclaimed_items", "conflicts", "ready_items",
                "blocked_items", "stale_items", "recent_decisions", "next_action",
            ),
            {
                "contract_version": {"const": "1"}, "sprint": {"type": "object"},
                "summary": {"type": "object"}, "active_claims": {"type": "array", "items": {"type": "object"}},
                "active_unclaimed_items": {"type": "array", "items": {"type": "object"}},
                "conflicts": {"type": "array", "items": {"type": "object"}},
                "ready_items": {"type": "array", "items": {"type": "object"}},
                "blocked_items": {"type": "array", "items": {"type": "object"}},
                "stale_items": {"type": "array", "items": {"type": "object"}},
                "recent_decisions": {"type": "array", "items": {"type": "object"}},
                "next_action": {"type": "object"},
            },
        ),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.maintain.check",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            (
                "repo_id", "sprint", "risk", "stale_items", "track_health",
                "findings", "threshold_hours", "pending_threshold_hours",
            ),
            {
                "repo_id": {"type": "string"},
                "sprint": {"type": "object"},
                "risk": {"type": "object"},
                "stale_items": {"type": "array", "items": {"type": "object"}},
                "track_health": {"type": "object"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "threshold_hours": {"type": "number", "exclusiveMinimum": 0},
                "pending_threshold_hours": {"type": ["number", "null"]},
            },
        ),
        "work:read", "read", "not-allowed",
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
    WorkOperationContract(
        "work.read.handoff",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}, "events_limit": {"type": "integer", "minimum": 1, "maximum": 500}, "git_context": {"type": ["object", "null"]}}, required=("events_limit",)),
        _result_schema(
            ("bundle_type", "bundle_version", "sprintctl_version", "generated_at", "generated_from", "sprint", "summary", "active_claims", "conflicts", "work", "recent_decisions", "recent_events", "next_action", "delta_since_last_handoff", "freshness", "evidence", "git_context", "claim_identity_model", "resume_instructions", "agent_shutdown_protocol", "items", "events"),
            {"bundle_type": {"const": "handoff"}, "bundle_version": {"const": "1"}, "sprintctl_version": {"type": "string"}, "generated_at": {"type": "string"}, "generated_from": {"type": "object"}, "sprint": {"type": "object"}, "summary": {"type": "object"}, "active_claims": {"type": "array", "items": {"type": "object"}}, "conflicts": {"type": "array", "items": {"type": "object"}}, "work": {"type": "object"}, "recent_decisions": {"type": "array", "items": {"type": "object"}}, "recent_events": {"type": "array", "items": {"type": "object"}}, "next_action": {"type": "object"}, "delta_since_last_handoff": {"type": "object"}, "freshness": {"type": "object"}, "evidence": {"type": "object"}, "git_context": {"type": ["object", "null"]}, "claim_identity_model": {"type": "object"}, "resume_instructions": {"type": "array", "items": {"type": "string"}}, "agent_shutdown_protocol": {"type": "object"}, "items": {"type": "array", "items": {"type": "object"}}, "events": {"type": "array", "items": {"type": "object"}}},
        ),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.handoff.record",
        _object_schema({"sprint_id": {"type": "integer", "minimum": 1}, "bundle": {"type": "object"}}, required=("sprint_id", "bundle")),
        _result_schema(("event_id", "sprint_id", "actor"), {"event_id": {"type": "integer", "minimum": 1}, "sprint_id": {"type": "integer", "minimum": 1}, "actor": {"type": "string"}}),
        "work:write", "write", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.next-work-explain",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("contract_version", "sprint", "summary", "ready_items", "dependency_waiting_items", "active_claims", "active_unclaimed_items", "conflicts", "next_action", "recommended_commands", "recommended_command_bundle"),
            {
                "contract_version": {"const": "1"}, "sprint": {"type": "object"},
                "summary": {"type": "object"}, "ready_items": {"type": "array", "items": {"type": "object"}},
                "dependency_waiting_items": {"type": "array", "items": {"type": "object"}},
                "active_claims": {"type": "array", "items": {"type": "object"}},
                "active_unclaimed_items": {"type": "array", "items": {"type": "object"}},
                "conflicts": {"type": "array", "items": {"type": "object"}}, "next_action": {"type": "object"},
                "recommended_commands": {"type": "array", "items": {"type": "string"}},
                "recommended_command_bundle": {"type": "object"},
            },
        ),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.records",
        _object_schema(
            {
                "after_offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": ["integer", "null"], "minimum": 1},
            }
        ),
        _result_schema(
            ("repo_id", "records", "stream_high_water"),
            {
                "repo_id": {"type": "string"},
                "records": {"type": "array", "items": {"type": "object"}},
                "stream_high_water": {
                    "type": "object",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.decisions",
        _object_schema(
            {
                "after_offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": ["integer", "null"], "minimum": 1},
            }
        ),
        _result_schema(
            ("repo_id", "decisions"),
            {
                "repo_id": {"type": "string"},
                "decisions": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.events",
        _object_schema(
            {
                "sprint_id": {"type": "integer", "minimum": 1},
                "work_item_id": {"type": ["integer", "null"], "minimum": 1},
                "after_offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": ["integer", "null"], "minimum": 1},
            },
            required=("sprint_id",),
        ),
        _result_schema(
            ("repo_id", "events"),
            {
                "repo_id": {"type": "string"},
                "events": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.sprint",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("repo_id", "sprint"),
            {"repo_id": {"type": "string"}, "sprint": {"type": "object"}},
        ),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.sprint-detail",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("repo_id", "sprint"),
            {"repo_id": {"type": "string"}, "sprint": {"type": "object"}},
        ),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.sprint.create",
        _object_schema(
            {
                "name": {"type": "string", "minLength": 1},
                "goal": {"type": "string", "default": ""},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "status": {"enum": ["planned", "active", "closed"], "default": "planned"},
                "kind": {"enum": ["active_sprint", "backlog", "archive"], "default": "active_sprint"},
            },
            required=("name",),
        ),
        _result_schema(
            ("repo_id", "sprint"),
            {"repo_id": {"type": "string"}, "sprint": {"type": "object"}},
        ),
        "work:sprint", "write", "not-allowed",
    ),
    WorkOperationContract(
        "work.event.add",
        _object_schema(
            {
                "sprint_id": {"type": "integer", "minimum": 1},
                "event_type": {"type": "string", "minLength": 1},
                "work_item_id": {"type": ["integer", "null"], "minimum": 1},
                "source_type": {"enum": ["actor", "daemon", "system"]},
                "payload": {"type": ["object", "null"]},
            }, required=("sprint_id", "event_type"),
        ),
        _result_schema(
            ("event_id", "sprint_id", "item_id", "type", "actor", "source"),
            {
                "event_id": {"type": "integer", "minimum": 1},
                "sprint_id": {"type": "integer", "minimum": 1},
                "item_id": {"type": ["integer", "null"]},
                "type": {"type": "string"}, "actor": {"type": "string"},
                "source": {"type": "string"},
            },
        ),
        "work:evidence", "write", "not-allowed",
    ),
    WorkOperationContract(
        "work.item.create",
        _object_schema(
            {
                "sprint_id": {"type": "integer", "minimum": 1},
                "track_name": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "description": {"type": ["string", "null"]},
                "assignee": {"type": ["string", "null"]},
                "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 9},
            }, required=("sprint_id", "track_name", "title"),
        ),
        _result_schema(
            ("item", "track_name"),
            {"item": {"type": "object"}, "track_name": {"type": "string"}},
        ),
        "work:lifecycle", "write", "not-allowed",
    ),
    WorkOperationContract(
        "work.item.edit",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "description": {"type": "string", "minLength": 1},
                "expected_revision": {
                    "type": "string",
                    "pattern": (
                        "^item:[0-9a-fA-F-]{36}@description:v[0-9]+"
                        "@sha256:[0-9a-f]{64}$"
                    ),
                },
            },
            required=("item_id", "description", "expected_revision"),
        ),
        _result_schema(
            (
                "repo_id",
                "item_id",
                "item",
                "event_id",
                "actor",
                "previous_revision",
                "revision",
            ),
            {
                "repo_id": {"type": "string"},
                "item_id": {"type": "integer", "minimum": 1},
                "item": {"type": "object"},
                "event_id": {"type": "integer", "minimum": 1},
                "actor": {"type": "string", "minLength": 1},
                "previous_revision": {"type": "string", "minLength": 1},
                "revision": {"type": "string", "minLength": 1},
            },
        ),
        "work:lifecycle",
        "write",
        "not-allowed",
    ),
    *(
        WorkOperationContract(name, _object_schema(properties, required=required), _result_schema(("repo_id", "item_id", result_id), {"repo_id": {"type": "string"}, "item_id": {"type": "integer", "minimum": 1}, result_id: {"type": "integer", "minimum": 1}}), "work:lifecycle", "write", "not-allowed")
        for name, properties, required, result_id in (
            ("work.item.ref.add", {"item_id": {"type": "integer", "minimum": 1}, "ref_type": {"type": "string", "minLength": 1}, "url": {"type": "string", "minLength": 1}, "label": {"type": "string", "default": ""}}, ("item_id", "ref_type", "url"), "ref_id"),
            ("work.item.ref.remove", {"item_id": {"type": "integer", "minimum": 1}, "ref_id": {"type": "integer", "minimum": 1}}, ("item_id", "ref_id"), "ref_id"),
            ("work.item.dep.add", {"item_id": {"type": "integer", "minimum": 1}, "blocked_item_id": {"type": "integer", "minimum": 1}}, ("item_id", "blocked_item_id"), "dep_id"),
            ("work.item.dep.remove", {"item_id": {"type": "integer", "minimum": 1}, "dep_id": {"type": "integer", "minimum": 1}}, ("item_id", "dep_id"), "dep_id"),
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
        "work.project.context",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("contract_version", "project", "summary", "sprints", "repositories"),
            {
                "contract_version": {"const": "project-1"},
                "project": {"type": "object"},
                "summary": {"type": "object"},
                "sprints": {"type": "array", "items": {"type": "object"}},
                "active_claims": {"type": "array", "items": {"type": "object"}},
                "active_unclaimed_items": {"type": "array", "items": {"type": "object"}},
                "conflicts": {"type": "array", "items": {"type": "object"}},
                "ready_items": {"type": "array", "items": {"type": "object"}},
                "blocked_items": {"type": "array", "items": {"type": "object"}},
                "stale_items": {"type": "array", "items": {"type": "object"}},
                "recent_decisions": {"type": "array", "items": {"type": "object"}},
                "next_actions": {"type": "array", "items": {"type": "object"}},
                "repositories": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:project-read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.project.sprints",
        _object_schema(
            {
                "include_backlog": {"type": "boolean", "default": False},
                "include_archive": {"type": "boolean", "default": False},
                "active_only": {"type": "boolean", "default": False},
            }
        ),
        _result_schema(
            ("contract_version", "project", "sprints", "repositories"),
            {
                "contract_version": {"const": "project-1"},
                "project": {"type": "object"},
                "sprints": {"type": "array", "items": {"type": "object"}},
                "repositories": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:project-read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.project.items",
        _object_schema(
            {
                "sprint_id": {"type": ["integer", "null"], "minimum": 1},
                "track_name": {"type": ["string", "null"]},
                "status": {
                    "type": ["string", "null"],
                    "enum": ["pending", "active", "done", "blocked", None],
                },
            }
        ),
        _result_schema(
            ("contract_version", "project", "items", "repositories"),
            {
                "contract_version": {"const": "project-1"},
                "project": {"type": "object"},
                "items": {"type": "array", "items": {"type": "object"}},
                "repositories": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:project-read",
        "read",
        "not-allowed",
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
        "work.read.maintenance-capability",
        _object_schema({"capability_id": _CAPABILITY_ID_SCHEMA}, required=("capability_id",)),
        _result_schema(
            ("repo_id", "capability"),
            {
                "repo_id": {"type": "string"},
                "capability": {
                    "type": "object",
                    "required": [
                        "capability_id", "envelope_id", "envelope_digest", "plan_ref",
                        "operator_identity", "not_before", "expires_at", "state",
                        "revision", "next_sequence", "created_at", "updated_at",
                    ],
                    "properties": {
                        "capability_id": _CAPABILITY_ID_SCHEMA,
                        "envelope_id": {"type": "string"},
                        "envelope_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "plan_ref": {"type": "string", "pattern": "^artifact:sha256:[0-9a-f]{64}$"},
                        "operator_identity": {"type": "string"},
                        "not_before": {"type": "string"},
                        "expires_at": {"type": "string"},
                        "state": {"enum": ["prepared", "attested", "active", "observing", "reconciled", "aborted", "revoked", "expired"]},
                        "revision": {"type": "integer", "minimum": 1},
                        "next_sequence": {"type": "integer", "minimum": 1},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        ),
        "work:maintenance",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.maintenance.prepare",
        _object_schema(
            {"capability_id": _CAPABILITY_ID_SCHEMA, "envelope": _MAINTENANCE_ENVELOPE_SCHEMA},
            required=("capability_id", "envelope"),
        ),
        _MAINTENANCE_EFFECT_RESULT,
        "work:maintenance",
        "admin",
        "required",
    ),
    WorkOperationContract(
        "work.maintenance.resource.get",
        _object_schema({"resource_ref": {"type": "string", "minLength": 1}}, required=("resource_ref",)),
        _RESOURCE_SNAPSHOT_RESULT,
        "work:maintenance",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.maintenance.resource.changes",
        _object_schema(
            {"resource_ref": {"type": "string", "minLength": 1}, "cursor": {"type": "string", "minLength": 1}, "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 30}},
            required=("resource_ref", "cursor", "wait_seconds"),
        ),
        _RESOURCE_CHANGES_RESULT,
        "work:maintenance",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.maintenance.resource.prepare",
        _object_schema(
            {"capability_id": _CAPABILITY_ID_SCHEMA, "envelope": _MAINTENANCE_ENVELOPE_SCHEMA},
            required=("capability_id", "envelope"),
        ),
        _RESOURCE_REFERENCE_RESULT,
        "work:maintenance",
        "admin",
        "required",
        result_contract_resource_kind="work.maintenance-capability",
    ),
    WorkOperationContract(
        "work.maintenance.transition",
        _object_schema(
            {
                "capability_id": _CAPABILITY_ID_SCHEMA,
                "action": {"enum": ["attest", "activate", "observe", "reconcile", "abort", "revoke"]},
                "expected_revision": {"type": "string", "minLength": 1},
                "step_id": {"type": ["string", "null"]},
                "command_id": {"type": ["string", "null"]},
                "command_ref": {"anyOf": [_SHA256_REF_SCHEMA, {"type": "null"}]},
                "effect_ref": {"anyOf": [_SHA256_REF_SCHEMA, {"type": "null"}]},
                "reconciliation": {"type": ["object", "null"]},
            },
            required=("capability_id", "action", "expected_revision"),
        ),
        _MAINTENANCE_EFFECT_RESULT,
        "work:maintenance",
        "admin",
        "required",
    ),
    WorkOperationContract(
        "work.maintenance.recovery-record",
        _object_schema(
            {
                "capability_id": _CAPABILITY_ID_SCHEMA,
                "kind": {"enum": ["observation", "requested-command"]},
                "payload_ref": _SHA256_REF_SCHEMA,
            },
            required=("capability_id", "kind", "payload_ref"),
        ),
        _result_schema(
            ("repo_id", "capability_id", "record_id", "kind", "authority"),
            {
                "repo_id": {"type": "string"},
                "capability_id": _CAPABILITY_ID_SCHEMA,
                "record_id": {"type": "string", "format": "uuid"},
                "kind": {"enum": ["observation", "requested-command"]},
                "authority": {"const": "none"},
            },
        ),
        "work:maintenance-audit",
        "write",
        "required",
    ),
)


LEGACY_REMOTE_COMMAND_PARITY: tuple[dict[str, str], ...] = (
    {"legacy": "sprintctl context-candidates --json", "operation": "work.read.context-candidates"},
    {"legacy": "sprintctl sprint list --json", "operation": "work.read.sprints"},
    {"legacy": "sprintctl item show --id ID --json", "operation": "work.read.item"},
    {"legacy": "sprintctl next-work --json", "operation": "work.read.next-work"},
    {
        "legacy": "sprintctl next-work --json --explain",
        "operation": "work.read.next-work-explain",
    },
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
    {"legacy": "sprintctl item done-from-claim", "operation": "work.lifecycle.arbitrate"},
    {"legacy": "sprintctl authority sync", "operation": "work.batch.apply"},
    {"legacy": "sprintctl event observation add", "operation": "work.evidence.ingest"},
    {"legacy": "sprintctl item note", "operation": "work.item.note"},
    {"legacy": "sprintctl item edit", "operation": "work.item.edit"},
    {"legacy": "sprintctl next-work --project", "operation": "work.project.next-work"},
    {"legacy": "project dispatch batching", "operation": "work.project.batch"},
)


_RESOURCE_OPERATIONS = frozenset(
    {
        "work.maintenance.resource.prepare",
        "work.maintenance.resource.get",
        "work.maintenance.resource.changes",
    }
)


def catalog_operation_specs(
    *, resource_schema_available: bool
) -> tuple[dict[str, Any], ...]:
    """Return fresh data-only Vuoro definitions in owner-declared order."""

    return tuple(
        operation_spec(
            contract.name,
            owning_domain="work",
            input_schema=contract.input_schema,
            result_schema=contract.result_schema,
            required_authority=contract.required_authority,
            execution_semantics=contract.execution_semantics,
            idempotency=contract.idempotency,
            repo_scoped=not contract.name.startswith("work.project."),
            required_client_schema_features=contract.required_client_schema_features,
            result_contract=(
                {
                    "mode": "resource-reference",
                    "resource_kind": contract.result_contract_resource_kind,
                }
                if contract.result_contract_resource_kind
                else None
            ),
        )
        for contract in WORK_OPERATION_CONTRACTS
        if resource_schema_available or contract.name not in _RESOURCE_OPERATIONS
    )


def validate_served_operation_registry() -> None:
    """Prove every client-exposed operation has a domain catalog contract.

    The generic handler registered below dispatches by operation name, so a
    missing contract would otherwise surface only after a served client had
    selected a route.  Validate the binding while the catalog is composed.
    """
    from .served_routes import OPERATION_SPECS

    contracts = {contract.name for contract in WORK_OPERATION_CONTRACTS}
    missing = sorted({spec.operation for spec in OPERATION_SPECS} - contracts)
    if missing:
        raise RuntimeError(
            "served operation registry references unpublished work contracts: "
            + ", ".join(missing)
        )


def register_work_catalog(
    registry: Any,
    application: WorkApplication,
    *,
    project_application: ProjectWorkApplication | None = None,
) -> None:
    """Register the complete work operation catalog in a Vuoro registry."""

    validate_served_operation_registry()

    from vuoro_service.catalog import OperationRejectedError
    from vuoro_service.contracts import (
        BoundedLongPollCapability,
        OperationDefinition,
        ResourceKindDefinition,
        ResourceObservationContract,
    )

    resource_schema_available = application.maintenance_resource_schema_available()
    resource_kind_registered = False
    contracts = {contract.name: contract for contract in WORK_OPERATION_CONTRACTS}
    for raw_spec in catalog_operation_specs(
        resource_schema_available=resource_schema_available
    ):
        operation = raw_spec["name"]
        contract = contracts[operation]
        result_contract_resource_kind = contract.result_contract_resource_kind
        if result_contract_resource_kind and not resource_kind_registered:
            registry.register_resource_kind(
                ResourceKindDefinition(
                    resource_kind="work.maintenance-capability",
                    observation=ResourceObservationContract(
                        snapshot_operation="work.maintenance.resource.get",
                        changes_operation="work.maintenance.resource.changes",
                        cursor_schema="sprintctl-maintenance-cursor/v1",
                        supports_terminality=True,
                    ),
                )
            )
            registry.register_observation_transport(
                BoundedLongPollCapability(maximum_wait_seconds=30)
            )
            resource_kind_registered = True
        # work.project.* operations aggregate across a project's member
        # repos using an origin_repo field inside their own arguments (see
        # ProjectWorkApplication) -- they have no single repo_id to scope
        # to, so they stay outside the envelope-level repo_id/authorization
        # gate that every other work.* operation requires.
        definition = OperationDefinition(**raw_spec)

        def handler(
            arguments: Any, context: Any, *, operation: str = operation
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

        registry.register(
            definition,
            handler,
            result_decoder=(
                (
                    lambda result: application._scoped_for(
                        result["repo_id"]
                    ).maintenance_resource_reference(result)
                )
                if result_contract_resource_kind else None
            ),
        )


__all__ = [
    "LEGACY_REMOTE_COMMAND_PARITY",
    "SCHEMA_DIALECT",
    "SCHEMA_FEATURES",
    "WORK_API_VERSION",
    "WORK_OPERATION_CONTRACTS",
    "WORK_SCHEMA_VERSION",
    "WorkOperationContract",
    "catalog_operation_specs",
    "register_work_catalog",
    "validate_served_operation_registry",
]
