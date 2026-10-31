"""Vuoro protocol-v1 catalog adapter for the sprintctl work domain.

The module has no import-time dependency on ``vuoro-service``.  Service
composition calls :func:`register_work_catalog` after installing pinned domain
and service releases; standalone sprintctl and its legacy CLI remain usable
without the service distribution.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Literal

from vuoro_adapter_kit import (
    SCHEMA_DIALECT as _ADAPTER_SCHEMA_DIALECT,
)
from vuoro_adapter_kit import (
    SCHEMA_FEATURES as _ADAPTER_SCHEMA_FEATURES,
)
from vuoro_adapter_kit import (
    object_schema,
    operation_spec,
)

from . import agent_inbox as _inbox
from . import approval as _approval
from . import decisions as _decisions
from . import effect_attempt as _attempt
from . import effect_intent as _effect
from . import effect_intent_export as _effect_export
from . import effect_verification_export as _verification_export
from . import effect_basis as _effect_basis
from . import delegation as _delegation
from . import releases as _releases
from . import unbound as _unbound
from .application import (
    SUPPORTED_BATCH_TYPES,
    ApplicationRejection,
    ProjectWorkApplication,
    WorkApplication,
)
from .application_common import (
    PREDECESSOR_CONTEXT_DEFAULT_LIMIT,
    PREDECESSOR_CONTEXT_MAX_LIMIT,
    SESSION_NOTE_MAX_LENGTH,
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
    #: Minimum owner schema that can implement this operation. Older owner
    #: catalogs must not advertise it as a callable promise.
    required_remote_schema_version: int = 1
    result_contract_resource_kind: str | None = None
    #: The catalog's deprecation record, ``{deprecated, replacement,
    #: sunset_at}``; None publishes the operation as current.
    deprecation: dict[str, Any] | None = None


_object_schema = object_schema

_UNBOUND_CATEGORIES = _unbound.CATEGORIES
_RESOLUTION_METRICS = (
    *_unbound.RESOLUTION_METRIC_KEYS,
    "decided_done",
    "legacy_done",
    "legacy_remarked",
    "done",
)
_UNBOUND_CATEGORY_RESULT: dict[str, Any] = {
    "type": "object",
    "required": ["count", "items"],
    "properties": {
        "count": {"type": "integer", "minimum": 0},
        "items": {"type": "array", "items": {"type": "object"}},
    },
    "additionalProperties": False,
}
_DECISION_KINDS = _decisions.DECISION_KINDS
_RESOLUTIONS = _decisions.RESOLUTIONS
_SHA256_HEX: dict[str, Any] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
# #2451: the unmet_obligations report (#2446) is report-only, alongside the
# four categories above -- it names no gate, no validator, no transition.
_UNMET_OBLIGATION_ITEM: dict[str, Any] = {
    "type": "object",
    "required": ["item_id", "decision_id", "release_digest", "unmet"],
    "properties": {
        "item_id": {"type": "integer", "minimum": 1},
        "decision_id": {"type": "integer", "minimum": 1},
        "release_digest": _SHA256_HEX,
        "unmet": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
    "additionalProperties": False,
}
_UNMET_OBLIGATIONS_RESULT: dict[str, Any] = {
    "type": "object",
    "required": ["count", "items"],
    "properties": {
        "count": {"type": "integer", "minimum": 0},
        "items": {"type": "array", "items": _UNMET_OBLIGATION_ITEM},
    },
    "additionalProperties": False,
}


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

# Every served event carries its storage ``created_at``: NOT NULL with a
# default on both backends, emitted as a UTC ISO-8601 string ending in ``Z``
# (SQLite ``strftime('%Y-%m-%dT%H:%M:%SZ')``; PostgreSQL ``timestamptz`` via
# ``rows.iso_timestamp``, which keeps fractional seconds). Other event fields
# stay open, so this is deliberately not ``_object_schema`` (which closes
# ``additionalProperties``).
_EVENT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"created_at": {"type": "string", "minLength": 1}},
    "required": ["created_at"],
    "additionalProperties": True,
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
# ``work.batch.apply`` advertises the record types it accepts, so a newer
# client can see in the (unauthenticated) catalog whether this server knows
# a record type before minting one into its contiguous outbox stream
# (``sprintctl.served.batch_record_types``).  The server's own
# ``record-type-not-allowed`` check (``SUPPORTED_BATCH_TYPES``) is unchanged.
_BATCH_APPLY_INPUT = _object_schema(
    {
        "records": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/record"},
        }
    },
    required=("records",),
    definitions={
        "record": {
            **_RECORD_DEFINITION,
            "properties": {
                **_RECORD_DEFINITION["properties"],
                "event_type": {
                    "type": "string",
                    "minLength": 1,
                    "enum": sorted(SUPPORTED_BATCH_TYPES),
                },
            },
        }
    },
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



# --------------------------------------------------------------------------
# work.public.* -- the workspace-scoped public-work contract (agentops#2514,
# design e1-stronger-baseline-design-2026-09-22 section 5).  These are the
# ``work.public.list/v1`` and ``work.public.item/v1`` contracts of that design;
# the adapter kit forbids ``/`` in an operation name, so the version rides as
# a ``-v1`` suffix and a future v2 (objective, acceptance as authored columns)
# registers beside it rather than replacing it.
#
# Every object is ``additionalProperties: false``.  The strict schema is the
# emission boundary: a handler that leaks a field the contract does not name
# fails the served result gate (adapter-result-invalid), and the tests force
# that failure for ``description`` and for every never-emit field by name.
# --------------------------------------------------------------------------
from .contracts import (
    PUBLIC_WORK_NEVER_EMIT_FIELDS,
    PUBLIC_WORK_STATUSES,
    PUBLIC_WORK_TITLE_MAX_LENGTH,
)

_PUBLIC_WORK_ID: dict[str, Any] = {"type": "integer", "minimum": 1}
_PUBLIC_WORK_LIST_ITEM_PROPERTIES: dict[str, Any] = {
    "work_id": _PUBLIC_WORK_ID,
    "title": {"type": "string", "minLength": 1, "maxLength": PUBLIC_WORK_TITLE_MAX_LENGTH},
    "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 9},
    "status": {"enum": list(PUBLIC_WORK_STATUSES)},
    "blocked": {"type": "boolean"},
    "updated_at": {"type": "string"},
}
_PUBLIC_WORK_LIST_ITEM_REQUIRED = (
    "work_id", "title", "priority", "status", "blocked", "updated_at",
)
_PUBLIC_WORK_ITEM_PROPERTIES: dict[str, Any] = {
    **_PUBLIC_WORK_LIST_ITEM_PROPERTIES,
    "created_at": {"type": "string"},
    "resolution": {"type": ["string", "null"]},
    "blocked_by": {"type": "array", "items": _PUBLIC_WORK_ID},
}
_PUBLIC_WORK_ITEM_REQUIRED = (
    *_PUBLIC_WORK_LIST_ITEM_REQUIRED, "created_at", "resolution", "blocked_by",
)
_PUBLIC_WORK_LIST_ITEM_SCHEMA = _object_schema(
    _PUBLIC_WORK_LIST_ITEM_PROPERTIES, required=_PUBLIC_WORK_LIST_ITEM_REQUIRED
)
_PUBLIC_WORK_ITEM_SCHEMA = _object_schema(
    _PUBLIC_WORK_ITEM_PROPERTIES, required=_PUBLIC_WORK_ITEM_REQUIRED
)
# The envelope names its authority and its state.  ``unavailable`` is in the
# enum so the contract states the vocabulary a consumer must handle, but the
# sprintctl handler never emits it with a payload: an unavailable authority is
# a rejection (postgres-runtime-unavailable, 503), never an empty list.
_PUBLIC_WORK_ENVELOPE: dict[str, Any] = {
    "authority": {"const": "sprintctl"},
    "as_of": {"type": "string"},
    "state": {"enum": ["ok", "unavailable"]},
}
_PUBLIC_WORK_LIST_RESULT = _result_schema(
    ("authority", "as_of", "state", "items"),
    {
        **_PUBLIC_WORK_ENVELOPE,
        "items": {"type": "array", "items": _PUBLIC_WORK_LIST_ITEM_SCHEMA},
    },
)
_PUBLIC_WORK_ITEM_RESULT = _result_schema(
    ("authority", "as_of", "state", "item"),
    {**_PUBLIC_WORK_ENVELOPE, "item": _PUBLIC_WORK_ITEM_SCHEMA},
)


# --------------------------------------------------------------------------
# work.run.* / work.evidence.* / work.session-note.* / work.idempotency.*
# (agentops#2466, E2: the vuoro-mcp-edge record bucket, vuoro:evidence.record
# -> work:evidence).  See docs/reference/work-public-contract.md's sibling
# note in this repo's own docs and the E2/E3 shared contract
# (vuoro docs/plans/2026-09-26-e2-e3-shared-contract.md sections 4-5) for the
# run-handle and idempotency-ledger semantics these operations back.
#
# The run record is the addressable composition object the shared contract
# names (vuoro_evidence.run.RunManifest): harness, model, recipe and the
# profile observed at session start, plus the grant/claim ids active when it
# was minted.  sprintctl stores exactly those fields; it does not construct a
# RunManifest object itself (that type lives in the vuoro_evidence package,
# which this repo does not depend on), it only holds the same named fields.
#
# The evidence chain's hash math (entry_digest/link) is computed by the
# caller (the Vuoro MCP edge, from vuoro_evidence.core.chain) from the tail
# it observes via work.evidence.tail-v1; work.evidence.append-v1 stores the
# already-linked item and, under a per-run lock, guards that chain_seq and
# chain_prev_digest extend the tail it actually holds (the prev digest is
# checked against entry_digest of the stored tail, not recomputed as a
# second chain), so a stale or racing append is refused
# (evidence-chain-conflict) rather than silently reordered or forked.
#
# Each write operation below is its own idempotent unit: it claims the
# write-tool idempotency ledger row the shared contract requires
# (docs/plans/2026-09-26-e2-e3-shared-contract.md section 5), performs its
# effect and records the result in ONE transaction, internally, rather than
# exposing the ledger as separate wire operations.  The row the effect
# writes (run, evidence item, session note) also stores the request digest,
# so a same-key retry with different arguments is refused by storage as
# well, not only by the ledger.
#
# A run binds the caller's principal, workspace and repository plus, when
# the identity asserts them, its OAuth client_id and grant_id; resolve
# refuses any other binding with run-not-found and echoes all of them. The ledger is keyed by (repo_id, workspace_id, principal_id,
# tool, idempotency_key) -- one dimension more specific than the shared
# contract's own words ("(workspace, tool, key)"), because that literal
# scoping lets a second principal in the same workspace replay a first
# principal's stored result by reusing its idempotency_key with identical
# arguments; see pg.py's work_idempotency_ledger docstring and the E2 final
# report -- and stores the first request's digest and result, so a retry
# with the same key and arguments replays it in one round trip with no
# second effect, and a retry with the same key and different arguments is
# refused (idempotency-conflict) -- the same behaviour
# vuoro_mcp_edge.idempotency.InMemoryIdempotencyLedger gives as a reference,
# reached here by a different (but behaviourally equivalent) path: sprintctl
# has no dependency on that package, so it keeps its own digest function
# (work_application._request_digest) rather than importing one. The
# workspace scope is always the caller's own identity, never a client-
# supplied argument.
# --------------------------------------------------------------------------
_RUN_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^run_[0-9A-HJKMNP-TV-Z]{26}$",
}
_IDEMPOTENCY_KEY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[A-Za-z0-9._:-]{8,128}$",
}
#: A run's OAuth client/grant binding: absent (null) only for a run minted
#: by an identity that asserts no grant.
_NULLABLE_BINDING_ID_SCHEMA: dict[str, Any] = {
    "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]
}
_SKILL_DIGEST_SCHEMA = _object_schema(
    {
        "skill_id": {"type": "string", "minLength": 1},
        "digest": {"type": "string", "minLength": 1},
    },
    required=("skill_id", "digest"),
)
_OBSERVED_PROFILE_SCHEMA = _object_schema(
    {
        "instruction_digest": {"type": "string", "minLength": 1},
        "skill_digests": {"type": "array", "items": _SKILL_DIGEST_SCHEMA},
    },
    required=("instruction_digest", "skill_digests"),
)
_RUN_RESULT_OBJECT = _object_schema(
    {
        "run_id": _RUN_ID_SCHEMA,
        "principal_id": {"type": "string", "minLength": 1},
        "workspace_id": {"type": "string", "minLength": 1},
        "client_id": _NULLABLE_BINDING_ID_SCHEMA,
        "grant_id": _NULLABLE_BINDING_ID_SCHEMA,
        "harness_id": {"type": "string", "minLength": 1},
        "harness_build": {"type": "string", "minLength": 1},
        "model_id": {"type": "string", "minLength": 1},
        "recipe_id": {"type": "string", "minLength": 1},
        "observed_profile": _OBSERVED_PROFILE_SCHEMA,
        "grant_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "claim_ids": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "created_at": {"type": "string"},
    },
    required=(
        "run_id", "principal_id", "workspace_id", "client_id", "grant_id",
        "harness_id", "harness_build",
        "model_id", "recipe_id", "observed_profile", "grant_ids", "claim_ids",
        "created_at",
    ),
)
_REGISTERED_RUN_RESULT_OBJECT = _object_schema(
    {
        **_RUN_RESULT_OBJECT["properties"],
        # agentops#2525: the run this one continues, null for none.  Not
        # required: a same-key replay returns the result the ledger stored,
        # and a result stored before this field existed has no such key.
        "predecessor_run_id": {"anyOf": [_RUN_ID_SCHEMA, {"type": "null"}]},
    },
    required=tuple(_RUN_RESULT_OBJECT["required"]),
)
_RUN_INVOCATION_RECEIPT_SCHEMA = _object_schema(
    {
        "schema": {"const": "sprintctl-run-invocation-receipt/v1"},
        "repo_id": {"type": "string", "minLength": 1},
        "invocation_id": {"type": "string", "pattern": "^inv_[0-9A-HJKMNP-TV-Z]{26}$"},
        "attempt_id": {"type": "string", "pattern": "^att_[0-9A-HJKMNP-TV-Z]{26}$"},
        "owner_record_kind": {"const": "run"},
        "owner_record_id": _RUN_ID_SCHEMA,
        "owner_record_digest": _SHA256_HEX,
        "owner_binding": _object_schema(
            {
                "repo_id": {"type": "string", "minLength": 1},
                "workspace_id": {"type": "string", "minLength": 1},
                "principal_id": {"type": "string", "minLength": 1},
                "principal_epoch": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
                "client_id": _NULLABLE_BINDING_ID_SCHEMA,
                "grant_id": _NULLABLE_BINDING_ID_SCHEMA,
            },
            required=("repo_id", "workspace_id", "principal_id", "principal_epoch", "client_id", "grant_id"),
        ),
    },
    required=("schema", "repo_id", "invocation_id", "attempt_id", "owner_record_kind", "owner_record_id", "owner_record_digest", "owner_binding"),
)
_CORRELATED_RUN_RESULT_OBJECT = _object_schema(
    {
        "repo_id": {"type": "string", "minLength": 1},
        "run": _REGISTERED_RUN_RESULT_OBJECT,
        "correlation_status": {"enum": ["bound", "legacy-unbound"]},
        "receipt": {"anyOf": [_RUN_INVOCATION_RECEIPT_SCHEMA, {"type": "null"}]},
    },
    required=("repo_id", "run", "correlation_status", "receipt"),
)
_CORRELATED_RUN_RESULT_OBJECT["oneOf"] = [
    {
        "properties": {
            "correlation_status": {"const": "bound"},
            "receipt": _RUN_INVOCATION_RECEIPT_SCHEMA,
        },
        "required": ["correlation_status", "receipt"],
    },
    {
        "properties": {
            "correlation_status": {"const": "legacy-unbound"},
            "receipt": {"type": "null"},
        },
        "required": ["correlation_status", "receipt"],
    },
]
_SESSION_NOTE_RESULT_OBJECT = _object_schema(
    {
        "note_id": {"type": "integer", "minimum": 1},
        "note": {"type": "string"},
        "created_at": {"type": "string"},
    },
    required=("note_id", "note", "created_at"),
)
_VALIDITY_WINDOW_SCHEMA = _object_schema(
    {
        "basis": {"enum": ["indefinite", "bounded", "until_inputs_change"]},
        "valid_from": {"type": "string", "minLength": 1},
        "valid_until": {"type": ["string", "null"]},
        "component_digests": {"type": "object"},
    },
    required=("basis", "valid_from", "valid_until", "component_digests"),
)
_CLAIM_SCHEMA = _object_schema(
    {
        "claim_type": {
            "enum": [
                "target_stale", "precondition_refused", "capability_unavailable",
                "evidence_persisted", "effect_completed", "effect_failed",
                "effect_uncertain", "effect_not_invoked", "observation",
            ]
        },
        "subject": {"type": "string", "minLength": 1},
        "grant_id": {"type": ["string", "null"]},
        "freshness": {
            "anyOf": [
                _object_schema(
                    {"scope": {"type": "string", "minLength": 1}, "position": {"type": "integer"}},
                    required=("scope", "position"),
                ),
                {"type": "null"},
            ]
        },
        "confirms": {"type": ["boolean", "null"]},
        "detail": {"type": "object"},
    },
    required=("claim_type", "subject", "grant_id", "freshness", "confirms", "detail"),
)
_EVIDENCE_ITEM_SCHEMA = _object_schema(
    {
        "item_id": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "minLength": 1},
        "ref": {"type": "string", "minLength": 1},
        "digest": {"type": "string", "minLength": 1},
        "collector": {"type": "string", "minLength": 1},
        "validity": _VALIDITY_WINDOW_SCHEMA,
        "claims": {"type": "array", "items": _CLAIM_SCHEMA},
        "provenance": {"type": "object"},
        "chain_seq": {"type": "integer", "minimum": 0},
        "chain_prev_digest": {"type": ["string", "null"]},
    },
    required=(
        "item_id", "kind", "ref", "digest", "collector", "validity", "claims",
        "provenance", "chain_seq", "chain_prev_digest",
    ),
)
# --------------------------------------------------------------------------
# work.lease.* (agentops#2520, E2b: the Vuoro MCP edge's coordinate bucket,
# vuoro:work.claim -> work:claim).  An exclusive durable lease on one work
# item, held by the caller's run and its whole binding.  This authority
# evaluates it at every call -- claim, heartbeat, complete -- against its own
# clock (TS-1: nothing sweeps, schedules, expires or retries in the
# background), and settles outcome reports itself: complete-v1 is an outcome
# report, and the owner decides whether it settles the item under the
# verification profile configured on the item's current release.
#
# acquire-v1 and complete-v1 are write tools with the section 5 ledger
# (workspace, principal, tool, key).  A replayed acquire re-evaluates the
# claim instead of repeating the stored answer: that is how a restarted
# worker resumes its own lease.  A refused completion is committed before it
# is refused, so the late payload stays on the item as evidence.
# heartbeat-v1 takes no key: refreshing a lease is idempotent by nature.
# --------------------------------------------------------------------------
_LEASE_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^lease_[0-9A-HJKMNP-TV-Z]{26}$",
}
_NULLABLE_LEASE_ID_SCHEMA: dict[str, Any] = {"anyOf": [_LEASE_ID_SCHEMA, {"type": "null"}]}
_NULLABLE_TEXT_SCHEMA: dict[str, Any] = {"type": ["string", "null"]}
_LEASE_STATE_SCHEMA: dict[str, Any] = {"enum": ["active", "superseded", "settled", "released"]}
# A profile is a set of requirements, and bars combine by their union
# (agentops#2539): a combined bar that no single profile names is the named
# profiles joined by "+", e.g. "role-separated+human-authorized".
_PROFILE_NAMES = "self-reported|checked|role-separated|identity-separated|human-authorized"
_VERIFICATION_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": f"^({_PROFILE_NAMES})(\\+({_PROFILE_NAMES}))*$",
}
_VERIFICATION_SCHEMA = _object_schema(
    {
        "profile": _VERIFICATION_PROFILE_SCHEMA,
        "requirements": {
            "type": "array",
            "items": {"enum": ["checks", "verifier-role", "verifier-identity", "human-authorization"]},
        },
        "required_checks": {"type": "array", "items": {"type": "string"}},
    },
    required=("profile", "requirements", "required_checks"),
)
_LEASE_PROPERTIES: dict[str, Any] = {
    "lease_id": _LEASE_ID_SCHEMA,
    # The claim generation on the item (agentops#2540): derived, 1 for the
    # item's first lease, the same across the holder's own reactivation.
    "generation": {"type": "integer", "minimum": 1},
    "item_id": {"type": "integer", "minimum": 1},
    "run_id": _RUN_ID_SCHEMA,
    "principal_id": {"type": "string", "minLength": 1},
    "workspace_id": {"type": "string", "minLength": 1},
    "state": _LEASE_STATE_SCHEMA,
    "ttl_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
    "heartbeat_interval_seconds": {"type": "integer", "minimum": 1},
    "acquired_at": {"type": "string"},
    "heartbeat_at": {"type": "string"},
    "expires_at": {"type": "string"},
    "ended_at": _NULLABLE_TEXT_SCHEMA,
    "end_reason": _NULLABLE_TEXT_SCHEMA,
    "takeover_of": _NULLABLE_LEASE_ID_SCHEMA,
    "superseded_by": _NULLABLE_LEASE_ID_SCHEMA,
    "basis_item_revision": {
        "type": ["string", "null"],
        "pattern": _releases._RELEASE_REVISION_RE.pattern,
    },
    "basis_release_digest": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
    "verification": _VERIFICATION_SCHEMA,
}
_LEASE_REQUIRED = tuple(_LEASE_PROPERTIES)
_LEASE_SCHEMA = _object_schema(_LEASE_PROPERTIES, required=_LEASE_REQUIRED)
_CURRENT_LEASE_SCHEMA = _object_schema(
    {**_LEASE_PROPERTIES, "stale": {"type": "boolean"}},
    required=(*_LEASE_REQUIRED, "stale"),
)
_OUTCOME_CHECK_SCHEMA = _object_schema(
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "status": {"enum": ["passed", "failed"]},
        "ref": {"type": "string", "maxLength": 512},
    },
    required=("name", "status"),
)
_PARKED_REPORT_SCHEMA = _object_schema(
    {"event_id": {"type": "integer", "minimum": 1}, "reason_ref": {"type": "string"}},
    required=("event_id", "reason_ref"),
)
# The work-level parking a failed report recorded and no release has lifted
# yet (agentops#2543): a property of the item, not of any lease.
_PARKING_SCHEMA = _object_schema(
    {
        "event_id": {"type": "integer", "minimum": 1},
        "reason_ref": {"type": ["string", "null"]},
        "report_id": {"type": ["string", "null"]},
        "lease_id": {"type": ["string", "null"]},
        "parked_at": {"type": "string"},
    },
    required=("event_id", "reason_ref", "report_id", "lease_id", "parked_at"),
)
_OUTCOME_REPORT_SCHEMA = _object_schema(
    {
        "report_id": {"type": "string", "pattern": "^outcome_[0-9A-HJKMNP-TV-Z]{26}$"},
        "item_id": {"type": "integer", "minimum": 1},
        "lease_id": _LEASE_ID_SCHEMA,
        "run_id": _RUN_ID_SCHEMA,
        "principal_id": {"type": "string", "minLength": 1},
        "outcome": {"enum": ["succeeded", "failed"]},
        "summary": {"type": "string"},
        "payload": {"type": "object"},
        "checks": {"type": "array", "items": _OUTCOME_CHECK_SCHEMA},
        "payload_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "disposition": {"enum": ["settled", "recorded", "awaiting-verification", "rejected"]},
        "reason_code": _NULLABLE_TEXT_SCHEMA,
        "verification_profile": _VERIFICATION_PROFILE_SCHEMA,
        "decision_id": {"type": ["integer", "null"]},
        "created_at": {"type": "string"},
        "detail": {"type": "string"},
        "details": {"type": "object"},
        # Present only on a report that parked its item (agentops#2543).
        "parked": _PARKED_REPORT_SCHEMA,
    },
    required=(
        "report_id", "item_id", "lease_id", "run_id", "principal_id", "outcome",
        "summary", "payload", "checks", "payload_digest", "disposition", "reason_code",
        "verification_profile", "decision_id", "created_at",
    ),
)
_CLAIM_RESULT = _result_schema(
    ("repo_id", "lease", "resumed", "took_over"),
    {
        "repo_id": {"type": "string"},
        "lease": _LEASE_SCHEMA,
        "resumed": {"type": "boolean"},
        "took_over": _NULLABLE_LEASE_ID_SCHEMA,
    },
)
# work.lease.report-outcome-v1 (agentops#2540): the worker reports an
# outcome and the authority settles (agentops#253 decision 4); the answer
# says what the report did (settlement_effect).  complete-v1 is its
# deprecated alias: the same input, result, handler and ledger tool.
_REPORT_OUTCOME_INPUT = _object_schema(
    {
        "lease_id": _LEASE_ID_SCHEMA,
        "run_id": _RUN_ID_SCHEMA,
        "outcome": {"enum": ["succeeded", "failed"]},
        "summary": {"type": "string", "maxLength": 4000},
        "payload": {"type": "object"},
        "checks": {"type": "array", "items": _OUTCOME_CHECK_SCHEMA, "maxItems": 64},
        # agentops#2543: a failed outcome may park the work itself.  The
        # holder names why with reason_ref (an issue, run or report it can
        # cite); the two go together and only with outcome "failed".
        "disposition": {"enum": ["parked"]},
        "reason_ref": {"type": "string", "minLength": 1, "maxLength": 512},
        "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
    },
    required=("lease_id", "run_id", "outcome", "idempotency_key"),
)
_REPORT_OUTCOME_RESULT = _result_schema(
    ("repo_id", "report", "settled", "settlement_effect"),
    {
        "repo_id": {"type": "string"},
        "report": _OUTCOME_REPORT_SCHEMA,
        "settled": {"type": "boolean"},
        "settlement_effect": {
            "enum": ["settled", "lease-released", "parked", "awaiting-verification", "none"],
        },
    },
)
# --------------------------------------------------------------------------
# work.effect.* (agentops#2541, M2-1; operator Decision 1 (A) and INV-E1 on
# agentops#253).  sprintctl owns the effect-intent lifecycle beside the work,
# runs and leases it describes: a hosted worker proposes a change as a unified
# diff, a trusted-side principal accepts or rejects exactly that proposal, and
# a reconciler marks it applied.  Nothing here applies a diff.
#
# Each mutating operation requires its own capability (work.effect.propose,
# .accept, .reject, .mark-applied), never work:claim / work:write, so a
# principal that may change work state does not inherit acceptance authority;
# the reads have theirs too (work.effect.get, .list-proposed).  An intent
# carries a ``revision`` and a ``canonical_intent_digest`` the authority
# computes from its content; accept, reject and mark-applied name both and are
# refused unless they still match.  The proposer and the acceptor are the
# authenticated principal, never a wire argument.  An accepted intent is
# immutable; a change is a new proposal.  propose-v1 is a write tool with the
# section 5 ledger (workspace, principal, tool, key); the transitions are
# compare-and-set on (revision, digest) and need no key.
# --------------------------------------------------------------------------
_EFFECT_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^intent_[0-9A-HJKMNP-TV-Z]{26}$",
}
# What a caller may name: any id a caller could have been handed, so that an
# unknown one is ``effect-not-found`` rather than a schema error.
_EFFECT_ID_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[A-Za-z0-9._:-]{1,128}$",
}
_EFFECT_DIGEST_SCHEMA: dict[str, Any] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_EFFECT_REVISION_SCHEMA: dict[str, Any] = {"type": "integer", "minimum": 1}
_EFFECT_COMMIT_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$",
}
_EFFECT_PR_URL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^https?://\\S+$",
    "maxLength": _effect.MAX_PR_URL,
}
_EFFECT_VERIFICATION_REF_SCHEMA = _object_schema(
    {"run_id": _RUN_ID_SCHEMA, "item_id": {"type": "string", "minLength": 1}},
    required=("run_id", "item_id"),
)
_EFFECT_VERIFICATION_BINDING_SCHEMA = _object_schema(
    {"run_id": _RUN_ID_SCHEMA, "item_id": {"type": "string"},
     "evidence_digest": {"type": "string"}, "entry_digest": {"type": "string"},
     "verifier_principal": {"type": "string"}, "workspace_id": {"type": "string"},
     "client_id": {"type": ["string", "null"]}, "grant_id": {"type": ["string", "null"]},
     "receipt": {"type": "object"}},
    required=("run_id", "item_id", "evidence_digest", "entry_digest", "verifier_principal",
              "workspace_id", "client_id", "grant_id", "receipt"),
)
_EFFECT_ACCEPTANCE_SCHEMA = _object_schema(
    {
        "intent_id": _EFFECT_ID_SCHEMA,
        "intent_revision": _EFFECT_REVISION_SCHEMA,
        "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA,
        "acceptor_principal": {"type": "string", "minLength": 1},
        # null until a policy acceptor exists; an operator acceptance has none.
        "acceptor_policy_version": {"type": ["string", "null"]},
        "accepted_at": {"type": "string"},
        "verification": _EFFECT_VERIFICATION_BINDING_SCHEMA,
    },
    required=(
        "intent_id", "intent_revision", "canonical_intent_digest", "acceptor_principal",
        "acceptor_policy_version", "accepted_at",
    ),
)
_EFFECT_REJECTION_SCHEMA = _object_schema(
    {
        "rejector_principal": {"type": "string", "minLength": 1},
        "reason": {"type": "string"},
        "rejected_at": {"type": "string"},
    },
    required=("rejector_principal", "reason", "rejected_at"),
)
_EFFECT_APPLICATION_SCHEMA = _object_schema(
    {
        "applier_principal": {"type": "string", "minLength": 1},
        "commit_sha": _EFFECT_COMMIT_SCHEMA,
        "pr_url": {"type": "string"},
        "applied_at": {"type": "string"},
    },
    required=("applier_principal", "commit_sha", "pr_url", "applied_at"),
)
_EFFECT_INTENT_SCHEMA = _object_schema(
    {
        "intent_id": _EFFECT_ID_SCHEMA,
        "revision": _EFFECT_REVISION_SCHEMA,
        "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA,
        "state": {"enum": list(_effect.EFFECT_STATES)},
        "release_digest": _EFFECT_DIGEST_SCHEMA,
        "item_id": {"type": "integer", "minimum": 1},
        "run_id": _RUN_ID_SCHEMA,
        "proposer_principal": {"type": "string", "minLength": 1},
        "repository": {"type": "string"},
        "base_commit": _EFFECT_COMMIT_SCHEMA,
        "title": {"type": "string"},
        "rationale": {"type": "string"},
        "unified_diff": {"type": "string"},
        "created_at": {"type": "string"},
        "acceptance": {"anyOf": [_EFFECT_ACCEPTANCE_SCHEMA, {"type": "null"}]},
        "rejection": {"anyOf": [_EFFECT_REJECTION_SCHEMA, {"type": "null"}]},
        "application": {"anyOf": [_EFFECT_APPLICATION_SCHEMA, {"type": "null"}]},
    },
    required=(
        "intent_id", "revision", "canonical_intent_digest", "state", "item_id", "run_id",
        "proposer_principal", "repository", "base_commit", "title", "rationale",
        "unified_diff", "created_at", "acceptance", "rejection", "application",
    ),
)
_EFFECT_RESULT = _result_schema(
    ("repo_id", "intent"),
    {"repo_id": {"type": "string"}, "intent": _EFFECT_INTENT_SCHEMA},
)
def _preview_object(properties):
    return _object_schema(properties, required=tuple(properties))

_EFFECT_ACCEPTANCE_SNAPSHOT_PROPERTIES = {
    "schema": {"const": "sprintctl-effect-acceptance-snapshot/v1"},
    "snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "authorizes_acceptance": {"const": False},
    "requested_basis_matches": {"type": "boolean"},
    "artifact_digest_matches": {"type": "boolean"},
    "intent": _preview_object({
        "intent_id": _EFFECT_ID_SCHEMA, "revision": _EFFECT_REVISION_SCHEMA,
        "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA,
        "state": {"enum": list(_effect.EFFECT_STATES)},
        "item_id": {"type": "integer", "minimum": 1},
        "release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
        "artifact_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    }),
    "current_item": _preview_object({
        "item_id": {"type": "integer", "minimum": 1},
        "status": {"enum": ["pending", "active", "blocked", "done"]},
        "work_edit_revision": {"type": "string", "minLength": 1},
        "status_revision": {"type": "string", "minLength": 1},
    }),
    "current_release": {"anyOf": [_preview_object({
        "release_digest": _EFFECT_DIGEST_SCHEMA,
        "item_revision": {"type": "string", "minLength": 1},
        "effect_verification_required": {"type": "boolean"},
        "effect_check_revisions": {"type": ["object", "null"],
            "maxProperties": 64,
            "propertyNames": {"type": "string", "minLength": 1, "maxLength": 200},
            "additionalProperties": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}},
        "revision_currentness": {"enum": ["unknown", "matches", "mismatch"]},
    }), {"type": "null"}]},
    "decision_facts": {"type": "array", "maxItems": 20, "items": _preview_object({
        "id": {"type": "integer", "minimum": 1},
        "kind": {"enum": sorted(_DECISION_KINDS)},
        "release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
        "evidence_digests": {"type": "array", "maxItems": 64, "items": _EFFECT_DIGEST_SCHEMA},
    })},
    "decision_count": {"type": "integer", "minimum": 0},
    "decision_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
    "decision_facts_complete": {"type": "boolean"},
    "reference_count": {"type": "integer", "minimum": 0},
    "reference_set_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
    "reference_facts_complete": {"type": "boolean"},
    "evidence_obligations": {"type": "array", "maxItems": 64, "items": _preview_object({
        "label": {"type": "string", "minLength": 1, "maxLength": 200},
        "label_truncated": {"type": "boolean"},
        "label_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "mapping": {"const": "unknown"},
    })},
    "evidence_obligation_count": {"type": "integer", "minimum": 0},
    "evidence_obligations_complete": {"type": "boolean"},
    "receipt": _preview_object({
        "status": {"enum": ["not-supplied", "not-found", "owner-record-observed", "unknown-no-evidence-authority"]},
        "comparison": {"enum": ["unknown", "matches", "mismatch"]},
        "evidence_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
        "check_revision_comparison": {"enum": ["unknown", "matches", "mismatch"]},
    }),
    "lease_settlement": {"const": "unknown"},
}
_EFFECT_ACCEPTANCE_SNAPSHOT_SCHEMA = _preview_object(_EFFECT_ACCEPTANCE_SNAPSHOT_PROPERTIES)


_PREVIEW_STATUS = {"type": "string", "enum": ["parsed", "unsupported", "unavailable"]}
_PREVIEW_NULLABLE_STRING = {"type": ["string", "null"]}
_PREVIEW_NULLABLE_COUNT = {"type": ["integer", "null"], "minimum": 0}
_EFFECT_PREVIEW_SCHEMA = _preview_object({
    "schema": {"const": "sprintctl-declared-effect-preview/v1"},
    "basis": _preview_object({
        "intent_id": _EFFECT_ID_SCHEMA, "revision": _EFFECT_REVISION_SCHEMA,
        "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA,
        "state": {"enum": list(_effect.EFFECT_STATES)},
        "item_id": {"type": "integer", "minimum": 1},
        "repository": {"type": "string"}, "base_commit": _EFFECT_COMMIT_SCHEMA,
        "release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
    }),
    "unified_diff_sha256": _EFFECT_DIGEST_SCHEMA,
    "declared_changes": _preview_object({
        "status": _PREVIEW_STATUS, "path_count": _PREVIEW_NULLABLE_COUNT,
        "paths": {"type": "array", "maxItems": 200, "items": _preview_object({
            "path": {"type": "string"}, "added_lines": _PREVIEW_NULLABLE_COUNT,
            "removed_lines": _PREVIEW_NULLABLE_COUNT, "source_only": {"type": "boolean"},
        })}, "redacted": {"type": "boolean"}, "truncated": {"type": "boolean"},
    }),
    "resources": _preview_object({"status": {"const": "not-declared"}}),
    "protected_policy": _preview_object({"status": {"const": "unknown"}}),
    "external_consequences": _preview_object({"status": {"const": "unknown"}}),
    "duplicate_relation": _preview_object({
        "status": {"enum": ["observed", "unknown"]},
        "candidate_intent_ids": {"type": "array", "maxItems": 200, "items": _EFFECT_ID_SCHEMA},
        "complete": {"type": "boolean"}, "scope": {"type": "string"},
    }),
    "receipt_source": _preview_object({
        "operation": {"const": "work.effect.get-v1"},
        "acceptance": {"enum": ["observed", "missing"]},
        "application": {"enum": ["observed", "missing"]},
    }),
    "authorization": {"const": "none; owner acceptance and execution checks remain required"},
})
_EFFECT_BINDING_PROPERTIES: dict[str, Any] = {
    "intent_id": _EFFECT_ID_ARGUMENT_SCHEMA,
    "revision": _EFFECT_REVISION_SCHEMA,
    "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA,
}
_EFFECT_BINDING_REQUIRED = ("intent_id", "revision", "canonical_intent_digest")
_BOUND_CAUSAL_BASIS_SCHEMA = _object_schema(
    {
        "expected_revision": {"type": "string", "pattern": _releases._RELEASE_REVISION_RE.pattern},
        "release_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "reserve_idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
        "commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "evidence_tail": _object_schema(
            {"item_id": {"type": "string", "minLength": 1},
             "chain_seq": {"type": "integer", "minimum": 0},
             "entry_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}},
            required=("item_id", "chain_seq", "entry_digest"),
        ),
    },
    required=("expected_revision", "release_digest", "reserve_idempotency_key", "commit_sha", "evidence_tail"),
)
_BOUND_RUN_BINDING_SCHEMA = _object_schema(
    {"repo_id": {"type": "string"}, "run_id": _RUN_ID_SCHEMA,
     "principal_id": {"type": "string", "minLength": 1},
     "workspace_id": {"type": "string", "minLength": 1},
     "client_id": _NULLABLE_BINDING_ID_SCHEMA, "grant_id": _NULLABLE_BINDING_ID_SCHEMA},
    required=("repo_id", "run_id", "principal_id", "workspace_id", "client_id", "grant_id"),
)
_BOUND_ADMISSION_SCHEMA = _object_schema(
    {"schema_version": {"const": "sprintctl-bound-proposal-admission/v1"},
     "causal_basis": _BOUND_CAUSAL_BASIS_SCHEMA,
     "run_binding": _BOUND_RUN_BINDING_SCHEMA,
     "reservation_id": {"type": "integer", "minimum": 1}},
    required=("schema_version", "causal_basis", "run_binding", "reservation_id"),
)
_EVALUATION_INPUTS_SCHEMA = {
    "type": "object",
    "maxProperties": 256,
    "propertyNames": {"type": "string", "minLength": 1, "maxLength": 256},
    "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 256},
}
_EVALUATION_TAIL_SCHEMA = _object_schema(
    {
        "item_id": {"type": "string", "minLength": 1},
        "chain_seq": {"type": "integer", "minimum": 0},
        "entry_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    },
    required=("item_id", "chain_seq", "entry_digest"),
)
_EVALUATION_BASIS_PROPERTIES = {
    "item_id": {"type": "integer", "minimum": 1},
    "expected_revision": {
        "type": "string",
        "pattern": _releases._RELEASE_REVISION_RE.pattern,
    },
    "release_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
}
_EVALUATION_REQUEST_BASIS = _object_schema(
    _EVALUATION_BASIS_PROPERTIES, required=tuple(_EVALUATION_BASIS_PROPERTIES)
)
_EVALUATION_CURRENT_BASIS = _object_schema(
    {
        **_EVALUATION_BASIS_PROPERTIES,
        "release_digest": {"type": ["string", "null"]},
        "release_item_revision": {"type": ["string", "null"]},
    },
    required=(*_EVALUATION_BASIS_PROPERTIES, "release_item_revision"),
)
_EVALUATION_NULL_CURRENT = {"anyOf": [_EVALUATION_CURRENT_BASIS, {"type": "null"}]}
_EVALUATION_STRINGS = {"type": "array", "items": {"type": "string"}}
_EVALUATION_VALIDITY = _object_schema(
    {
        "item_id": {"type": "string"},
        "digest": {"type": "string"},
        "status": {
            "enum": [
                "valid",
                "invalid",
                "not-yet-valid",
                "expired",
                "unknown",
                "changed",
            ]
        },
        "reason_codes": _EVALUATION_STRINGS,
        "missing_inputs": _EVALUATION_STRINGS,
        "changed_inputs": _EVALUATION_STRINGS,
    },
    required=(
        "item_id",
        "digest",
        "status",
        "reason_codes",
        "missing_inputs",
        "changed_inputs",
    ),
)
_EVALUATION_ASSERTION = _object_schema(
    {
        "evidence_item_id": {"type": "string"},
        "claim_index": {"type": "integer", "minimum": 0},
        "assertion": {"type": "object"},
        "validity_status": {
            "enum": [
                "valid",
                "invalid",
                "not-yet-valid",
                "expired",
                "unknown",
                "changed",
            ]
        },
        "authority": {"const": "authored-assertion"},
    },
    required=(
        "evidence_item_id",
        "claim_index",
        "assertion",
        "validity_status",
        "authority",
    ),
)
_EVALUATION_RESULT_PROPERTIES = {
    "schema_version": {"const": "evidence-evaluation/v1"},
    "evaluator_revision": {"const": "s4-evidence-evaluation/v1"},
    "repo_id": {"type": "string"},
    "run_binding": _BOUND_RUN_BINDING_SCHEMA,
    "subject": {"type": "string", "minLength": 1, "maxLength": 512},
    "as_of": {"type": "string"},
    "observed_at": {"type": "string"},
    "requested_basis": _EVALUATION_REQUEST_BASIS,
    "current_basis": _EVALUATION_NULL_CURRENT,
    "basis_status": {"enum": ["current", "missing", "stale"]},
    "evaluated_input_digests": _EVALUATION_INPUTS_SCHEMA,
    "input_assurance": {"const": "caller-supplied"},
    "source_watermark": _object_schema(
        {
            "evidence_chain": _object_schema(
                {
                    "tail": {"anyOf": [_EVALUATION_TAIL_SCHEMA, {"type": "null"}]},
                    "item_count": {"type": "integer", "minimum": 0},
                },
                required=("tail", "item_count"),
            ),
            "item_release": _EVALUATION_NULL_CURRENT,
            "execution_facts": {"type": "null"},
        },
        required=("evidence_chain", "item_release", "execution_facts"),
    ),
    "snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "evidence_validity": {"type": "array", "items": _EVALUATION_VALIDITY},
    "authored_assertions": {"type": "array", "items": _EVALUATION_ASSERTION},
    "authenticated_execution_facts": {"type": "array", "maxItems": 0},
    "authority_coverage": {"const": "unsupported"},
    "effect_state": {"const": "unknown"},
    "recommendation": {"const": "reconcile"},
    "reason_codes": _EVALUATION_STRINGS,
    "supporting_ids": {"type": "array", "maxItems": 0},
    "conflicting_ids": {"type": "array", "maxItems": 0},
    "authorizes_execution": {"const": False},
}
_EVALUATION_RESULT_SCHEMA = _result_schema(
    tuple(_EVALUATION_RESULT_PROPERTIES), _EVALUATION_RESULT_PROPERTIES
)

_COMPLETION_BASIS_SCHEMA = _object_schema(
    {
        "item_id": {"type": "integer", "minimum": 1},
        "expected_revision": {
            "type": "string", "pattern": _releases._RELEASE_REVISION_RE.pattern,
        },
        "release_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    required=("item_id", "expected_revision", "release_digest"),
)
_COMPLETION_CURRENT_BASIS = _object_schema(
    {
        "item_id": {"type": "integer", "minimum": 1},
        "expected_revision": {
            "type": "string", "pattern": _releases._RELEASE_REVISION_RE.pattern,
        },
        "release_digest": {"type": ["string", "null"]},
        "release_item_revision": {"type": ["string", "null"]},
    },
    required=("item_id", "expected_revision", "release_digest", "release_item_revision"),
)
_COMPLETION_VERIFICATION = _object_schema(
    {
        "profile": {"type": "string", "minLength": 1},
        "requirements": {"type": "array", "items": {"type": "string"}},
        "required_checks": {"type": "array", "items": {"type": "string"}},
    },
    required=("profile", "requirements", "required_checks"),
)
_COMPLETION_LEASE = _object_schema(
    {
        "lease_id": {"type": "string", "pattern": "^lease_[0-9A-HJKMNP-TV-Z]{26}$"},
        "state": {"enum": ["active", "superseded", "settled", "released"]},
        "stale": {"type": "boolean"},
        "verification": _COMPLETION_VERIFICATION,
    },
    required=("lease_id", "state", "stale", "verification"),
)
_COMPLETION_REPORT = _object_schema(
    {
        "report_id": {"type": "string", "pattern": "^outcome_[0-9A-HJKMNP-TV-Z]{26}$"},
        "lease_id": {"type": "string", "pattern": "^lease_[0-9A-HJKMNP-TV-Z]{26}$"},
        "outcome": {"enum": ["succeeded", "failed"]},
        "disposition": {"enum": ["settled", "recorded", "awaiting-verification", "rejected"]},
        "reason_code": {"type": ["string", "null"]},
        "verification_profile": {"type": "string", "minLength": 1},
        "decision_id": {"type": ["integer", "null"], "minimum": 1},
        "created_at": {"type": "string", "minLength": 1},
    },
    required=(
        "report_id", "lease_id", "outcome", "disposition", "reason_code",
        "verification_profile", "decision_id", "created_at",
    ),
)
_COMPLETION_DECISION = _object_schema(
    {
        "decision_id": {"type": "integer", "minimum": 1},
        "kind": {"enum": ["accept", "reject", "withdraw", "supersede", "revise"]},
        "release_digest": {"type": ["string", "null"]},
    },
    required=("decision_id", "kind", "release_digest"),
)
_COMPLETION_SUGGESTION = _object_schema(
    {
        "recommendation_id": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "action_kind": {
            "enum": [
                "no-change", "reverify", "inspect-conflict",
                "request-independent-review",
            ]
        },
        "target_item_id": {"type": "integer", "minimum": 1},
        "source_snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "current_revision": {
            "anyOf": [
                {"type": "string", "pattern": _releases._RELEASE_REVISION_RE.pattern},
                {"type": "null"},
            ]
        },
        "reason_code": {"type": "string", "minLength": 1},
        "next_operation": {"type": ["string", "null"]},
    },
    required=(
        "recommendation_id", "action_kind", "target_item_id", "source_snapshot_digest",
        "current_revision", "reason_code", "next_operation",
    ),
)
_COMPLETION_RESULT_PROPERTIES = {
    "repo_id": {"type": "string", "minLength": 1},
    "schema_version": {"const": "work-completion-assessment/v1"},
    "evaluator_revision": {"const": "sprintctl-owner-completion/v1"},
    "requested_basis": _COMPLETION_BASIS_SCHEMA,
    "current_basis": _COMPLETION_CURRENT_BASIS,
    "basis_status": {"enum": ["current", "missing", "stale"]},
    "snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "assessment": _object_schema(
        {
            "status": {
                "enum": [
                    "accepted-current", "stale-basis", "missing-release", "conflict",
                    "unknown", "blocked", "requires-judgment", "incomplete",
                ]
            },
            "reason_codes": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        required=("status", "reason_codes"),
    ),
    "owner_facts": _object_schema(
        {
            "item_status": {"enum": ["pending", "active", "blocked", "done"]},
            "resolution": {
                "anyOf": [
                    {"enum": ["accepted", "rejected", "withdrawn", "superseded"]},
                    {"type": "null"},
                ]
            },
            "terminal_decision": {"anyOf": [_COMPLETION_DECISION, {"type": "null"}]},
            "verification": _COMPLETION_VERIFICATION,
            "active_lease": {"anyOf": [_COMPLETION_LEASE, {"type": "null"}]},
            "outcome_reports": {
                "type": "array", "maxItems": 64, "items": _COMPLETION_REPORT,
            },
            "blocker_item_ids": {
                "type": "array", "maxItems": 64, "items": {"type": "integer", "minimum": 1},
            },
            "source_completeness": _object_schema(
                {
                    "current_releases": {"type": "boolean"},
                    "leases": {"type": "boolean"},
                    "outcome_reports": {"type": "boolean"},
                    "blockers": {"type": "boolean"},
                },
                required=("current_releases", "leases", "outcome_reports", "blockers"),
            ),
            "observed_at": {"type": "string", "minLength": 1},
        },
        required=(
            "item_status", "resolution", "terminal_decision", "verification", "active_lease",
            "outcome_reports", "blocker_item_ids", "source_completeness", "observed_at",
        ),
    ),
    "follow_up": _object_schema(
        {
            "dedupe_scope": {"const": "exact-source-snapshot"},
            "prior_rejection_tracking": {"const": "unsupported"},
            "suggestions": {
                "type": "array", "maxItems": 1, "items": _COMPLETION_SUGGESTION,
            },
        },
        required=("dedupe_scope", "prior_rejection_tracking", "suggestions"),
    ),
    "authorizes_acceptance": {"const": False},
}
_COMPLETION_RESULT_SCHEMA = _result_schema(
    tuple(_COMPLETION_RESULT_PROPERTIES), _COMPLETION_RESULT_PROPERTIES
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
            ("repo_id", "item", "events", "active_reservations", "refs", "deps"),
            {
                "repo_id": {"type": "string"},
                "item": {"type": "object"},
                "events": {"type": "array", "items": {"type": "object"}},
                "active_reservations": {"type": "array", "items": {"type": "object"}},
                "refs": {"type": "array", "items": {"type": "object"}},
                "deps": {"type": "object"},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.public.list-v1",
        _object_schema({}),
        _PUBLIC_WORK_LIST_RESULT,
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.public.item-v1",
        _object_schema({"work_id": _PUBLIC_WORK_ID}, required=("work_id",)),
        _PUBLIC_WORK_ITEM_RESULT,
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.run.register-v1",
        _object_schema(
            {
                "harness_id": {"type": "string", "minLength": 1},
                "harness_build": {"type": "string", "minLength": 1},
                "model_id": {"type": "string", "minLength": 1},
                "recipe_id": {"type": "string", "minLength": 1},
                "observed_profile": _OBSERVED_PROFILE_SCHEMA,
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
                # agentops#2525: the run this one continues -- a run of this
                # repository in the caller's workspace; its principal, client
                # and grant may differ.  Any other id (unknown, malformed or
                # another workspace's) is predecessor-not-eligible, one code
                # for all, so the format is checked there rather than here.
                # Served with work.run.predecessor-context-v1: a consumer
                # detects both from that operation's presence in the catalog.
                # A string or absent: callers omit it for "no predecessor".
                # Needs work:read as well (authority-required otherwise).
                "predecessor_run_id": {"type": "string", "minLength": 1},
            },
            required=(
                "harness_id", "harness_build", "model_id", "recipe_id",
                "observed_profile", "idempotency_key",
            ),
        ),
        _result_schema(
            ("repo_id", "run"),
            {"repo_id": {"type": "string"}, "run": _REGISTERED_RUN_RESULT_OBJECT},
        ),
        "work:evidence",
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.run.register-correlated-v1",
        _object_schema(
            {
                "harness_id": {"type": "string", "minLength": 1},
                "harness_build": {"type": "string", "minLength": 1},
                "model_id": {"type": "string", "minLength": 1},
                "recipe_id": {"type": "string", "minLength": 1},
                "observed_profile": _OBSERVED_PROFILE_SCHEMA,
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
                "predecessor_run_id": {"type": "string", "minLength": 1},
            },
            required=("harness_id", "harness_build", "model_id", "recipe_id", "observed_profile", "idempotency_key"),
        ),
        _CORRELATED_RUN_RESULT_OBJECT,
        "work:evidence",
        "write",
        "required",
    ),
    WorkOperationContract(
        "work.run.invocation-receipt-get-v1",
        _object_schema(
            {
                "run_id": _RUN_ID_SCHEMA,
                "invocation_id": {"type": "string", "pattern": "^inv_[0-9A-HJKMNP-TV-Z]{26}$"},
                "attempt_id": {"type": "string", "pattern": "^att_[0-9A-HJKMNP-TV-Z]{26}$"},
            },
            required=("run_id", "invocation_id", "attempt_id"),
        ),
        _RUN_INVOCATION_RECEIPT_SCHEMA,
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.run.resolve-v1",
        _object_schema({"run_id": _RUN_ID_SCHEMA}, required=("run_id",)),
        # The run's full binding, echoed so the edge can compare it with the
        # caller's own (vuoro_mcp_edge.record_tools.SprintctlRecordStore).
        _result_schema(
            ("repo_id", "run_id", "principal_id", "workspace_id", "client_id", "grant_id"),
            {
                "repo_id": {"type": "string"},
                "run_id": _RUN_ID_SCHEMA,
                "principal_id": {"type": "string", "minLength": 1},
                "workspace_id": {"type": "string", "minLength": 1},
                "client_id": _NULLABLE_BINDING_ID_SCHEMA,
                "grant_id": _NULLABLE_BINDING_ID_SCHEMA,
            },
        ),
        # No authority requirement: this only ever echoes the caller's own
        # binding back to them (run-not-found otherwise, one code for both
        # an unknown run and someone else's), so it cannot disclose anything
        # a caller could not already state about itself.  Any bucket that
        # carries a run_id (record, coordinate or propose) needs to resolve
        # it, and gating this on one bucket's authority would refuse the
        # others -- see the E2 final report for why this is not "work:read".
        None,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.run.predecessor-context-v1",
        _object_schema(
            {
                "run_id": _RUN_ID_SCHEMA,
                # Each list is paged by its own cursor; a page holds at most
                # `limit` notes and `limit` evidence items.
                "limit": {
                    "type": "integer", "minimum": 1,
                    "maximum": PREDECESSOR_CONTEXT_MAX_LIMIT,
                    "default": PREDECESSOR_CONTEXT_DEFAULT_LIMIT,
                },
                "after_note_id": {"type": "integer", "minimum": 0},
                "after_chain_seq": {"type": "integer", "minimum": 0},
            },
            required=("run_id",),
        ),
        # agentops#2525: the predecessor's session notes and evidence, read
        # through the caller's own run (run-not-found otherwise, as for
        # work.run.resolve-v1).  One hop; the predecessor's run is never
        # resolved to the caller.  Continuation transfers context, not
        # authority, so reading it needs work:read and nothing more.
        _result_schema(
            (
                "repo_id", "run_id", "predecessor_run_id", "session_notes", "evidence",
                "next_after_note_id", "next_after_chain_seq",
            ),
            {
                "repo_id": {"type": "string"},
                "run_id": _RUN_ID_SCHEMA,
                "predecessor_run_id": {"anyOf": [_RUN_ID_SCHEMA, {"type": "null"}]},
                "session_notes": {"type": "array", "items": _SESSION_NOTE_RESULT_OBJECT},
                "evidence": {"type": "array", "items": _EVIDENCE_ITEM_SCHEMA},
                # The cursor for the next page of that list; null when the
                # list is exhausted (so non-null means this page was cut).
                "next_after_note_id": {"type": ["integer", "null"], "minimum": 1},
                "next_after_chain_seq": {"type": ["integer", "null"], "minimum": 0},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.run.continuation-delta-v1",
        _object_schema({
            "run_id": _RUN_ID_SCHEMA,
            "item_id": {"type": "integer", "minimum": 1},
            "checkpoint": {"type": "object", "properties": {
                "repo_id": {"type": "string", "minLength": 1},
                "item_id": {"type": "integer", "minimum": 1},
                "work_edit_revision": {"type": "string", "minLength": 1},
                "status_revision": {"type": "string", "minLength": 1},
                "current_release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
                "current_decision_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
                "current_reference_set_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
            }, "required": ["repo_id", "item_id", "work_edit_revision", "status_revision",
                           "current_release_digest", "current_decision_digest", "current_reference_set_digest"],
                "additionalProperties": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 64, "default": 32},
            "cursor": {"type": "object", "properties": {
                "expected_snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "checkpoint_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "last_reference_id": {"type": "integer", "minimum": 1},
            }, "required": ["expected_snapshot_digest", "checkpoint_digest", "last_reference_id"],
                "additionalProperties": False},
            "after_note_id": {"type": "integer", "minimum": 0},
            "after_chain_seq": {"type": "integer", "minimum": 0},
        }, required=("run_id", "item_id")),
        _result_schema(("repo_id", "delta"), {"repo_id": {"type": "string"},
            "delta": _preview_object({
                "schema": {"const": "sprintctl-work-continuation-delta/v1"},
                "snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "authorizes_action": {"const": False},
                "authorization_scope": _preview_object({
                    "repo_id": {"type": "string"}, "run_id": _RUN_ID_SCHEMA,
                    "run_owned": {"const": True},
                }),
                "work": _preview_object({
                    "item_id": {"type": "integer", "minimum": 1},
                    "status": {"enum": ["pending", "active", "blocked", "done"]},
                    "work_edit_revision": {"type": "string", "minLength": 1},
                    "status_revision": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "maxLength": 200},
                    "title_truncated": {"type": "boolean"},
                    "description_excerpt": {"type": "string", "maxLength": 500},
                    "description_truncated": {"type": "boolean"},
                    "description_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                }),
                "current": _preview_object({
                    "repo_id": {"type": "string"}, "item_id": {"type": "integer", "minimum": 1},
                    "work_edit_revision": {"type": "string", "minLength": 1},
                    "status_revision": {"type": "string", "minLength": 1},
                    "current_release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
                    "current_decision_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
                    "current_reference_set_digest": {"anyOf": [{"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}, {"type": "null"}]},
                }),
                "comparison": _preview_object({key: {"enum": ["unknown", "changed", "unchanged"]}
                    for key in ("work_edit_revision", "status_revision", "current_release_digest",
                                "current_decision_digest", "current_reference_set_digest")}),
                "reference_page": {"type": "array", "maxItems": 64, "items": _preview_object({
                    "reference_id": {"type": "integer", "minimum": 1},
                    "ref_type": {"type": "string", "maxLength": 32},
                    "url": {"type": "string", "maxLength": 2048},
                    "url_truncated": {"type": "boolean"},
                    "url_is_complete": {"type": "boolean"},
                    "url_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "label": {"type": "string", "maxLength": 200},
                    "label_truncated": {"type": "boolean"},
                    "label_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                })},
                "reference_page_complete": {"type": "boolean"},
                "next_reference_cursor": {"anyOf": [
                    {"type": "object", "properties": {
                        "expected_snapshot_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                        "checkpoint_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                        "last_reference_id": {"type": "integer", "minimum": 1},
                    }, "required": ["expected_snapshot_digest", "checkpoint_digest", "last_reference_id"],
                        "additionalProperties": False}, {"type": "null"}]},
                "reference_git_correspondence": {"const": "unknown"},
                "reference_digest_domain": {"const": "owner-reference-records/v1"},
                "source_bytes": {"const": "unknown"},
                "external_missing_content": {"const": "unknown"},
                "source_checkpoint": {"anyOf": [_preview_object({
                    "item_id": {"type": "integer", "minimum": 1},
                    "note_id": {"type": "integer", "minimum": 1},
                    "created_at": {"type": "string"},
                    "release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
                    "declared_git_sha": {"anyOf": [_EFFECT_COMMIT_SCHEMA, {"type": "null"}],
                                         "description": "Owner-declared only; not checked against Git."},
                    "acknowledgement": {"const": "unacknowledged"},
                    "git_currentness": {"const": "unknown"},
                }), {"type": "null"}]},
                "decision_page": {"type": "array", "maxItems": 64, "items": _preview_object({
                    "id": {"type": "integer", "minimum": 1}, "kind": {"enum": sorted(_DECISION_KINDS)},
                    "release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
                    "evidence_digests": {"type": "array", "maxItems": 64, "items": _EFFECT_DIGEST_SCHEMA},
                })},
                "decision_count": {"type": "integer", "minimum": 0},
                "decision_page_complete": {"type": "boolean"},
                "latest_decision": {"anyOf": [_preview_object({
                    "id": {"type": "integer", "minimum": 1},
                    "kind": {"enum": sorted(_DECISION_KINDS)},
                    "release_digest": {"anyOf": [_EFFECT_DIGEST_SCHEMA, {"type": "null"}]},
                    "evidence_digests": {"type": "array", "maxItems": 64, "items": _EFFECT_DIGEST_SCHEMA},
                }), {"type": "null"}]},
                "latest_decision_id": {"type": ["integer", "null"], "minimum": 1},
                "current_release": {"anyOf": [_preview_object({
                    "release_digest": _EFFECT_DIGEST_SCHEMA, "item_revision": {"type": "string", "minLength": 1},
                }), {"type": "null"}]},
                "predecessor_run_id": {"anyOf": [_RUN_ID_SCHEMA, {"type": "null"}]},
                "predecessor_notes": {"type": "array", "maxItems": 64, "items": _preview_object({
                    "note_id": {"type": "integer", "minimum": 1}, "created_at": {"type": "string"},
                })},
                "predecessor_evidence": {"type": "array", "maxItems": 64, "items": _preview_object({
                    "item_id": {"type": "string"}, "chain_seq": {"type": "integer", "minimum": 0},
                    "digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                    "kind": {"type": "string"},
                })},
                "next_after_note_id": {"type": ["integer", "null"], "minimum": 1},
                "next_after_chain_seq": {"type": ["integer", "null"], "minimum": 0},
            })}),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.evidence.tail-v1",
        _object_schema({"run_id": _RUN_ID_SCHEMA}, required=("run_id",)),
        _result_schema(
            ("repo_id", "run_id", "item"),
            {
                "repo_id": {"type": "string"},
                "run_id": _RUN_ID_SCHEMA,
                "item": {"anyOf": [_EVIDENCE_ITEM_SCHEMA, {"type": "null"}]},
            },
        ),
        "work:evidence",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.evidence.evaluate-v1",
        _object_schema(
            {
                "run_id": _RUN_ID_SCHEMA,
                "subject": {"type": "string", "minLength": 1, "maxLength": 512},
                "basis": _object_schema(
                    {
                        "item_id": {"type": "integer", "minimum": 1},
                        "expected_revision": {
                            "type": "string",
                            "pattern": _releases._RELEASE_REVISION_RE.pattern,
                        },
                        "release_digest": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{64}$",
                        },
                    },
                    required=("item_id", "expected_revision", "release_digest"),
                ),
                "as_of": {"type": "string", "minLength": 1},
                "current_input_digests": _EVALUATION_INPUTS_SCHEMA,
                "expected_tail": {
                    "anyOf": [
                        {"type": "null"},
                        _object_schema(
                            {
                                "item_id": {"type": "string", "minLength": 1},
                                "chain_seq": {"type": "integer", "minimum": 0},
                                "entry_digest": {
                                    "type": "string",
                                    "pattern": "^sha256:[0-9a-f]{64}$",
                                },
                            },
                            required=("item_id", "chain_seq", "entry_digest"),
                        ),
                    ]
                },
            },
            required=(
                "run_id",
                "subject",
                "basis",
                "as_of",
                "current_input_digests",
                "expected_tail",
            ),
        ),
        _EVALUATION_RESULT_SCHEMA,
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.completion.assess-v1",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "expected_revision": {
                    "type": "string", "pattern": _releases._RELEASE_REVISION_RE.pattern,
                },
                "release_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            required=("item_id", "expected_revision", "release_digest"),
        ),
        _COMPLETION_RESULT_SCHEMA,
        "work:read",
        "read",
        "not-allowed",
        required_remote_schema_version=28,
    ),
    WorkOperationContract(
        "work.evidence.append-v1",
        _object_schema(
            {
                "run_id": _RUN_ID_SCHEMA,
                "item_id": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "minLength": 1},
                "ref": {"type": "string", "minLength": 1},
                "digest": {"type": "string", "minLength": 1},
                "collector": {"type": "string", "minLength": 1},
                "validity": _VALIDITY_WINDOW_SCHEMA,
                "claims": {"type": "array", "items": _CLAIM_SCHEMA, "default": []},
                "provenance": {"type": "object", "default": {}},
                "chain_seq": {"type": "integer", "minimum": 0},
                "chain_prev_digest": {"type": ["string", "null"]},
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
            },
            required=(
                "run_id", "item_id", "kind", "ref", "digest", "collector",
                "validity", "chain_seq", "chain_prev_digest", "idempotency_key",
            ),
        ),
        _result_schema(
            ("repo_id", "run_id", "item"),
            {
                "repo_id": {"type": "string"},
                "run_id": _RUN_ID_SCHEMA,
                "item": _EVIDENCE_ITEM_SCHEMA,
            },
        ),
        "work:evidence",
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.session-note.write-v1",
        _object_schema(
            {
                "run_id": _RUN_ID_SCHEMA,
                "note": {
                    "type": "string", "minLength": 1,
                    "maxLength": SESSION_NOTE_MAX_LENGTH,
                },
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
            },
            required=("run_id", "note", "idempotency_key"),
        ),
        _result_schema(
            ("repo_id", "run_id", "note_id", "note", "created_at"),
            {
                "repo_id": {"type": "string"},
                "run_id": _RUN_ID_SCHEMA,
                "note_id": {"type": "integer", "minimum": 1},
                "note": {"type": "string"},
                "created_at": {"type": "string"},
            },
        ),
        "work:evidence",
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.lease.acquire-v1",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "run_id": _RUN_ID_SCHEMA,
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
            },
            required=("item_id", "run_id", "idempotency_key"),
        ),
        _CLAIM_RESULT,
        "work:claim",
        "write",
        "not-allowed",
        required_remote_schema_version=29,
    ),
    WorkOperationContract(
        "work.lease.heartbeat-v1",
        _object_schema(
            {"lease_id": _LEASE_ID_SCHEMA, "run_id": _RUN_ID_SCHEMA},
            required=("lease_id", "run_id"),
        ),
        _result_schema(
            ("repo_id", "lease"), {"repo_id": {"type": "string"}, "lease": _LEASE_SCHEMA}
        ),
        "work:claim",
        "write",
        "not-allowed",
        required_remote_schema_version=29,
    ),
    WorkOperationContract(
        "work.lease.report-outcome-v1",
        _REPORT_OUTCOME_INPUT,
        _REPORT_OUTCOME_RESULT,
        "work:claim",
        "write",
        "not-allowed",
        required_remote_schema_version=29,
    ),
    WorkOperationContract(
        "work.lease.complete-v1",
        _REPORT_OUTCOME_INPUT,
        _REPORT_OUTCOME_RESULT,
        "work:claim",
        "write",
        "not-allowed",
        required_remote_schema_version=29,
        deprecation={
            "deprecated": True,
            "replacement": "work.lease.report-outcome-v1",
            "sunset_at": None,
        },
    ),
    WorkOperationContract(
        "work.lease.read-v1",
        _object_schema({"item_id": {"type": "integer", "minimum": 1}}, required=("item_id",)),
        _result_schema(
            (
                "repo_id", "item_id", "current_lease", "leases", "outcome_reports",
                "verification", "evaluated_at",
            ),
            {
                "repo_id": {"type": "string"},
                "item_id": {"type": "integer", "minimum": 1},
                "current_lease": {"anyOf": [_CURRENT_LEASE_SCHEMA, {"type": "null"}]},
                "leases": {"type": "array", "items": _LEASE_SCHEMA},
                "outcome_reports": {"type": "array", "items": _OUTCOME_REPORT_SCHEMA},
                "verification": _VERIFICATION_SCHEMA,
                # Not required: an older authority does not send it.  null
                # when the item is not parked (agentops#2543).
                "parked": {"anyOf": [_PARKING_SCHEMA, {"type": "null"}]},
                "evaluated_at": {"type": "string"},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
        required_remote_schema_version=29,
    ),
    WorkOperationContract(
        _effect.OPERATION_PROPOSE,
        _object_schema(
            {
                "run_id": _RUN_ID_SCHEMA,
                "item_id": {"type": "integer", "minimum": 1},
                "repository": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_REPOSITORY},
                "base_commit": _EFFECT_COMMIT_SCHEMA,
                "title": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_TITLE},
                "rationale": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_RATIONALE},
                "unified_diff": {
                    "type": "string", "minLength": 1, "maxLength": _effect.MAX_UNIFIED_DIFF,
                },
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
            },
            required=(
                "run_id", "item_id", "repository", "base_commit", "title", "rationale",
                "unified_diff", "idempotency_key",
            ),
        ),
        _EFFECT_RESULT,
        _effect.AUTHORITY_PROPOSE,
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_BOUND_PROPOSE,
        _object_schema(
            {
                "causal_basis": _BOUND_CAUSAL_BASIS_SCHEMA,
                "run_id": _RUN_ID_SCHEMA,
                "item_id": {"type": "integer", "minimum": 1},
                "repository": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_REPOSITORY},
                "base_commit": _EFFECT_COMMIT_SCHEMA,
                "title": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_TITLE},
                "rationale": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_RATIONALE},
                "unified_diff": {
                    "type": "string", "minLength": 1, "maxLength": _effect.MAX_UNIFIED_DIFF,
                },
                "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
            },
            required=(
                "run_id", "item_id", "repository", "base_commit", "title", "rationale",
                "unified_diff", "idempotency_key", "causal_basis",
            ),
        ),
        _result_schema(
            ("repo_id", "intent", "admission"),
            {"repo_id": {"type": "string"}, "intent": _EFFECT_INTENT_SCHEMA,
             "admission": _BOUND_ADMISSION_SCHEMA},
        ),
        _effect.AUTHORITY_PROPOSE,
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_GET,
        _object_schema({"intent_id": _EFFECT_ID_ARGUMENT_SCHEMA}, required=("intent_id",)),
        _EFFECT_RESULT,
        _effect.AUTHORITY_GET,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_EXPORT_PROPOSAL,
        _object_schema({
            "intent_id": _EFFECT_ID_ARGUMENT_SCHEMA,
            "intent_revision": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
        }, required=("intent_id", "intent_revision")),
        _effect_export.RESULT_SCHEMA,
        _effect.AUTHORITY_GET,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_EXPORT_PROPOSAL_V3,
        _object_schema({"intent_id": _EFFECT_ID_ARGUMENT_SCHEMA,
                        "intent_revision": {"type": "integer", "minimum": 1, "maximum": 9007199254740991}},
                       required=("intent_id", "intent_revision")),
        _verification_export.RESULT_SCHEMA_V3,
        _effect.AUTHORITY_GET, "read", "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_BASIS,
        _object_schema({"intent_id": {"type": "string", "minLength": 1, "maxLength": 200},
                        "intent_revision": {"type": "integer", "minimum": 1, "maximum": 9007199254740991}},
                       required=("intent_id", "intent_revision")),
        _effect_basis.RESULT_SCHEMA,
        _effect.AUTHORITY_GET, "read", "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_ACCEPTANCE_SNAPSHOT,
        _object_schema({
            "intent_id": _EFFECT_ID_ARGUMENT_SCHEMA,
            "revision": _EFFECT_REVISION_SCHEMA,
            "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA,
            "artifact_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "verification_ref": {"type": "object", "properties": {
                "run_id": _RUN_ID_SCHEMA,
                "item_id": {"type": "string", "minLength": 1, "maxLength": 128},
            }, "required": ["run_id", "item_id"], "additionalProperties": False},
        }, required=("intent_id", "revision", "canonical_intent_digest", "artifact_sha256")),
        _result_schema(("repo_id", "snapshot"), {
            "repo_id": {"type": "string"}, "snapshot": _EFFECT_ACCEPTANCE_SNAPSHOT_SCHEMA,
        }),
        _effect.AUTHORITY_GET,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_PREVIEW,
        _object_schema({"intent_id": _EFFECT_ID_ARGUMENT_SCHEMA,
                        "disclose_paths": {"type": "boolean"}}, required=("intent_id",)),
        _result_schema(("repo_id", "preview"),
                       {"repo_id": {"type": "string"}, "preview": _EFFECT_PREVIEW_SCHEMA}),
        _effect.AUTHORITY_GET,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_LIST_PROPOSED,
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": _effect.LIST_MAX_LIMIT},
            }
        ),
        _result_schema(
            ("repo_id", "intents"),
            {
                "repo_id": {"type": "string"},
                "intents": {"type": "array", "items": _EFFECT_INTENT_SCHEMA},
            },
        ),
        _effect.AUTHORITY_LIST_PROPOSED,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_LIST_ACCEPTED,
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": _effect.LIST_MAX_LIMIT},
            }
        ),
        _result_schema(
            ("repo_id", "intents"),
            {
                "repo_id": {"type": "string"},
                "intents": {"type": "array", "items": _EFFECT_INTENT_SCHEMA},
            },
        ),
        _effect.AUTHORITY_LIST_ACCEPTED,
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_ACCEPT,
        _object_schema({**_EFFECT_BINDING_PROPERTIES, "verification_ref": _EFFECT_VERIFICATION_REF_SCHEMA},
                       required=_EFFECT_BINDING_REQUIRED),
        _EFFECT_RESULT,
        _effect.AUTHORITY_ACCEPT,
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_REJECT,
        _object_schema(
            {
                **_EFFECT_BINDING_PROPERTIES,
                "reason": {"type": "string", "minLength": 1, "maxLength": _effect.MAX_REASON},
            },
            required=(*_EFFECT_BINDING_REQUIRED, "reason"),
        ),
        _EFFECT_RESULT,
        _effect.AUTHORITY_REJECT,
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        _effect.OPERATION_MARK_APPLIED,
        _object_schema(
            {
                **_EFFECT_BINDING_PROPERTIES,
                "commit_sha": _EFFECT_COMMIT_SCHEMA,
                "pr_url": _EFFECT_PR_URL_SCHEMA,
            },
            required=(*_EFFECT_BINDING_REQUIRED, "commit_sha", "pr_url"),
        ),
        _EFFECT_RESULT,
        _effect.AUTHORITY_MARK_APPLIED,
        "write",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.item-projection",
        _object_schema(
            {"item_id": {"type": "integer", "minimum": 1}}, required=("item_id",)
        ),
        _result_schema(
            ("repo_id", "projection"),
            {
                "repo_id": {"type": "string"},
                "projection": {
                    "type": "object",
                    "required": [
                        "contract_version", "provider_id", "resource_id",
                        "revision", "data_class", "item", "truncated",
                    ],
                    "properties": {
                        "contract_version": {"const": "work-item-context/v1"},
                        "provider_id": {"const": "sprintctl.work-item"},
                        "resource_id": {"type": "string", "minLength": 3},
                        "revision": {"type": "string", "minLength": 1},
                        "data_class": {"const": "untrusted-work-state"},
                        "item": {"type": "object"},
                        "truncated": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.validate.item-status-mutation",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "expected_revision": {"type": ["string", "null"]},
            },
            required=("item_id", "expected_revision"),
        ),
        _result_schema(
            (
                "repo_id", "allowed", "reason", "current_revision", "projection",
            ),
            {
                "repo_id": {"type": "string"},
                "allowed": {"type": "boolean"},
                "reason": {"type": "string"},
                "current_revision": {"type": "string"},
                "projection": {"type": "object"},
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
        "work.read.context",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            (
                "contract_version", "sprint", "summary", "active_reservations",
                "active_unreserved_items", "conflicts", "ready_items",
                "blocked_items", "stale_items", "recent_decisions", "next_action",
            ),
            {
                "contract_version": {"const": "1"}, "sprint": {"type": "object"},
                "summary": {"type": "object"}, "active_reservations": {"type": "array", "items": {"type": "object"}},
                "active_unreserved_items": {"type": "array", "items": {"type": "object"}},
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
                "sprint": {"type": ["object", "null"]},
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
        # The three dispatch fields are advisory facts the handler has emitted
        # since the project variant gained them; that variant's contract was
        # updated and this one was not, so every served next-work call failed
        # its own result schema.  Keep the two in step.
        _result_schema(
            ("repo_id", "sprint", "ready_items", "graph_ready", "dispatch_admissible", "dispatch_reason"),
            {
                "repo_id": {"type": "string"},
                "sprint": {"type": "object"},
                "ready_items": {"type": "array", "items": {"type": "object"}},
                "graph_ready": {"type": "boolean"},
                "dispatch_admissible": {"enum": ["admissible", "inadmissible", "unknown"]},
                "dispatch_reason": {"type": "string"},
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
            ("bundle_type", "bundle_version", "sprintctl_version", "generated_at", "generated_from", "sprint", "summary", "active_reservations", "conflicts", "work", "recent_decisions", "recent_events", "next_action", "delta_since_last_handoff", "freshness", "evidence", "git_context", "last_checkpoint", "reservation_model", "resume_instructions", "agent_shutdown_protocol", "items", "events"),
            {"bundle_type": {"const": "handoff"}, "bundle_version": {"const": "1"}, "sprintctl_version": {"type": "string"}, "generated_at": {"type": "string"}, "generated_from": {"type": "object"}, "sprint": {"type": "object"}, "summary": {"type": "object"}, "active_reservations": {"type": "array", "items": {"type": "object"}}, "conflicts": {"type": "array", "items": {"type": "object"}}, "work": {"type": "object"}, "recent_decisions": {"type": "array", "items": {"type": "object"}}, "recent_events": {"type": "array", "items": {"type": "object"}}, "next_action": {"type": "object"}, "delta_since_last_handoff": {"type": "object"}, "freshness": {"type": "object"}, "evidence": {"type": "object"}, "git_context": {"type": ["object", "null"]}, "last_checkpoint": {"type": ["object", "null"]}, "reservation_model": {"type": "object"}, "resume_instructions": {"type": "array", "items": {"type": "string"}}, "agent_shutdown_protocol": {"type": "object"}, "items": {"type": "array", "items": {"type": "object"}}, "events": {"type": "array", "items": {"type": "object"}}},
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
            ("contract_version", "sprint", "summary", "ready_items", "dependency_waiting_items", "active_reservations", "active_unreserved_items", "checkpointed_unacked", "conflicts", "next_action", "recommended_commands", "recommended_command_bundle"),
            {
                # The served aggregate has emitted "2" since the
                # reservation-derived explanation landed (2026-08-14); this
                # const still said "1", so every served explain call failed
                # its own result schema.  The local CLI aggregate remains at
                # "1" -- a real divergence, tracked separately.
                "contract_version": {"const": "2"}, "sprint": {"type": "object"},
                "summary": {"type": "object"}, "ready_items": {"type": "array", "items": {"type": "object"}},
                "dependency_waiting_items": {"type": "array", "items": {"type": "object"}},
                "active_reservations": {"type": "array", "items": {"type": "object"}},
                "active_unreserved_items": {"type": "array", "items": {"type": "object"}},
                # S6 ledger checkpoint (agentops #2450, slice 2450-2 /
                # #2474): newest-per-item unacked lane.checkpoint notes,
                # scoped to this sprint the same way ready/waiting are.
                "checkpointed_unacked": {"type": "array", "items": {"type": "object"}},
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
                "events": {"type": "array", "items": _EVENT_ITEM_SCHEMA},
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
        "work.sprint.kind",
        _object_schema({
            "sprint_id": {"type": "integer", "minimum": 1, "maximum": (1 << 63) - 1},
            "kind": {"enum": ["active_sprint", "backlog", "archive"]},
            "expected_revision": {"type": "string", "minLength": 1},
        }, required=("sprint_id", "kind", "expected_revision")),
        _result_schema(("repo_id", "sprint"), {
            "repo_id": {"type": "string"}, "sprint": {"type": "object"},
        }),
        "work:sprint", "write", "not-allowed",
    ),
    WorkOperationContract(
        "work.event.add",
        _object_schema(
            {
                "sprint_id": {"type": "integer", "minimum": 1},
                "event_type": {"type": "string", "minLength": 1},
                "work_item_id": {"type": ["integer", "null"], "minimum": 1},
                # Optional, never authorizing: it only lets the reservation
                # ledger attribute this mutation to the caller's session.
                "session_id": {"type": ["string", "null"], "minLength": 1},
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
                "session_id": {"type": ["string", "null"], "minLength": 1},
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
    WorkOperationContract(
        "work.item.priority",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 9},
            },
            required=("item_id", "priority"),
        ),
        _result_schema(("item",), {"item": {"type": "object"}}),
        "work:lifecycle",
        "write",
        "not-allowed",
    ),
    *(
        WorkOperationContract(name, _object_schema(properties, required=required), _result_schema(("repo_id", "item_id", result_id), {"repo_id": {"type": "string"}, "item_id": {"type": "integer", "minimum": 1}, result_id: {"type": "integer", "minimum": 1}}), "work:lifecycle", "write", "not-allowed")
        for name, properties, required, result_id in (
            ("work.item.ref.add", {"item_id": {"type": "integer", "minimum": 1}, "ref_type": {"type": "string", "minLength": 1}, "url": {"type": "string", "minLength": 1}, "label": {"type": "string", "default": ""}, "session_id": {"type": ["string", "null"], "minLength": 1},}, ("item_id", "ref_type", "url"), "ref_id"),
            ("work.item.ref.remove", {"item_id": {"type": "integer", "minimum": 1}, "ref_id": {"type": "integer", "minimum": 1}, "session_id": {"type": ["string", "null"], "minLength": 1},}, ("item_id", "ref_id"), "ref_id"),
            ("work.item.dep.add", {"item_id": {"type": "integer", "minimum": 1}, "blocked_item_id": {"type": "integer", "minimum": 1}, "session_id": {"type": ["string", "null"], "minLength": 1},}, ("item_id", "blocked_item_id"), "dep_id"),
            ("work.item.dep.remove", {"item_id": {"type": "integer", "minimum": 1}, "dep_id": {"type": "integer", "minimum": 1}, "session_id": {"type": ["string", "null"], "minLength": 1},}, ("item_id", "dep_id"), "dep_id"),
        )
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
                "session_id": {"type": ["string", "null"], "minLength": 1},
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
                # S6 ledger-checkpoint fields (agentops #2450,
                # docs/plans/2450-s6-ledger-checkpoint.md): optional,
                # meaningful for ``lane.checkpoint`` notes only.
                "release_digest": {"type": ["string", "null"]},
                "worktree_host": {"type": ["string", "null"]},
                "predecessor_session": {"type": ["string", "null"]},
                "acked_by": {"type": ["string", "null"]},
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
        "work.decision.record",
        _object_schema(
            {
                "item_id": {"type": "integer", "minimum": 1},
                "kind": {"enum": list(_DECISION_KINDS)},
                "rationale": {"type": "string"},
                "evidence_digests": {
                    "type": "array",
                    "items": _SHA256_HEX,
                    "maxItems": 64,
                },
                "release_digest": {**_SHA256_HEX, "type": ["string", "null"]},
                "superseded_by_item_id": {"type": ["integer", "null"], "minimum": 1},
                # Optional, never authorizing: it only lets the reservation
                # ledger attribute this decision to the caller's session.
                "session_id": {"type": ["string", "null"], "minLength": 1},
            },
            required=("item_id", "kind", "rationale", "evidence_digests"),
        ),
        _result_schema(
            (
                "repo_id",
                "item_id",
                "decision",
                "status",
                "resolution",
                "terminal_decision_id",
                "replayed",
            ),
            {
                "repo_id": {"type": "string"},
                "item_id": {"type": "integer", "minimum": 1},
                "decision": {"type": "object"},
                "status": {"enum": ["pending", "active", "done", "blocked"]},
                "resolution": {"enum": [*_RESOLUTIONS, None]},
                "terminal_decision_id": {"type": ["integer", "null"], "minimum": 1},
                "replayed": {"type": "boolean"},
            },
        ),
        "work:lifecycle",
        "write",
        "required",
    ),
    WorkOperationContract(
        "work.read.item-decisions",
        _object_schema(
            {"item_id": {"type": "integer", "minimum": 1}}, required=("item_id",)
        ),
        _result_schema(
            ("repo_id", "item_id", "status", "resolution", "terminal_decision_id", "decisions"),
            {
                "repo_id": {"type": "string"},
                "item_id": {"type": "integer", "minimum": 1},
                "status": {"enum": ["pending", "active", "done", "blocked"]},
                "resolution": {"enum": [*_RESOLUTIONS, None]},
                "terminal_decision_id": {"type": ["integer", "null"], "minimum": 1},
                "decisions": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.release",
        _object_schema(
            {
                "release_digest": _SHA256_HEX,
                "item_id": {"type": "integer", "minimum": 1},
            }
        ),
        _result_schema(
            ("repo_id", "release", "commits"),
            {
                "repo_id": {"type": "string"},
                "release": {"type": "object"},
                "commits": {"type": "array", "items": {"type": "object"}},
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.read.unbound",
        _object_schema(
            {
                "sprint_id": {"type": ["integer", "null"], "minimum": 1},
                "category": {"enum": [*_UNBOUND_CATEGORIES, None]},
                "limit": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": _unbound.MAX_LIMIT,
                },
            }
        ),
        _result_schema(
            ("repo_id", "sprint_id", "limit", "categories", "resolutions"),
            {
                "repo_id": {"type": "string"},
                "sprint_id": {"type": ["integer", "null"], "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
                "categories": {
                    "type": "object",
                    "properties": {
                        name: _UNBOUND_CATEGORY_RESULT for name in _UNBOUND_CATEGORIES
                    },
                    "additionalProperties": False,
                },
                "resolutions": {
                    "type": "object",
                    "required": list(_RESOLUTION_METRICS),
                    "properties": {
                        name: {"type": "integer", "minimum": 0}
                        for name in _RESOLUTION_METRICS
                    },
                    "additionalProperties": False,
                },
                # #2451: optional, alongside the categories above -- see
                # sprintctl.unbound and db.list_unmet_obligations.  Report
                # only: no transition or validator reads this key.
                "unmet_obligations": _UNMET_OBLIGATIONS_RESULT,
            },
        ),
        "work:read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.batch.apply",
        _BATCH_APPLY_INPUT,
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
                "active_reservations": {"type": "array", "items": {"type": "object"}},
                "active_unreserved_items": {"type": "array", "items": {"type": "object"}},
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
            ("contract_version", "project_id", "project", "ready_items", "repositories", "graph_ready", "dispatch_admissible", "dispatch_reason"),
            {
                "contract_version": {"const": "project-1"},
                "project_id": {"type": "string"},
                "project": {"type": "object"},
                "ready_items": {"type": "array", "items": {"type": "object"}},
                "repositories": {"type": "array", "items": {"type": "object"}},
                "graph_ready": {"type": "boolean"},
                "dispatch_admissible": {"enum": ["admissible", "inadmissible", "unknown"]},
                "dispatch_reason": {"type": "string"},
            },
        ),
        "work:project-read",
        "read",
        "not-allowed",
    ),
    WorkOperationContract(
        "work.project.next-work-explain",
        _object_schema({"sprint_id": {"type": ["integer", "null"], "minimum": 1}}),
        _result_schema(
            ("contract_version", "project_id", "project", "ready_items", "repositories", "graph_ready", "dispatch_admissible", "dispatch_reason", "explanation"),
            {"contract_version": {"const": "project-1"}, "project_id": {"type": "string"}, "project": {"type": "object"}, "ready_items": {"type": "array"}, "repositories": {"type": "array"}, "graph_ready": {"type": "boolean"}, "dispatch_admissible": {"enum": ["admissible", "inadmissible", "unknown"]}, "dispatch_reason": {"type": "string"}, "explanation": {"type": "object"}},
        ),
        "work:project-read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.reservations",
        _object_schema({"item_id": {"type": ["integer", "null"], "minimum": 1}, "active_only": {"type": "boolean", "default": True}}),
        _result_schema(("repo_id", "reservations"), {"repo_id": {"type": "string"}, "reservations": {"type": "array", "items": {"type": "object"}}}),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.read.reservation",
        _object_schema({"reservation_id": {"type": "integer", "minimum": 1}}, required=("reservation_id",)),
        _result_schema(("repo_id", "reservation"), {"repo_id": {"type": "string"}, "reservation": {"type": "object"}}),
        "work:read", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.reservation.reserve",
        _object_schema({"item_id": {"type": "integer", "minimum": 1}, "actor": {"type": "string", "minLength": 1}, "session_id": {"type": "string", "minLength": 1}, "role": {"enum": ["execution", "verification", "observation"]}, "correlation_ref": {"type": ["string", "null"]}, "interrupt_existing": {"type": "boolean", "default": False}, "expected_revision": {"type": ["string", "null"], "minLength": 1}, "acceptance_contract": {"type": "object"}}, required=("item_id", "actor", "session_id")),
        _result_schema(("repo_id", "reservation"), {"repo_id": {"type": "string"}, "reservation": {"type": "object"}}),
        "work:write", "write", "required",
    ),
    WorkOperationContract(
        "work.reservation.reserve-v1",
        _object_schema({"item_id": {"type": "integer", "minimum": 1}, "actor": {"type": "string", "minLength": 1}, "session_id": {"type": "string", "minLength": 1}, "role": {"enum": ["execution", "verification", "observation"]}, "correlation_ref": {"type": ["string", "null"]}, "interrupt_existing": {"type": "boolean", "default": False}, "expected_revision": {"type": ["string", "null"], "minLength": 1}, "acceptance_contract": {"type": "object"}}, required=("item_id", "actor", "session_id")),
        _result_schema(("repo_id", "reservation"), {"repo_id": {"type": "string"}, "reservation": {"type": "object"}}),
        "work:write", "write", "required",
    ),
    WorkOperationContract(
        "work.reservation.touch",
        _object_schema({"reservation_id": {"type": "integer", "minimum": 1}, "session_id": {"type": "string", "minLength": 1}, "correlation_ref": {"type": ["string", "null"]}}, required=("reservation_id", "session_id")),
        _result_schema(("repo_id", "reservation"), {"repo_id": {"type": "string"}, "reservation": {"type": "object"}}),
        "work:write", "write", "required",
    ),
    WorkOperationContract(
        "work.reservation.reassign",
        _object_schema({"reservation_id": {"type": "integer", "minimum": 1}, "actor": {"type": "string", "minLength": 1}, "session_id": {"type": "string", "minLength": 1}, "correlation_ref": {"type": ["string", "null"]}}, required=("reservation_id", "actor", "session_id")),
        _result_schema(("repo_id", "reservation"), {"repo_id": {"type": "string"}, "reservation": {"type": "object"}}),
        "work:write", "write", "required",
    ),
    WorkOperationContract(
        "work.reservation.release",
        _object_schema({"reservation_id": {"type": "integer", "minimum": 1}, "actor": {"type": ["string", "null"]}}, required=("reservation_id",)),
        _result_schema(("repo_id", "reservation"), {"repo_id": {"type": "string"}, "reservation": {"type": "object"}}),
        "work:write", "write", "required",
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


def _attempt_operation_contracts() -> tuple[WorkOperationContract, ...]:
    """Additive attempt descriptors; legacy operation bytes stay unchanged."""
    attempt_id = {"type": "string", "pattern": "^attempt_[0-9A-HJKMNP-TV-Z]{26}$"}
    branch = {"type": "string", "minLength": 1, "maxLength": 200}
    revision = {"type": "string", "pattern": _releases._RELEASE_REVISION_RE.pattern}
    common_target = {"branch": branch, "commit_sha": _EFFECT_COMMIT_SCHEMA}
    wire_target = {"anyOf": [
        _object_schema({**common_target, "operation": {"enum": ["push_branch"]}},
                       required=("operation", "branch", "commit_sha")),
        _object_schema({**common_target, "operation": {"enum": ["open_pull_request"]}, "base_branch": branch},
                       required=("operation", "branch", "commit_sha", "base_branch")),
    ]}
    owned_target_fields = {**common_target, "operation": {"enum": ["push_branch", "open_pull_request"]},
        "repository": {"type": "string"}, "base_commit": _EFFECT_COMMIT_SCHEMA,
        "title_sha256": _EFFECT_DIGEST_SCHEMA, "body_sha256": _EFFECT_DIGEST_SCHEMA, "base_branch": branch}
    owned_target = _object_schema(owned_target_fields,
        required=tuple(k for k in owned_target_fields if k != "base_branch"))
    authorization_fields = {"repo_id": {"type": "string"}, "attempt_id": attempt_id,
        "principal_id": {"type": "string", "minLength": 1}, "workspace_id": {"type": "string", "minLength": 1},
        "client_id": {"type": ["string", "null"]}, "grant_id": {"type": ["string", "null"]},
        "intent_id": _EFFECT_ID_SCHEMA, "intent_revision": _EFFECT_REVISION_SCHEMA,
        "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA, "work_item_id": {"type": "integer", "minimum": 1},
        "expected_revision": revision, "release_digest": _EFFECT_DIGEST_SCHEMA,
        "target": owned_target, "target_digest": _EFFECT_DIGEST_SCHEMA, "acceptance": _EFFECT_ACCEPTANCE_SCHEMA,
        "verification_binding": {"anyOf": [_EFFECT_VERIFICATION_BINDING_SCHEMA, {"type": "null"}]}}
    authorization = _object_schema(authorization_fields, required=tuple(authorization_fields))
    payload = {"anyOf": [
        _object_schema({"authorization": authorization}, required=("authorization",)),
        _object_schema({"target_digest": _EFFECT_DIGEST_SCHEMA}, required=("target_digest",)),
        _object_schema({"commit_sha": _EFFECT_COMMIT_SCHEMA, "pr_url": _EFFECT_PR_URL_SCHEMA},
                       required=("commit_sha", "pr_url")),
    ]}
    event_fields = {"repo_id": {"type": "string"}, "attempt_id": attempt_id,
        "event_seq": {"type": "integer", "minimum": 0, "maximum": 2}, "event_kind": {"enum": list(_attempt.EVENT_KINDS)},
        "authorization_digest": _EFFECT_DIGEST_SCHEMA, "previous_event_digest": {"type": ["string", "null"]},
        "event_digest": _EFFECT_DIGEST_SCHEMA, "created_at": {"type": "string"}, "payload": payload}
    event = _object_schema(event_fields, required=tuple(event_fields))
    consume_fields = {"attempt_id": attempt_id, "authorization_digest": _EFFECT_DIGEST_SCHEMA,
                      "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA}
    receipt_fields = {"repo_id": {"type": "string"}, "attempt_id": attempt_id,
        "authorization_digest": _EFFECT_DIGEST_SCHEMA, "receipt": event, "delivery": {"enum": ["fresh", "replay"]}}
    open_fields = {"intent_id": _EFFECT_ID_SCHEMA, "revision": _EFFECT_REVISION_SCHEMA,
        "canonical_intent_digest": _EFFECT_DIGEST_SCHEMA, "expected_revision": revision,
        "release_digest": _EFFECT_DIGEST_SCHEMA, "target": wire_target, "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA}
    open_result = {"repo_id": {"type": "string"}, "authorization": authorization,
        "authorization_digest": _EFFECT_DIGEST_SCHEMA, "receipt": event, "delivery": {"enum": ["fresh", "replay"]}}
    get_result = {"repo_id": {"type": "string"}, "authorization": authorization,
        "authorization_digest": _EFFECT_DIGEST_SCHEMA, "state": {"enum": ["accepted", "redeemed", "sealed_unused"]},
        "events": {"type": "array", "minItems": 1, "maxItems": 3, "items": event}}
    definitions = (
        (_attempt.OPERATION_OPEN, open_fields, open_result, "write"),
        (_attempt.OPERATION_REDEEM, consume_fields, {**receipt_fields, "dispatch_permitted": {"type": "boolean"}}, "write"),
        (_attempt.OPERATION_SEAL_UNUSED, consume_fields, receipt_fields, "write"),
        (_attempt.OPERATION_REPORT, {**consume_fields, "commit_sha": _EFFECT_COMMIT_SCHEMA,
                                   "pr_url": _EFFECT_PR_URL_SCHEMA}, receipt_fields, "write"),
        (_attempt.OPERATION_GET, {"attempt_id": attempt_id}, get_result, "read"),
    )
    return tuple(WorkOperationContract(name, _object_schema(inputs, required=tuple(inputs)),
        _object_schema(outputs, required=tuple(outputs)), _attempt.AUTHORITY, semantics, "not-allowed")
        for name, inputs, outputs, semantics in definitions)


WORK_OPERATION_CONTRACTS += _attempt_operation_contracts()


# --------------------------------------------------------------------------
# work.agent.* / work.message.* (schema 25): the agent directory and durable
# inbox, vuoro contract v1 (docs/architecture/agent-directory-and-inbox.md;
# schemas agent-{declaration,message,message-receipt,directory-entry}-v1).
# Writes need work:evidence and reads work:read (vuoro M0 baseline 2.5).
# principal_id, workspace_id and repo_id are the caller's, never arguments.
# Nested records are validated by the owner (sprintctl.agent_inbox) against
# the contract's schemas; the catalog fixes the argument names.
# --------------------------------------------------------------------------
def _agent_inbox_operation_contracts() -> tuple[WorkOperationContract, ...]:
    instance_id = {"type": "string", "pattern": "^ins_[0-9A-HJKMNP-TV-Z]{26}$"}
    message_id = {"type": "string", "pattern": "^msg_[0-9A-HJKMNP-TV-Z]{26}$"}
    text = {"type": "string", "maxLength": 2048}
    timestamp = {"type": "string"}
    record = {"type": "object"}
    reason = _object_schema({"code": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}, "text": text},
                            required=("code", "text"))
    replayed = {"replayed": {"type": "boolean"}}
    receipt_required = ("schema", "message_id", "kind", "principal_id", "instance_id", "recorded_at")
    receipt = {"type": "object", "required": list(receipt_required)}
    # An agent-message-receipt/v1 plus "replayed"; kind fields stay open.
    receipt_result = _object_schema(
        {"schema": {"const": "agent-message-receipt/v1"}, "message_id": message_id,
         "kind": {"type": "string"}, "principal_id": {"type": "string"},
         "instance_id": {"anyOf": [instance_id, {"type": "null"}]}, "recorded_at": timestamp, **replayed},
        required=(*receipt_required, "replayed"), additional_properties=True)
    stamps = {
        "message_id": message_id, "sender_principal_id": {"type": "string"},
        "workspace_id": {"type": "string"}, "repo_id": {"type": "string"},
        "persisted_at": timestamp, "envelope_digest": _SHA256_HEX,
        "inbox_seq": {"type": "integer", "minimum": 1},
    }
    stamped_message = {"type": "object", "required": [*stamps, "receipts"]}
    definitions = (
        (_inbox.OPERATION_REGISTER, "work:evidence", "write",
         _object_schema({"instance_id": instance_id, "declaration": record,
                         "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA},
                        required=("instance_id", "declaration", "idempotency_key")),
         _object_schema({"instance_id": instance_id, "declaration_revision": {"type": "integer", "minimum": 1},
                         **replayed}, required=("instance_id", "declaration_revision", "replayed"))),
        (_inbox.OPERATION_OBSERVE, "work:evidence", "write",
         _object_schema({"instance_id": instance_id,
                         "fact": {"enum": ["observed", "reachable", "executing", "attended"]},
                         "channel": {"type": "string", "pattern": "^[a-z][a-z0-9.-]{0,63}$"},
                         "run_id": _RUN_ID_SCHEMA,
                         "valid_for_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                         "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA},
                        required=("instance_id", "fact", "valid_for_seconds", "idempotency_key")),
         _object_schema({"instance_id": instance_id, "fact": {"type": "string"}, "observed_at": timestamp,
                         "valid_until": timestamp, "source": {"const": "self-reported"}, **replayed},
                        required=("instance_id", "fact", "observed_at", "valid_until", "source", "replayed"))),
        (_inbox.OPERATION_RETIRE, "work:evidence", "write",
         _object_schema({"instance_id": instance_id, "reason": {"type": "string", "minLength": 1, "maxLength": 2048},
                         "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA},
                        required=("instance_id", "reason", "idempotency_key")),
         _object_schema({"instance_id": instance_id, "retired_at": timestamp, **replayed},
                        required=("instance_id", "retired_at", "replayed"))),
        (_inbox.OPERATION_LIST, "work:read", "read",
         _object_schema({"include_retired": {"type": "boolean", "default": True}}),
         _object_schema({"entries": {"type": "array", "items": record}}, required=("entries",))),
        (_inbox.OPERATION_DESCRIBE, "work:read", "read",
         _object_schema({"principal_id": {"type": "string", "minLength": 1, "maxLength": 512}},
                        required=("principal_id",)),
         _object_schema({"schema": {"const": "agent-directory-entry/v1"}, "principal_id": {"type": "string"},
                         "evaluated_at": timestamp, "instances": {"type": "array", "items": record}},
                        required=("schema", "principal_id", "evaluated_at", "instances"))),
        (_inbox.OPERATION_SEND, "work:evidence", "write",
         _object_schema({"message": record}, required=("message",)),
         _object_schema({**stamps, **replayed}, required=(*stamps, "replayed"))),
        (_inbox.OPERATION_READ, "work:read", "read",
         _object_schema({"after_seq": {"type": "integer", "minimum": 0},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                         "instance_id": instance_id}, required=("after_seq", "limit")),
         _object_schema({"messages": {"type": "array", "items": stamped_message},
                         "next_after_seq": {"type": "integer", "minimum": 0}},
                        required=("messages", "next_after_seq"))),
        (_inbox.OPERATION_STATUS, "work:read", "read",
         _object_schema({"message_id": message_id}, required=("message_id",)),
         _object_schema({"message": stamped_message, "receipts": {"type": "array", "items": receipt}},
                        required=("message", "receipts"))),
        (_inbox.OPERATION_RECEIPT, "work:evidence", "write",
         _object_schema({"message_id": message_id, "instance_id": instance_id,
                         "kind": {"enum": list(_inbox.RECEIPT_KINDS)}, "reason": reason, "run_id": _RUN_ID_SCHEMA,
                         "detail": text, "outcome": {"enum": list(_inbox.OUTCOMES)},
                         "evidence_refs": {"type": "array", "maxItems": 32, "items": record},
                         "summary": text},
                        required=("message_id", "instance_id", "kind")),
         receipt_result),
        (_inbox.OPERATION_CANCEL, "work:evidence", "write",
         _object_schema({"message_id": message_id, "reason": text}, required=("message_id", "reason")),
         receipt_result),
    )
    return tuple(
        WorkOperationContract(name, input_schema, result_schema, authority, semantics, "not-allowed")
        for name, authority, semantics, input_schema, result_schema in definitions
    )


WORK_OPERATION_CONTRACTS += _agent_inbox_operation_contracts()


# --------------------------------------------------------------------------
# work.request.* (schema 28): durable operator-request/v1 lifecycle.  These
# records are independent of work status, Agent Inbox v1, and effect approvals.
# --------------------------------------------------------------------------
def _operator_request_operation_contracts() -> tuple[WorkOperationContract, ...]:
    request_id = {"type": "string", "pattern": r"^req_[0-9A-HJKMNP-TV-Z]{26}$"}
    idem_key = {"type": "string", "pattern": r"^[A-Za-z0-9._:-]{8,128}$"}
    revision = {"type": "integer", "minimum": 1, "maximum": 2147483647}
    receipt_request_id = {**request_id, "minLength": 30, "maxLength": 30}
    receipt_work_revision = {"type": "string", "minLength": 1, "maxLength": 512,
                             "pattern": r"^(?![\s\S]*\u0000)(?=[\s\S]*\S)[\s\S]+(?![\s\S])"}
    packet = _object_schema(
        {
            "schema": {"const": "operator-request/v1"},
            "kind": {"enum": ["perform", "authorize", "decide", "provide-information"]},
            "required_outcome": {"type": "string", "minLength": 1, "maxLength": 2048},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2048},
            "execution": _object_schema(
                {
                    "host": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "directory": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "intended_identity": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "target": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "inputs": {"type": "array", "maxItems": 16, "items": _object_schema(
                        {"name": {"type": "string", "minLength": 1, "maxLength": 256},
                         "acquire_from": {"type": "string", "minLength": 1, "maxLength": 1024}},
                        required=("name", "acquire_from"))},
                }, required=("host", "directory", "intended_identity", "target", "inputs")),
            "procedure": _object_schema(
                {
                    "version": {"type": "string", "minLength": 1, "maxLength": 128},
                    "steps": {"type": "array", "minItems": 1, "maxItems": 64, "items": _object_schema(
                        {
                            "step_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "instruction": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "expected_result": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "preconditions": {"type": "array", "maxItems": 32,
                                              "items": {"type": "string", "maxLength": 1024}},
                            "verification": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "partial_progress": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "recovery": {"type": "string", "minLength": 1, "maxLength": 4096},
                        }, required=("step_id", "instruction", "expected_result", "preconditions",
                                     "verification", "partial_progress", "recovery"))},
                }, required=("version", "steps")),
            "blocker": _object_schema(
                {
                    "condition": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "checked_at": {"type": "string", "format": "date-time"},
                    "fresh_until": {"type": "string", "format": "date-time"},
                    "source_revisions": {"type": "array", "maxItems": 32, "items": _object_schema(
                        {"source": {"type": "string", "minLength": 1, "maxLength": 1024},
                         "revision": {"type": "string", "minLength": 1, "maxLength": 512}},
                        required=("source", "revision"))},
                }, required=("condition", "checked_at", "fresh_until", "source_revisions")),
        }, required=("schema", "kind", "required_outcome", "reason", "execution", "procedure", "blocker"))
    dependant = _object_schema(
        {"work_item_id": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
         "session_id": {"type": "string", "minLength": 1, "maxLength": 256},
         "runtime_id": {"type": "string", "minLength": 1, "maxLength": 256}},
        required=("work_item_id", "session_id"))
    dependant_record = _object_schema(
        {**dependant["properties"], "runtime_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
         "work_revision": {"type": "string"},
         "current_work_revision": {"anyOf": [{"type": "string"}, {"type": "null"}]},
         "source_changed": {"type": "boolean"}, "state": {"enum": ["active", "withdrawn"]},
         "attached_at": {"type": "string", "format": "date-time"},
         "withdrawn_at": {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]}},
        required=("work_item_id", "session_id", "runtime_id", "work_revision", "current_work_revision",
                  "source_changed", "state", "attached_at", "withdrawn_at"), additional_properties=False)
    snapshot = _object_schema(
        {"request_id": request_id, "revision": revision,
         "state": {"enum": ["open", "reported-performed", "verifying", "resolved", "declined", "withdrawn", "superseded"]},
         "freshness": {"enum": ["current", "needs-refresh", "unknown"]}, "packet": packet,
         "dependants": {"type": "array", "items": dependant_record},
         "created_at": {"type": "string", "format": "date-time"},
         "updated_at": {"type": "string", "format": "date-time"}},
        required=("request_id", "revision", "state", "freshness", "packet", "dependants", "created_at", "updated_at"))
    mutation = _object_schema({"request_id": request_id, "revision": revision, "replayed": {"type": "boolean"}},
                              required=("request_id", "revision", "replayed"))
    event = _object_schema(
        {"request_id": request_id, "revision": revision, "action": {"type": "string"},
         "actor": {"type": "string"}, "details": {"type": "object"},
         "recorded_at": {"type": "string", "format": "date-time"}},
        required=("request_id", "revision", "action", "actor", "details", "recorded_at"))
    continuation = _object_schema(
        {"schema": {"const": "operator-request-continuation-ready/v1"},
         "owner": _object_schema(
             {"repo_id": {"type": "string", "minLength": 1},
              "read_operation": {"const": "work.request.continuation-v1"}},
             required=("repo_id", "read_operation")),
         "request_id": request_id, "request_revision": revision,
         "resolution": _object_schema(
             {"event_revision": revision, "basis": {"const": "human-attestation"},
              "event_recorded_at": {"type": "string", "format": "date-time"}},
             required=("event_revision", "basis", "event_recorded_at")),
         "dependants": {"type": "array", "maxItems": 128, "items": _object_schema(
             {"work_item_id": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
              "session_id": {"type": "string", "minLength": 1, "maxLength": 256},
              "work_revision": {"type": "string", "minLength": 1},
              "current_work_revision": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
              "state": {"enum": ["continuation-ready", "needs-refresh"]}},
             required=("work_item_id", "session_id", "work_revision", "current_work_revision", "state"))}},
        required=("schema", "owner", "request_id", "request_revision", "resolution", "dependants"))
    return (
        WorkOperationContract("work.request.continuation-receipt-v1",
            _object_schema({"request_id": receipt_request_id, "resolution_event_revision": revision,
                            "work_item_id": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
                            "expected_work_revision": receipt_work_revision,
                            "idempotency_key": idem_key},
                           required=("request_id", "resolution_event_revision", "work_item_id",
                                     "expected_work_revision", "idempotency_key")),
            _object_schema({"request_id": receipt_request_id, "resolution_event_revision": revision,
                            "work_item_id": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
                            "work_revision": receipt_work_revision,
                            "material_revision": revision,
                            "packet_digest": {"type": "string", "pattern": r"^[0-9a-f]{64}(?![\s\S])"},
                            "sessions": {"type": "array", "minItems": 1,
                                         "items": {"type": "string", "minLength": 1, "maxLength": 256}},
                            "recorded_at": {"type": "string", "format": "date-time"}, "replayed": {"type": "boolean"}},
                           required=("request_id", "resolution_event_revision", "work_item_id", "work_revision",
                                     "material_revision", "packet_digest", "sessions", "recorded_at", "replayed")),
            "work:write", "write", "not-allowed", required_remote_schema_version=30),
        WorkOperationContract("work.request.continuation-v1",
            _object_schema({"request_id": request_id}, required=("request_id",)),
            continuation, "work:read", "read", "not-allowed", required_remote_schema_version=29),
        WorkOperationContract("work.request.publish-v1",
            _object_schema({"request_id": request_id, "packet": packet,
                            "dependants": {"type": "array", "minItems": 1, "maxItems": 128, "items": dependant},
                            "idempotency_key": idem_key},
                           required=("request_id", "packet", "dependants", "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.get-v1",
            _object_schema({"request_id": request_id}, required=("request_id",)),
            snapshot, "work:read", "read", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.list-v1",
            _object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100},
                            "after_request_id": request_id,
                            "state": {"enum": ["open", "reported-performed", "verifying", "resolved", "declined", "withdrawn", "superseded"]}},
                           required=("limit",)),
            _object_schema({"requests": {"type": "array", "items": snapshot},
                            "next_after_request_id": {"anyOf": [request_id, {"type": "null"}]}},
                           required=("requests", "next_after_request_id")),
            "work:read", "read", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.revise-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "packet": packet, "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "packet", "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.dependent-add-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "dependant": dependant, "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "dependant", "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.dependent-withdraw-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "work_item_id": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
                            "session_id": {"type": "string", "minLength": 1, "maxLength": 256},
                            "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "work_item_id", "session_id", "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.events-v1",
            _object_schema({"request_id": request_id, "after_revision": {"type": "integer", "minimum": 0,
                                                                            "maximum": 2147483647},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                           required=("request_id", "after_revision", "limit")),
            _object_schema({"request_id": request_id, "events": {"type": "array", "items": event},
                            "next_after_revision": {"type": "integer", "minimum": 0, "maximum": 2147483647}},
                           required=("request_id", "events", "next_after_revision")),
            "work:read", "read", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.progress-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "work_item_id": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
                            "session_id": {"type": "string", "minLength": 1, "maxLength": 256},
                            "step_id": {"type": "string", "minLength": 1, "maxLength": 128},
                            "status": {"enum": ["in-progress", "completed", "blocked", "uncertain"]},
                            "note": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "work_item_id", "session_id", "step_id",
                                     "status", "note", "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.report-performed-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "outcome": {"enum": ["performed", "partial", "uncertain"]},
                            "note": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "outcome", "note", "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.attest-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "statement": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "performed_at": {"type": "string", "format": "date-time"},
                            "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "statement", "performed_at", "idempotency_key")),
            mutation, "work:approve", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.decline-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "reason": {"type": "string", "minLength": 1, "maxLength": 2048},
                            "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "reason", "idempotency_key")),
            mutation, "work:approve", "write", "not-allowed", required_remote_schema_version=28),
        WorkOperationContract("work.request.supersede-v1",
            _object_schema({"request_id": request_id, "expected_revision": revision,
                            "replacement_request_id": request_id, "packet": packet,
                            "idempotency_key": idem_key},
                           required=("request_id", "expected_revision", "replacement_request_id", "packet",
                                     "idempotency_key")),
            mutation, "work:evidence", "write", "not-allowed", required_remote_schema_version=28),
    )


WORK_OPERATION_CONTRACTS += _operator_request_operation_contracts()


# --------------------------------------------------------------------------
# Run resolution facts and structured approvals (schema 26).
#
# work.run.resolve-v2 is resolve-v1's binding plus the owner-side facts an
# effect-boundary authority check needs: when the binding was issued and
# whether a successor run has superseded it.  resolve-v1's descriptor is
# byte-frozen, so the facts are a new version, not added result fields.  A
# run has no recorded close, so state is only active | superseded.
#
# work.approval.record-v1 / get-v1 store and read one immutable operator
# approval of a named transition for one run, until expires_at.  Recording
# needs work:approve, which the default agent grant does not carry; the
# approver, repository and workspace are the caller's, never arguments.
# --------------------------------------------------------------------------
def _run_resolution_operation_contracts() -> tuple[WorkOperationContract, ...]:
    timestamp = {"type": "string", "minLength": 1}
    approval_id = {"type": "string", "pattern": _approval.APPROVAL_ID_PATTERN}
    approval_properties = {
        "approval_id": approval_id,
        "repo_id": {"type": "string"},
        "workspace_id": {"type": "string", "minLength": 1},
        "run_id": _RUN_ID_SCHEMA,
        "transition": {"type": "string", "pattern": _approval.TRANSITION_PATTERN},
        "rationale": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "approver": _object_schema(
            {
                "principal_id": {"type": "string", "minLength": 1},
                "client_id": _NULLABLE_BINDING_ID_SCHEMA,
                "grant_id": _NULLABLE_BINDING_ID_SCHEMA,
            },
            required=("principal_id", "client_id", "grant_id"),
        ),
        "recorded_at": timestamp,
        "expires_at": timestamp,
    }
    return (
        WorkOperationContract(
            "work.run.resolve-v2",
            _object_schema({"run_id": _RUN_ID_SCHEMA}, required=("run_id",)),
            _result_schema(
                (
                    "repo_id", "run_id", "principal_id", "workspace_id", "client_id",
                    "grant_id", "issued_at", "superseded_by_run_id", "state",
                ),
                {
                    "repo_id": {"type": "string"},
                    "run_id": _RUN_ID_SCHEMA,
                    "principal_id": {"type": "string", "minLength": 1},
                    "workspace_id": {"type": "string", "minLength": 1},
                    "client_id": _NULLABLE_BINDING_ID_SCHEMA,
                    "grant_id": _NULLABLE_BINDING_ID_SCHEMA,
                    "issued_at": timestamp,
                    "superseded_by_run_id": {"anyOf": [_RUN_ID_SCHEMA, {"type": "null"}]},
                    "state": {"enum": ["active", "superseded"]},
                },
            ),
            # As resolve-v1: only the caller's own run resolves (run-not-found
            # otherwise), so no bucket authority gates it.
            None,
            "read",
            "not-allowed",
        ),
        WorkOperationContract(
            _approval.OPERATION_RECORD,
            _object_schema(
                {
                    "transition": {"type": "string", "pattern": _approval.TRANSITION_PATTERN},
                    "run_id": _RUN_ID_SCHEMA,
                    "expires_at": {"type": "string", "minLength": 1},
                    "rationale": {"type": "string", "maxLength": _approval.RATIONALE_MAX_BYTES},
                    "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
                },
                required=("transition", "run_id", "expires_at", "idempotency_key"),
            ),
            _result_schema(
                (*approval_properties, "replayed"),
                {**approval_properties, "replayed": {"type": "boolean"}},
            ),
            _approval.AUTHORITY_RECORD,
            "write",
            "not-allowed",
        ),
        WorkOperationContract(
            _approval.OPERATION_GET,
            _object_schema({"approval_id": approval_id}, required=("approval_id",)),
            _result_schema(
                (*approval_properties, "evaluated_at", "expired"),
                {**approval_properties, "evaluated_at": timestamp, "expired": {"type": "boolean"}},
            ),
            _approval.AUTHORITY_GET,
            "read",
            "not-allowed",
        ),
    )


WORK_OPERATION_CONTRACTS += _run_resolution_operation_contracts()


def _run_delegation_operation_contracts() -> tuple[WorkOperationContract, ...]:
    delegation_id = {"type": "string", "pattern": _delegation.DELEGATION_ID_PATTERN}
    operations = {
        "type": "array",
        "minItems": 1,
        "uniqueItems": True,
        "items": {"enum": sorted(_delegation.DELEGATED_OPERATIONS)},
    }
    record = {
        "delegation_id": delegation_id,
        "run_id": _RUN_ID_SCHEMA,
        "creator_instance_id": {"type": "string", "pattern": _inbox.INSTANCE_ID_RE.pattern},
        "operations": operations,
        "created_at": {"type": "string", "minLength": 1},
        "expires_at": {"type": "string", "minLength": 1},
        "revoked_at": {"type": ["string", "null"]},
        "revoked_reason": {"type": ["string", "null"]},
        "state": {"enum": ["live", "expired", "revoked"]},
    }
    return (
        WorkOperationContract(
            _delegation.OP_DELEGATE,
            _object_schema(
                {
                    "run_id": _RUN_ID_SCHEMA,
                    "creator_instance_id": {"type": "string", "pattern": _inbox.INSTANCE_ID_RE.pattern},
                    "operations": operations,
                    "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": _delegation.MAX_TTL_SECONDS},
                    "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
                },
                required=("run_id", "creator_instance_id", "operations", "ttl_seconds", "idempotency_key"),
            ),
            _result_schema(
                (*record, "token", "replayed"),
                {**record, "token": {"type": ["string", "null"], "pattern": "^vrd_[A-Z2-7]{52}$"},
                 "replayed": {"type": "boolean"}},
            ),
            "work:evidence", "write", "required",
        ),
        WorkOperationContract(
            _delegation.OP_REVOKE,
            _object_schema(
                {
                    "delegation_id": delegation_id,
                    "reason": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"},
                    "idempotency_key": _IDEMPOTENCY_KEY_SCHEMA,
                },
                required=("delegation_id", "reason", "idempotency_key"),
            ),
            _result_schema(
                (*record, "already_revoked", "replayed"),
                {**record, "already_revoked": {"type": "boolean"}, "replayed": {"type": "boolean"}},
            ),
            "work:evidence", "write", "required",
        ),
        WorkOperationContract(
            _delegation.OP_GET,
            _object_schema({"delegation_id": delegation_id}, required=("delegation_id",)),
            _result_schema(
                (*record, "evaluated_at"),
                {**record, "evaluated_at": {"type": "string", "minLength": 1}},
            ),
            "work:read", "read", "not-allowed",
        ),
    )


WORK_OPERATION_CONTRACTS += _run_delegation_operation_contracts()

_PARK_RATE_SCHEMA = _object_schema({
    "observed_at": {"type": "string"}, "status": {"const": "rejected"},
    "limit_scope": {"enum": ["plan", "family", "unknown"]},
    "resets_at": {"type": ["string", "null"]},
    "reset_source": {"enum": ["harness-structured", "unparsed-local-string", "absent"]},
    "http_status": {"type": ["integer", "null"], "minimum": 100, "maximum": 599},
    "overage": {"type": ["boolean", "null"]},
}, required=("observed_at", "status", "limit_scope", "resets_at", "reset_source", "http_status", "overage"))
_PARK_CONTEXT_FIELDS = {
    "event_id": {"type": "integer", "minimum": 1}, "reason_ref": {"type": ["string", "null"]},
    "parked_at": {"type": "string"}, "report_id": {"type": ["string", "null"]},
    "lease_id": {"type": ["string", "null"]}, "run_id": {"type": ["string", "null"]},
    "model_id": {"type": ["string", "null"]},
    "rate_limit": {"anyOf": [_PARK_RATE_SCHEMA, {"type": "null"}]},
}
_PARK_PREVIOUS_FIELDS = {**_PARK_CONTEXT_FIELDS, "release_event_id": {"type": "integer", "minimum": 1},
                         "released_at": {"type": "string"}}

WORK_OPERATION_CONTRACTS += (
    WorkOperationContract(
        "work.evidence.entry-v1",
        _object_schema({
            "run_id": _RUN_ID_SCHEMA,
            "chain_seq": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
        }, required=("run_id", "chain_seq")),
        _result_schema(("repo_id", "run_id", "item"), {
            "repo_id": {"type": "string"}, "run_id": _RUN_ID_SCHEMA,
            "item": {"anyOf": [_EVIDENCE_ITEM_SCHEMA, {"type": "null"}]},
        }),
        "work:evidence", "read", "not-allowed",
    ),
    WorkOperationContract(
        "work.lease.parking-context-v1",
        _object_schema({"item_id": {"type": "integer", "minimum": 1}, "lease_id": _LEASE_ID_SCHEMA},
                       required=("item_id",)),
        _result_schema(("repo_id", "item_id", "current", "previous", "lease_run", "evaluated_at"), {
            "repo_id": {"type": "string"}, "item_id": {"type": "integer"},
            "lease_run": {"anyOf": [_object_schema({"run_id": _RUN_ID_SCHEMA, "model_id": {"type": "string"}},
                                                       required=("run_id", "model_id")), {"type": "null"}]},
            "current": {"anyOf": [_object_schema(_PARK_CONTEXT_FIELDS, required=tuple(_PARK_CONTEXT_FIELDS)), {"type": "null"}]},
            "previous": {"anyOf": [_object_schema(_PARK_PREVIOUS_FIELDS, required=tuple(_PARK_PREVIOUS_FIELDS)), {"type": "null"}]},
            "evaluated_at": {"type": "string"},
        }),
        "work:read", "read", "not-allowed",
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
        "legacy": "sprintctl item status / sprint status",
        "operation": "work.lifecycle.arbitrate",
    },
    {"legacy": "sprintctl authority sync", "operation": "work.batch.apply"},
    {"legacy": "sprintctl event observation add", "operation": "work.evidence.ingest"},
    {"legacy": "sprintctl item note", "operation": "work.item.note"},
    {"legacy": "sprintctl item edit", "operation": "work.item.edit"},
    {"legacy": "sprintctl item priority", "operation": "work.item.priority"},
    {"legacy": "sprintctl reservation reserve", "operation": "work.reservation.reserve"},
    {"legacy": "sprintctl reservation touch", "operation": "work.reservation.touch"},
    {"legacy": "sprintctl reservation reassign", "operation": "work.reservation.reassign"},
    {"legacy": "sprintctl reservation release", "operation": "work.reservation.release"},
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


def _operation_rejected(error_type: type[Exception], error: ApplicationRejection) -> Exception:
    """The service's rejection for ``error``, with its structured details
    (e.g. ``claim-superseded``'s generations, agentops#2540) when the
    service's error type carries them.  Released vuoro-service (0.1.77)
    does not; there the details stay readable in the message instead of
    failing the call.  Only an explicit ``details`` parameter counts: a
    type taking ``**kwargs`` gets none, conservatively."""
    if error.details is not None and "details" in inspect.signature(error_type.__init__).parameters:
        return error_type(
            error.code, error.message, http_status=error.http_status, details=dict(error.details),
        )
    return error_type(error.code, error.message, http_status=error.http_status)


def catalog_operation_specs(
    *, resource_schema_available: bool, remote_schema_version: int | None = None
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
            deprecation=contract.deprecation,
        )
        for contract in WORK_OPERATION_CONTRACTS
        if resource_schema_available or contract.name not in _RESOURCE_OPERATIONS
        if remote_schema_version is None or contract.required_remote_schema_version <= remote_schema_version
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
        resource_schema_available=resource_schema_available,
        remote_schema_version=getattr(application.store, "remote_schema_version", None) or 0,
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
                raise _operation_rejected(OperationRejectedError, error) from error

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
    "PUBLIC_WORK_NEVER_EMIT_FIELDS",
    "PUBLIC_WORK_STATUSES",
    "PUBLIC_WORK_TITLE_MAX_LENGTH",
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
