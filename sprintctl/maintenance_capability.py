"""Exact-plan-bound maintenance capability lifecycle.

This module owns no transport or cluster operation.  It validates a reviewed
``maintenance-envelope/v1`` and records fenced, idempotent lifecycle effects.
Recovery observations and requested commands are audit input only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID


CONTRACT_ID = "maintenance-envelope/v1"
TOP_FIELDS = frozenset({"contract_id", "envelope_id", "plan_ref", "issued_at", "window", "operator", "repositories", "command_registry_ref", "command_registry", "operations", "jit_fields", "jit_bindings", "start_gate", "steps", "abort", "recovery_policy", "audit_reconciliation"})
JIT_NAMES = frozenset({"backup_name", "backup_uid", "drain_boundary_utc"})
JIT_SOURCES = {"backup_name": "backup-observation", "backup_uid": "backup-observation", "drain_boundary_utc": "clock-observation"}
ABORT_FORBIDDEN = frozenset({"delete-migration-ledger", "edit-released-migration", "unreviewed-commit", "recovery-request-authority"})
RECOVERY_FORBIDDEN = frozenset({"grant", "claim", "approve", "publish", "reconcile", "advance", "bind-jit"})
AUDIT_RECEIPTS = frozenset({"command", "effect", "review", "publication", "jit-binding", "start-gate", "abort", "reconciliation"})
AUDIT_OUTCOMES = frozenset({"accepted", "rejected", "duplicate", "expired", "aborted", "incomplete"})
AUDIT_REDACT = frozenset({"credentials", "claim-tokens", "capability-secrets"})
CAPABILITY_ID = re.compile(r"^mcap:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA256_REF = re.compile(r"^(?:artifact:)?sha256:[0-9a-f]{64}$")
ARTIFACT_REF = re.compile(r"^artifact:sha256:[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLACEHOLDER = re.compile(r"(?:REPLACE_AT_ACTIVATION|\$\{|<[^>]+>)")
SENSITIVE = re.compile(r"(?:claim[_-]?token|credential|password|api[_-]?key|access[_-]?token)\s*[:=]", re.I)
PHASE_ORDER = {"pre-migration": 0, "migration": 1, "post-migration": 2}
TERMINAL_STATES = frozenset({"reconciled", "aborted", "revoked", "expired"})
TRANSITIONS = {
    "attest": {"prepared": "attested"},
    "activate": {"attested": "active"},
    "observe": {"active": "observing", "observing": "observing"},
    "reconcile": {"active": "reconciled", "observing": "reconciled"},
    "abort": {"prepared": "aborted", "attested": "aborted", "active": "aborted", "observing": "aborted"},
    "revoke": {"prepared": "revoked", "attested": "revoked", "active": "revoked", "observing": "revoked"},
}
RECOVERY_KINDS = frozenset({"observation", "requested-command"})


class MaintenanceCapabilityError(ValueError):
    """A request failed closed without an authority effect."""


class StaleCapabilityRevision(MaintenanceCapabilityError):
    """The request was based on a stale capability revision."""


@dataclass(frozen=True, slots=True)
class FrozenEnvelope:
    envelope_id: str
    digest: str
    plan_ref: str
    operator: str
    not_before: datetime
    expires_at: datetime
    steps: tuple[Mapping[str, Any], ...]
    commands: Mapping[str, tuple[str, ...]]
    jit_fields: Mapping[str, Mapping[str, Any]]
    jit_bindings: Mapping[str, Mapping[str, Any]]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise MaintenanceCapabilityError(f"{field} must be an RFC3339 timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaintenanceCapabilityError(f"{field} must be an RFC3339 timestamp") from exc
    if result.tzinfo is None:
        raise MaintenanceCapabilityError(f"{field} must include an offset")
    return result.astimezone(timezone.utc)


def _exact_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_REF.fullmatch(value):
        raise MaintenanceCapabilityError(f"{field} must be an immutable sha256 reference")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or PLACEHOLDER.search(value) or value in {"HEAD", "main", "latest"} or SENSITIVE.search(value):
        raise MaintenanceCapabilityError(f"{field} must be nonblank, exact, credential-free text")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _text(value, field)
    if not IDENTIFIER.fullmatch(value):
        raise MaintenanceCapabilityError(f"{field} must be an identifier")
    return value


def _sorted_unique(values: Any, field: str, *, non_empty: bool = False) -> list[str]:
    if not isinstance(values, list) or (non_empty and not values) or any(not isinstance(value, str) for value in values) or values != sorted(set(values)):
        raise MaintenanceCapabilityError(f"{field} must be a sorted unique array")
    return values


def _relative_path(value: Any, field: str) -> str:
    value = _text(value, field)
    parsed = PurePosixPath(value)
    if value.startswith("/") or "//" in value or any(part in {"", ".", ".."} for part in parsed.parts):
        raise MaintenanceCapabilityError(f"{field} must be normalized, relative, and traversal-free")
    return value.rstrip("/")


def _repository_url(value: Any, field: str) -> str:
    value = _text(value, field)
    if value.startswith("git@"):
        if not re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+(?:\.git)?", value):
            raise MaintenanceCapabilityError(f"{field} must be a credential-free Git URL")
        return value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MaintenanceCapabilityError(f"{field} must be a credential-free Git URL")
    return value


def _immutable_ref(value: Any, field: str, kinds: set[str]) -> Mapping[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "source", "revision"}:
        raise MaintenanceCapabilityError(f"{field} must be an exact immutable reference")
    if value["kind"] not in kinds:
        raise MaintenanceCapabilityError(f"{field} has a disallowed immutable-reference kind")
    _text(value["source"], f"{field}.source")
    revision = value["revision"]
    if value["kind"] == "sprint-event":
        valid = isinstance(revision, str) and re.fullmatch(r"event:[1-9][0-9]*", revision)
    else:
        valid = isinstance(revision, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", revision)
    if not valid:
        raise MaintenanceCapabilityError(f"{field} has a mutable or malformed revision")
    return value


def freeze_envelope(envelope: Any) -> FrozenEnvelope:
    """Validate the authority-bearing subset and return its canonical identity."""
    if not isinstance(envelope, dict) or envelope.get("contract_id") != CONTRACT_ID:
        raise MaintenanceCapabilityError(f"envelope must use {CONTRACT_ID}")
    if set(envelope) != TOP_FIELDS:
        raise MaintenanceCapabilityError(f"envelope fields must exactly match {sorted(TOP_FIELDS)}")
    envelope_id = _identifier(envelope["envelope_id"], "envelope_id")
    plan_ref = envelope["plan_ref"]
    if not isinstance(plan_ref, str) or not ARTIFACT_REF.fullmatch(plan_ref):
        raise MaintenanceCapabilityError("plan_ref must be artifact:sha256:<digest>")
    operator = envelope["operator"]
    if not isinstance(operator, dict) or set(operator) != {"identity", "decision_ref"}:
        raise MaintenanceCapabilityError("operator identity is required")
    _identifier(operator["identity"], "operator.identity")
    _immutable_ref(operator.get("decision_ref"), "operator.decision_ref", {"artifact", "verification-result", "sprint-event"})
    window = envelope["window"]
    if not isinstance(window, dict) or set(window) != {"not_before", "expires_at"}:
        raise MaintenanceCapabilityError("window must be exact")
    not_before = _time(window.get("not_before"), "window.not_before")
    expires_at = _time(window.get("expires_at"), "window.expires_at")
    issued_at = _time(envelope["issued_at"], "issued_at")
    if issued_at > not_before:
        raise MaintenanceCapabilityError("issued_at must not follow window.not_before")
    if expires_at <= not_before or expires_at - not_before > timedelta(hours=24):
        raise MaintenanceCapabilityError("window must be positive, immutable, and no longer than 24 hours")

    repositories: dict[str, str] = {}
    if not isinstance(envelope["repositories"], list) or not envelope["repositories"]:
        raise MaintenanceCapabilityError("repositories must be non-empty")
    for repository in envelope["repositories"]:
        if not isinstance(repository, dict) or set(repository) != {"id", "url", "commit"}:
            raise MaintenanceCapabilityError("repository bindings must be exact")
        repo_id, commit = _identifier(repository["id"], "repository.id"), repository["commit"]
        if repo_id in repositories or not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", commit) is None:
            raise MaintenanceCapabilityError("repository IDs and full commits must be unique and immutable")
        _repository_url(repository["url"], "repository.url")
        repositories[repo_id] = commit

    registry: dict[str, tuple[str, ...]] = {}
    for command in envelope["command_registry"]:
        if not isinstance(command, dict) or set(command) != {"id", "argv"}:
            raise MaintenanceCapabilityError("command registry entries must be exact")
        command_id, argv = _identifier(command["id"], "command_registry.id"), command["argv"]
        if command_id in registry or not isinstance(argv, list) or not argv or len(argv) > 32 or any(len(_text(arg, "command_registry.argv")) > 512 or any(c in arg for c in ";|`\r\n") for arg in argv):
            raise MaintenanceCapabilityError("command registry must contain unique safe exact argv")
        registry[command_id] = tuple(argv)
    expected_registry_ref = "artifact:sha256:" + hashlib.sha256(_canonical(envelope["command_registry"])).hexdigest()
    if envelope.get("command_registry_ref") != expected_registry_ref:
        raise MaintenanceCapabilityError("command_registry_ref must bind canonical exact argv")

    operations: dict[str, Mapping[str, Any]] = {}
    if not isinstance(envelope["operations"], list) or not envelope["operations"]:
        raise MaintenanceCapabilityError("operations must be non-empty")
    for operation in envelope["operations"]:
        if not isinstance(operation, dict) or set(operation) != {"id", "owner_repository", "command_id", "allowed_paths", "allowed_commands"} or operation.get("id") in operations:
            raise MaintenanceCapabilityError("operations must have unique exact IDs")
        _identifier(operation["id"], "operation.id")
        owner = _identifier(operation.get("owner_repository"), "operation.owner_repository")
        commands = _sorted_unique(operation.get("allowed_commands"), "operation.allowed_commands", non_empty=True)
        paths = _sorted_unique(operation.get("allowed_paths"), "operation.allowed_paths", non_empty=True)
        if owner not in repositories or not isinstance(commands, list) or not commands or any(command not in registry for command in commands):
            raise MaintenanceCapabilityError("operation must bind a repository and registered commands")
        for path in paths:
            _relative_path(path, "operation.allowed_paths")
        if operation.get("command_id") not in commands:
            raise MaintenanceCapabilityError("operation primary command must be allowlisted")
        operations[operation["id"]] = operation

    steps = envelope["steps"]
    if not isinstance(steps, list) or not steps:
        raise MaintenanceCapabilityError("steps must be non-empty")
    step_ids: list[str] = []
    prior: set[str] = set()
    repo_heads = dict(repositories)
    previous_phase = -1
    for index, step in enumerate(steps, 1):
        step_fields = {"id", "sequence", "repository_id", "base_commit", "commit", "operation_id", "depends_on", "paths", "commands", "reviews", "verification_refs", "publication_ref", "phase"}
        if not isinstance(step, dict) or set(step) != step_fields or isinstance(step.get("sequence"), bool) or step.get("sequence") != index:
            raise MaintenanceCapabilityError("steps must have contiguous deterministic sequence")
        step_id = _identifier(step["id"], "step.id")
        deps = _sorted_unique(step.get("depends_on"), "step.depends_on")
        if step_id in prior or any(dep not in prior for dep in deps):
            raise MaintenanceCapabilityError("step dependency graph must reference unique earlier steps")
        commands = _sorted_unique(step.get("commands"), "step.commands", non_empty=True)
        paths = _sorted_unique(step.get("paths"), "step.paths", non_empty=True)
        for path in paths:
            _relative_path(path, "step.paths")
        if any(command not in registry for command in commands):
            raise MaintenanceCapabilityError("steps may use only immutable registered commands")
        repo_id = step.get("repository_id")
        operation = operations.get(step.get("operation_id"))
        if repo_id not in repo_heads or operation is None or operation.get("owner_repository") != repo_id:
            raise MaintenanceCapabilityError("step must use an owned allowlisted operation")
        if step.get("base_commit") != repo_heads[repo_id] or not isinstance(step.get("commit"), str) or re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", step["commit"]) is None or step["commit"] == step["base_commit"]:
            raise MaintenanceCapabilityError("step must bind the preceding full repository commit")
        repo_heads[repo_id] = step["commit"]
        if any(command not in operation["allowed_commands"] for command in step.get("commands", [])):
            raise MaintenanceCapabilityError("step command exceeds its operation allowlist")
        if any(not any(path == root or path.startswith(root + "/") for root in operation["allowed_paths"]) for path in step.get("paths", [])):
            raise MaintenanceCapabilityError("step path exceeds its operation allowlist")
        if not step.get("reviews") or not step.get("verification_refs") or not step.get("publication_ref"):
            raise MaintenanceCapabilityError("each step requires independent review, verification, and publication receipts")
        review_refs: set[tuple[str, str, str]] = set()
        for review in step["reviews"]:
            if not isinstance(review, dict) or set(review) != {"reviewer", "author", "verdict", "ref"}:
                raise MaintenanceCapabilityError("step review fields must be exact")
            _identifier(review["reviewer"], "step.review.reviewer")
            _identifier(review["author"], "step.review.author")
            if review.get("reviewer") == review.get("author") or review.get("verdict") != "pass":
                raise MaintenanceCapabilityError("step review must be independent and passing")
            review_ref = _immutable_ref(review.get("ref"), "step.review.ref", {"verification-result"})
            key = (review_ref["kind"], review_ref["source"], review_ref["revision"])
            if key in review_refs:
                raise MaintenanceCapabilityError("step review refs must be unique")
            review_refs.add(key)
        for ref in step["verification_refs"]:
            _immutable_ref(ref, "step.verification_ref", {"artifact", "verification-result"})
        _immutable_ref(step["publication_ref"], "step.publication_ref", {"artifact", "verification-result"})
        phase = _text(step["phase"], "step.phase")
        if phase not in PHASE_ORDER or PHASE_ORDER[phase] < previous_phase:
            raise MaintenanceCapabilityError("step phase must be known and non-decreasing")
        previous_phase = PHASE_ORDER[phase]
        step_ids.append(step["id"])
        prior.add(step["id"])

    definitions: dict[str, Mapping[str, Any]] = {}
    if not isinstance(envelope["jit_fields"], list) or len(envelope["jit_fields"]) != 3:
        raise MaintenanceCapabilityError("JIT definitions must contain the three fixed v1 fields")
    for definition in envelope["jit_fields"]:
        name = definition.get("name") if isinstance(definition, dict) else None
        if not isinstance(definition, dict) or set(definition) != {"name", "source", "pattern", "bind_before_step", "bind_by", "required"} or name not in JIT_NAMES or name in definitions or definition.get("source") != JIT_SOURCES[name] or definition.get("bind_before_step") not in prior or definition.get("required") is not True:
            raise MaintenanceCapabilityError("JIT definitions must be unique, required, and step-scoped")
        bind_by = _time(definition.get("bind_by"), f"jit_fields.{name}.bind_by")
        if bind_by < not_before or bind_by >= expires_at:
            raise MaintenanceCapabilityError("JIT binding deadline must be inside the envelope window")
        pattern = _text(definition.get("pattern"), f"jit_fields.{name}.pattern")
        if len(pattern) > 256 or not pattern.startswith("^") or not pattern.endswith("$") or ".*" in pattern or ".+" in pattern:
            raise MaintenanceCapabilityError("JIT pattern must be bounded and anchored")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise MaintenanceCapabilityError("JIT pattern must compile") from exc
        definitions[name] = definition
    if set(definitions) != JIT_NAMES:
        raise MaintenanceCapabilityError("JIT definitions must contain exactly the fixed v1 fields")
    bindings: dict[str, Mapping[str, Any]] = {}
    for binding in envelope["jit_bindings"]:
        name = binding.get("name") if isinstance(binding, dict) else None
        if not isinstance(binding, dict) or set(binding) != {"name", "value", "observed_at", "bound_at", "evidence_ref", "receipt_ref"} or name not in definitions or name in bindings:
            raise MaintenanceCapabilityError("JIT bindings must be unique declared fields")
        binding_value = _text(binding.get("value"), f"jit_bindings.{name}.value")
        if re.fullmatch(definitions[name]["pattern"], binding_value) is None:
            raise MaintenanceCapabilityError("JIT binding violates its frozen pattern")
        observed = _time(binding.get("observed_at"), f"jit_bindings.{name}.observed_at")
        bound = _time(binding.get("bound_at"), f"jit_bindings.{name}.bound_at")
        deadline = _time(definitions[name]["bind_by"], f"jit_fields.{name}.bind_by")
        if not_before > observed or observed > bound or bound > deadline:
            raise MaintenanceCapabilityError("JIT binding exceeds its observation or target-step deadline")
        _immutable_ref(binding.get("evidence_ref"), "jit.evidence_ref", {"artifact", "verification-result"})
        _immutable_ref(binding.get("receipt_ref"), "jit.receipt_ref", {"artifact"})
        bindings[name] = binding

    gate = envelope["start_gate"]
    if not isinstance(gate, dict) or set(gate) != {"plan", "dependent_implementation_sessions", "active_normal_claims"} or gate.get("plan") != "plan-1":
        raise MaintenanceCapabilityError("activation requires plan-1")
    for name in ("dependent_implementation_sessions", "active_normal_claims"):
        predicate = gate.get(name)
        if not isinstance(predicate, dict) or set(predicate) != {"expected_count", "observed_at", "evidence_ref", "receipt_ref"} or predicate.get("expected_count") != 0:
            raise MaintenanceCapabilityError("plan-1 start predicates must require zero")
        observed = _time(predicate.get("observed_at"), f"start_gate.{name}.observed_at")
        if observed < not_before or observed >= expires_at:
            raise MaintenanceCapabilityError("start-gate observation must fall inside the maintenance window")
        _immutable_ref(predicate.get("evidence_ref"), f"start_gate.{name}.evidence_ref", {"verification-result"})
        _immutable_ref(predicate.get("receipt_ref"), f"start_gate.{name}.receipt_ref", {"artifact"})
    abort = envelope["abort"]
    if not isinstance(abort, dict) or set(abort) != {"before_migration", "after_migration", "forbidden"} or abort["before_migration"] != "restore-reviewed-pre-migration-state" or abort["after_migration"] not in {"restore-uid-attested-backup", "reviewed-forward-fix"} or set(abort["forbidden"]) != ABORT_FORBIDDEN:
        raise MaintenanceCapabilityError("abort policy must exactly match maintenance-envelope/v1")
    recovery = envelope["recovery_policy"]
    if not isinstance(recovery, dict) or set(recovery) != {"record_kinds", "authority", "forbidden_uses"}:
        raise MaintenanceCapabilityError("recovery records must remain non-authoritative")
    recovery_forbidden = _sorted_unique(recovery["forbidden_uses"], "recovery_policy.forbidden_uses")
    if recovery.get("authority") != "none" or recovery.get("record_kinds") != ["observation", "requested-command"] or set(recovery_forbidden) != RECOVERY_FORBIDDEN:
        raise MaintenanceCapabilityError("recovery records must remain non-authoritative")
    audit = envelope["audit_reconciliation"]
    audit_fields = {"incident_correlation_required", "immutable_receipts", "required_outcomes", "redact", "retention", "export_required", "independent_review_required"}
    if not isinstance(audit, dict) or set(audit) != audit_fields or audit["incident_correlation_required"] is not True or audit["export_required"] is not True or audit["independent_review_required"] is not True or any(set(_sorted_unique(audit[field], f"audit_reconciliation.{field}")) != expected for field, expected in (("immutable_receipts", AUDIT_RECEIPTS), ("required_outcomes", AUDIT_OUTCOMES), ("redact", AUDIT_REDACT))) or audit["retention"] not in {"append-only-export", "content-addressed-export"}:
        raise MaintenanceCapabilityError("audit reconciliation policy must exactly match maintenance-envelope/v1")
    digest = hashlib.sha256(_canonical(envelope)).hexdigest()
    return FrozenEnvelope(envelope_id, digest, plan_ref, operator["identity"], not_before, expires_at, tuple(steps), registry, definitions, bindings)


def revision(capability_id: str, state: str, number: int) -> str:
    return f"maintenance-capability:{capability_id}@{state}:{number}"


def _request_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError) as exc:
        raise MaintenanceCapabilityError("request_id must be a canonical UUID") from exc


def _validate_reconciliation_bundle(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != {"incident_ref", "export_ref", "review_ref", "receipts"}:
        raise MaintenanceCapabilityError("reconciliation requires the complete frozen audit bundle")
    _immutable_ref(value["incident_ref"], "reconciliation.incident_ref", {"artifact", "verification-result"})
    _immutable_ref(value["export_ref"], "reconciliation.export_ref", {"artifact"})
    _immutable_ref(value["review_ref"], "reconciliation.review_ref", {"verification-result"})
    receipts = value["receipts"]
    if not isinstance(receipts, dict) or set(receipts) != AUDIT_RECEIPTS:
        raise MaintenanceCapabilityError("reconciliation receipts must cover every frozen audit class")
    for name, receipt in receipts.items():
        _immutable_ref(receipt, f"reconciliation.receipts.{name}", {"artifact", "verification-result"})
    return _canonical(value).decode()


class SQLiteMaintenanceCapabilityStore:
    """Transactional lifecycle store over a Sprintctl SQLite connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def prepare(self, *, capability_id: str, request_id: str, envelope: Mapping[str, Any], actor: str, at: str) -> dict[str, Any]:
        if not CAPABILITY_ID.fullmatch(capability_id):
            raise MaintenanceCapabilityError("capability_id must be mcap:<canonical UUID>")
        request_id = _request_id(request_id)
        frozen = freeze_envelope(envelope)
        now = _time(at, "at")
        # ``at`` is an authority-clock observation, not caller-authored request
        # content.  Excluding it lets a response-loss retry with the same
        # request identity remain idempotent while the first receipt retains
        # the actual server timestamp.
        payload_digest = hashlib.sha256(_canonical({"capability_id": capability_id, "envelope_digest": frozen.digest, "actor": actor})).hexdigest()
        return self._write_prepare(capability_id, request_id, frozen, _canonical(envelope).decode(), actor, at, now, payload_digest)

    def transition(self, *, capability_id: str, request_id: str, action: str, expected_revision: str, actor: str, at: str, step_id: str | None = None, command_id: str | None = None, command_ref: str | None = None, effect_ref: str | None = None, reconciliation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = _request_id(request_id)
        now = _time(at, "at")
        payload = {"capability_id": capability_id, "action": action, "expected_revision": expected_revision, "actor": actor, "step_id": step_id, "command_id": command_id, "command_ref": command_ref, "effect_ref": effect_ref, "reconciliation": reconciliation}
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate(capability_id, request_id, digest)
            if duplicate:
                self.conn.commit()
                return duplicate
            row = self.conn.execute("SELECT * FROM maintenance_capability WHERE capability_id = ?", (capability_id,)).fetchone()
            if row is None:
                raise MaintenanceCapabilityError("unknown maintenance capability")
            current = dict(row)
            if actor != current["operator_identity"]:
                raise MaintenanceCapabilityError("actor must equal the frozen operator identity")
            current_revision = revision(capability_id, current["state"], current["revision"])
            if expected_revision != current_revision:
                raise StaleCapabilityRevision(f"stale capability revision: expected {expected_revision!r}, current {current_revision!r}")
            expires_at = _time(current["expires_at"], "expires_at")
            not_before = _time(current["not_before"], "not_before")
            if action in {"activate", "observe", "reconcile"} and now < not_before:
                raise MaintenanceCapabilityError("maintenance execution is before window.not_before")
            next_sequence = current["next_sequence"]
            audit_bundle_json = None
            if now >= expires_at:
                target = "expired"
                outcome = "expired"
            else:
                target = TRANSITIONS.get(action, {}).get(current["state"])
                if target is None:
                    raise MaintenanceCapabilityError(f"action {action!r} is invalid from {current['state']!r}")
                outcome = "accepted"
                envelope = json.loads(current["envelope_json"])
                if action in {"activate", "observe"}:
                    executed_sequence = self._authorize_step(envelope, current, action, step_id, command_id, command_ref, effect_ref, now)
                    if executed_sequence != current["next_sequence"] or (action == "activate" and executed_sequence != 1):
                        raise MaintenanceCapabilityError("step execution must follow the exact forward sequence cursor")
                    next_sequence = executed_sequence + 1
                elif action in {"attest", "reconcile", "abort", "revoke"}:
                    if command_ref is not None or effect_ref is None:
                        raise MaintenanceCapabilityError("authority transition requires one immutable effect receipt and no command substitution")
                    _exact_ref(effect_ref, "effect_ref")
                if action == "reconcile":
                    if current["next_sequence"] != len(envelope["steps"]) + 1:
                        raise MaintenanceCapabilityError("reconciliation requires every reviewed step receipt in order")
                    audit_bundle_json = _validate_reconciliation_bundle(reconciliation)
                if action == "activate":
                    self._require_zero_ordinary_claims(now)
                    self._require_fresh_start_gate(envelope, now)
            next_revision = current["revision"] + 1
            self.conn.execute("UPDATE maintenance_capability SET state = ?, revision = ?, next_sequence = ?, updated_at = ? WHERE capability_id = ? AND revision = ?", (target, next_revision, next_sequence, at, capability_id, current["revision"]))
            result_revision = revision(capability_id, target, next_revision)
            self._receipt(capability_id, request_id, action, outcome, current["state"], target, result_revision, step_id, command_ref, effect_ref, audit_bundle_json, digest, actor, at)
            self.conn.commit()
            return {"capability_id": capability_id, "request_id": request_id, "action": action, "outcome": outcome, "state": target, "revision": result_revision, "duplicate": False}
        except Exception:
            self.conn.rollback()
            raise

    def append_recovery_record(self, *, capability_id: str, record_id: str, kind: str, payload_ref: str, actor: str, at: str) -> dict[str, Any]:
        if kind not in RECOVERY_KINDS:
            raise MaintenanceCapabilityError("recovery record kind is observation or requested-command only")
        record_id = _request_id(record_id)
        _exact_ref(payload_ref, "payload_ref")
        existing = self.conn.execute("SELECT kind,payload_ref,actor FROM maintenance_capability_recovery WHERE capability_id=? AND record_id=?", (capability_id, record_id)).fetchone()
        if existing is not None:
            existing = dict(existing)
            if (existing["kind"], existing["payload_ref"], existing["actor"]) != (kind, payload_ref, actor):
                raise MaintenanceCapabilityError("recovery record replay changed immutable content")
        else:
            self.conn.execute("INSERT INTO maintenance_capability_recovery (capability_id, record_id, kind, payload_ref, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)", (capability_id, record_id, kind, payload_ref, actor, at))
        self.conn.commit()
        return {"capability_id": capability_id, "record_id": record_id, "kind": kind, "authority": "none"}

    def get(self, capability_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM maintenance_capability WHERE capability_id = ?", (capability_id,)).fetchone()
        return dict(row) if row else None

    def _write_prepare(self, capability_id: str, request_id: str, frozen: FrozenEnvelope, envelope_json: str, actor: str, at: str, now: datetime, digest: str) -> dict[str, Any]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate(capability_id, request_id, digest)
            if duplicate:
                self.conn.commit()
                return duplicate
            if now >= frozen.expires_at:
                raise MaintenanceCapabilityError("expired envelopes cannot be prepared")
            if self.conn.execute("SELECT 1 FROM maintenance_capability WHERE capability_id = ?", (capability_id,)).fetchone():
                raise MaintenanceCapabilityError("capability_id already exists; capabilities cannot be renewed or replaced")
            self.conn.execute("INSERT INTO maintenance_capability (capability_id, envelope_id, envelope_digest, envelope_json, plan_ref, operator_identity, not_before, expires_at, state, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', 1, ?, ?)", (capability_id, frozen.envelope_id, frozen.digest, envelope_json, frozen.plan_ref, frozen.operator, frozen.not_before.isoformat(), frozen.expires_at.isoformat(), at, at))
            result_revision = revision(capability_id, "prepared", 1)
            self._receipt(capability_id, request_id, "prepare", "accepted", None, "prepared", result_revision, None, None, None, None, digest, actor, at)
            self.conn.commit()
            return {"capability_id": capability_id, "request_id": request_id, "action": "prepare", "outcome": "accepted", "state": "prepared", "revision": result_revision, "duplicate": False}
        except Exception:
            self.conn.rollback()
            raise

    def _duplicate(self, capability_id: str, request_id: str, digest: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM maintenance_capability_receipt WHERE capability_id = ? AND request_id = ?", (capability_id, request_id)).fetchone()
        if row is None:
            return None
        row = dict(row)
        if row["request_digest"] != digest:
            raise MaintenanceCapabilityError("request_id replay changed immutable request content")
        return {"capability_id": capability_id, "request_id": request_id, "action": row["action"], "outcome": row["outcome"], "state": row["to_state"], "revision": row["result_revision"], "duplicate": True}

    def _receipt(self, capability_id: str, request_id: str, action: str, outcome: str, from_state: str | None, to_state: str, result_revision: str, step_id: str | None, command_ref: str | None, effect_ref: str | None, audit_bundle_json: str | None, digest: str, actor: str, at: str) -> None:
        self.conn.execute("INSERT INTO maintenance_capability_receipt (capability_id, request_id, action, outcome, from_state, to_state, result_revision, step_id, command_ref, effect_ref, audit_bundle_json, request_digest, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (capability_id, request_id, action, outcome, from_state, to_state, result_revision, step_id, command_ref, effect_ref, audit_bundle_json, digest, actor, at))

    def _require_zero_ordinary_claims(self, now: datetime) -> None:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM claim WHERE status = 'active' AND julianday(expires_at) > julianday(?)", (now.isoformat(),)).fetchone()
        count = row["count"] if hasattr(row, "keys") else row[0]
        if count:
            raise MaintenanceCapabilityError("activation requires zero live ordinary claims")

    def _authorize_step(self, envelope: Mapping[str, Any], current: Mapping[str, Any], action: str, step_id: str | None, command_id: str | None, command_ref: str | None, effect_ref: str | None, now: datetime) -> int:
        if not step_id or not command_id or not command_ref or not effect_ref:
            raise MaintenanceCapabilityError("step execution requires exact step, command, command receipt, and effect receipt")
        _exact_ref(command_ref, "command_ref")
        _exact_ref(effect_ref, "effect_ref")
        steps = {step["id"]: step for step in envelope["steps"]}
        step = steps.get(step_id)
        if step is None or command_id not in step.get("commands", []):
            raise MaintenanceCapabilityError("step or command is outside the reviewed envelope")
        for definition in envelope["jit_fields"]:
            target_sequence = steps[definition["bind_before_step"]]["sequence"]
            if step["sequence"] >= target_sequence:
                binding = next((item for item in envelope["jit_bindings"] if item["name"] == definition["name"]), None)
                if binding is None or _time(binding["bound_at"], "bound_at") > _time(definition["bind_by"], "bind_by") or _time(binding["bound_at"], "bound_at") > now:
                    raise MaintenanceCapabilityError("required step-scoped JIT binding is absent or late")
        return int(step["sequence"])

    def _require_fresh_start_gate(self, envelope: Mapping[str, Any], now: datetime) -> None:
        for name in ("dependent_implementation_sessions", "active_normal_claims"):
            observed = _time(envelope["start_gate"][name]["observed_at"], f"start_gate.{name}.observed_at")
            if observed > now or now - observed > timedelta(minutes=5):
                raise MaintenanceCapabilityError("activation requires fresh start-gate evidence within five minutes")


class PostgresMaintenanceCapabilityStore:
    """Repository-scoped PostgreSQL implementation with SQLite-equivalent effects."""

    def __init__(self, store: Any):
        self.store = store
        self.conn = store.conn
        self.repo_id = store.repo_id

    def prepare(self, *, capability_id: str, request_id: str, envelope: Mapping[str, Any], actor: str, at: str) -> dict[str, Any]:
        if not CAPABILITY_ID.fullmatch(capability_id):
            raise MaintenanceCapabilityError("capability_id must be mcap:<canonical UUID>")
        request_id = _request_id(request_id)
        frozen = freeze_envelope(envelope)
        now = _time(at, "at")
        digest = hashlib.sha256(_canonical({"capability_id": capability_id, "envelope_digest": frozen.digest, "actor": actor})).hexdigest()
        try:
            with self.conn.cursor() as cur:
                duplicate = self._duplicate(cur, capability_id, request_id, digest)
                if duplicate:
                    self.conn.commit()
                    return duplicate
                if now >= frozen.expires_at:
                    raise MaintenanceCapabilityError("expired envelopes cannot be prepared")
                cur.execute("SELECT 1 FROM maintenance_capability WHERE repo_id = %s AND capability_id = %s FOR UPDATE", (self.repo_id, capability_id))
                if cur.fetchone():
                    raise MaintenanceCapabilityError("capability_id already exists; capabilities cannot be renewed or replaced")
                cur.execute("INSERT INTO maintenance_capability (repo_id, capability_id, envelope_id, envelope_digest, envelope_json, plan_ref, operator_identity, not_before, expires_at, state, revision, created_at, updated_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,'prepared',1,%s,%s)", (self.repo_id, capability_id, frozen.envelope_id, frozen.digest, _canonical(envelope).decode(), frozen.plan_ref, frozen.operator, frozen.not_before, frozen.expires_at, now, now))
                result_revision = revision(capability_id, "prepared", 1)
                self._receipt(cur, capability_id, request_id, "prepare", "accepted", None, "prepared", result_revision, None, None, None, None, digest, actor, now)
            self.conn.commit()
            return {"capability_id": capability_id, "request_id": request_id, "action": "prepare", "outcome": "accepted", "state": "prepared", "revision": result_revision, "duplicate": False}
        except Exception:
            self.conn.rollback()
            raise

    def transition(self, *, capability_id: str, request_id: str, action: str, expected_revision: str, actor: str, at: str, step_id: str | None = None, command_id: str | None = None, command_ref: str | None = None, effect_ref: str | None = None, reconciliation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_id = _request_id(request_id)
        now = _time(at, "at")
        payload = {"capability_id": capability_id, "action": action, "expected_revision": expected_revision, "actor": actor, "step_id": step_id, "command_id": command_id, "command_ref": command_ref, "effect_ref": effect_ref, "reconciliation": reconciliation}
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        try:
            with self.conn.cursor() as cur:
                duplicate = self._duplicate(cur, capability_id, request_id, digest)
                if duplicate:
                    self.conn.commit()
                    return duplicate
                if action == "activate":
                    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (self.repo_id,))
                cur.execute("SELECT * FROM maintenance_capability WHERE repo_id = %s AND capability_id = %s FOR UPDATE", (self.repo_id, capability_id))
                row = cur.fetchone()
                if row is None:
                    raise MaintenanceCapabilityError("unknown maintenance capability")
                current = dict(row)
                if actor != current["operator_identity"]:
                    raise MaintenanceCapabilityError("actor must equal the frozen operator identity")
                current_revision = revision(capability_id, current["state"], current["revision"])
                if expected_revision != current_revision:
                    raise StaleCapabilityRevision(f"stale capability revision: expected {expected_revision!r}, current {current_revision!r}")
                expires_at = current["expires_at"]
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                not_before = current["not_before"]
                if not_before.tzinfo is None:
                    not_before = not_before.replace(tzinfo=timezone.utc)
                if action in {"activate", "observe", "reconcile"} and now < not_before:
                    raise MaintenanceCapabilityError("maintenance execution is before window.not_before")
                next_sequence = current["next_sequence"]
                audit_bundle_json = None
                if now >= expires_at:
                    target, outcome = "expired", "expired"
                else:
                    target = TRANSITIONS.get(action, {}).get(current["state"])
                    if target is None:
                        raise MaintenanceCapabilityError(f"action {action!r} is invalid from {current['state']!r}")
                    outcome = "accepted"
                    envelope = current["envelope_json"]
                    if isinstance(envelope, str):
                        envelope = json.loads(envelope)
                    if action in {"activate", "observe"}:
                        executed_sequence = SQLiteMaintenanceCapabilityStore._authorize_step(self, envelope, current, action, step_id, command_id, command_ref, effect_ref, now)
                        if executed_sequence != current["next_sequence"] or (action == "activate" and executed_sequence != 1):
                            raise MaintenanceCapabilityError("step execution must follow the exact forward sequence cursor")
                        next_sequence = executed_sequence + 1
                    elif action in {"attest", "reconcile", "abort", "revoke"}:
                        if command_ref is not None or effect_ref is None:
                            raise MaintenanceCapabilityError("authority transition requires one immutable effect receipt and no command substitution")
                        _exact_ref(effect_ref, "effect_ref")
                    if action == "reconcile":
                        if current["next_sequence"] != len(envelope["steps"]) + 1:
                            raise MaintenanceCapabilityError("reconciliation requires every reviewed step receipt in order")
                        audit_bundle_json = _validate_reconciliation_bundle(reconciliation)
                    if action == "activate":
                        cur.execute("SELECT COUNT(*) AS count FROM claim WHERE repo_id = %s AND status = 'active' AND expires_at > %s", (self.repo_id, now))
                        if int(cur.fetchone()["count"]):
                            raise MaintenanceCapabilityError("activation requires zero live ordinary claims")
                        SQLiteMaintenanceCapabilityStore._require_fresh_start_gate(self, envelope, now)
                next_revision = current["revision"] + 1
                cur.execute("UPDATE maintenance_capability SET state=%s, revision=%s, next_sequence=%s, updated_at=%s WHERE repo_id=%s AND capability_id=%s AND revision=%s", (target, next_revision, next_sequence, now, self.repo_id, capability_id, current["revision"]))
                result_revision = revision(capability_id, target, next_revision)
                self._receipt(cur, capability_id, request_id, action, outcome, current["state"], target, result_revision, step_id, command_ref, effect_ref, audit_bundle_json, digest, actor, now)
            self.conn.commit()
            return {"capability_id": capability_id, "request_id": request_id, "action": action, "outcome": outcome, "state": target, "revision": result_revision, "duplicate": False}
        except Exception:
            self.conn.rollback()
            raise

    def append_recovery_record(self, *, capability_id: str, record_id: str, kind: str, payload_ref: str, actor: str, at: str) -> dict[str, Any]:
        if kind not in RECOVERY_KINDS:
            raise MaintenanceCapabilityError("recovery record kind is observation or requested-command only")
        record_id = _request_id(record_id)
        _exact_ref(payload_ref, "payload_ref")
        with self.conn.cursor() as cur:
            cur.execute("SELECT kind,payload_ref,actor FROM maintenance_capability_recovery WHERE repo_id=%s AND capability_id=%s AND record_id=%s", (self.repo_id, capability_id, record_id))
            existing = cur.fetchone()
            if existing is not None:
                existing = dict(existing)
                if (existing["kind"], existing["payload_ref"], existing["actor"]) != (kind, payload_ref, actor):
                    raise MaintenanceCapabilityError("recovery record replay changed immutable content")
            else:
                cur.execute("INSERT INTO maintenance_capability_recovery (repo_id,capability_id,record_id,kind,payload_ref,actor,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)", (self.repo_id, capability_id, record_id, kind, payload_ref, actor, _time(at, "at")))
        self.conn.commit()
        return {"capability_id": capability_id, "record_id": record_id, "kind": kind, "authority": "none"}

    def get(self, capability_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM maintenance_capability WHERE repo_id=%s AND capability_id=%s", (self.repo_id, capability_id))
            row = cur.fetchone()
        self.conn.rollback()
        return dict(row) if row else None

    def _duplicate(self, cur: Any, capability_id: str, request_id: str, digest: str) -> dict[str, Any] | None:
        cur.execute("SELECT * FROM maintenance_capability_receipt WHERE repo_id=%s AND capability_id=%s AND request_id=%s", (self.repo_id, capability_id, request_id))
        row = cur.fetchone()
        if row is None:
            return None
        row = dict(row)
        if row["request_digest"] != digest:
            raise MaintenanceCapabilityError("request_id replay changed immutable request content")
        return {"capability_id": capability_id, "request_id": request_id, "action": row["action"], "outcome": row["outcome"], "state": row["to_state"], "revision": row["result_revision"], "duplicate": True}

    def _receipt(self, cur: Any, capability_id: str, request_id: str, action: str, outcome: str, from_state: str | None, to_state: str, result_revision: str, step_id: str | None, command_ref: str | None, effect_ref: str | None, audit_bundle_json: str | None, digest: str, actor: str, at: datetime) -> None:
        cur.execute("INSERT INTO maintenance_capability_receipt (repo_id,capability_id,request_id,action,outcome,from_state,to_state,result_revision,step_id,command_ref,effect_ref,audit_bundle_json,request_digest,actor,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)", (self.repo_id, capability_id, request_id, action, outcome, from_state, to_state, result_revision, step_id, command_ref, effect_ref, audit_bundle_json, digest, actor, at))
