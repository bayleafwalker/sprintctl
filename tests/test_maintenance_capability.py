from __future__ import annotations

import copy
import hashlib
import json
from uuid import uuid4

import pytest

from sprintctl import db
from sprintctl.maintenance_capability import (
    MaintenanceCapabilityError,
    SQLiteMaintenanceCapabilityStore,
    StaleCapabilityRevision,
)


CAPABILITY_ID = "mcap:12345678-1234-4234-8234-123456789abc"
AT = "2026-08-02T20:00:00Z"


def ref(kind="verification-result", digit="1"):
    return {"kind": kind, "source": "test:evidence", "revision": "sha256:" + digit * 64}


def envelope():
    step = {
        "id": "attest-backup", "sequence": 1, "depends_on": [],
        "repository_id": "appservice", "base_commit": "a" * 40,
        "commit": "b" * 40, "operation_id": "targeted-maintenance",
        "paths": ["clusters/main/vuoro"],
        "phase": "pre-migration",
        "commands": ["verify-backup"],
        "reviews": [{"reviewer": "reviewer", "author": "author", "verdict": "pass", "ref": ref()}],
        "verification_refs": [ref(digit="2")], "publication_ref": ref(kind="artifact", digit="3"),
    }
    value = {
        "contract_id": "maintenance-envelope/v1",
        "envelope_id": "vuoro-cutover-exact-1",
        "plan_ref": "artifact:sha256:" + "a" * 64,
        "issued_at": "2026-08-02T19:00:00Z",
        "window": {"not_before": "2026-08-02T19:00:00Z", "expires_at": "2026-08-03T00:00:00Z"},
        "operator": {"identity": "operator", "decision_ref": ref(kind="sprint-event")} | {"decision_ref": {"kind": "sprint-event", "source": "sprintctl:decision", "revision": "event:2253"}},
        "repositories": [{"id": "appservice", "url": "https://github.com/example/appservice.git", "commit": "a" * 40}],
        "command_registry_ref": "",
        "command_registry": [{"id": "verify-backup", "argv": ["verify-backup", "--exact"]}],
        "operations": [{"id": "targeted-maintenance", "owner_repository": "appservice", "command_id": "verify-backup", "allowed_paths": ["clusters/main/vuoro"], "allowed_commands": ["verify-backup"]}],
        "steps": [step],
        "jit_fields": [
            {"name": "backup_name", "source": "backup-observation", "pattern": "^backup-[0-9]{4}$", "bind_before_step": "attest-backup", "bind_by": AT, "required": True},
            {"name": "backup_uid", "source": "backup-observation", "pattern": "^[0-9a-f-]{36}$", "bind_before_step": "attest-backup", "bind_by": AT, "required": True},
            {"name": "drain_boundary_utc", "source": "clock-observation", "pattern": "^[0-9TZ:-]{20}$", "bind_before_step": "attest-backup", "bind_by": AT, "required": True},
        ],
        "jit_bindings": [
            {"name": "backup_name", "value": "backup-0001", "observed_at": "2026-08-02T19:58:00Z", "bound_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
            {"name": "backup_uid", "value": "12345678-1234-1234-1234-123456789abc", "observed_at": "2026-08-02T19:58:00Z", "bound_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
            {"name": "drain_boundary_utc", "value": "2026-08-02T19:50:00Z", "observed_at": "2026-08-02T19:58:00Z", "bound_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
        ],
        "start_gate": {
            "plan": "plan-1",
            "dependent_implementation_sessions": {"expected_count": 0, "observed_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="6"), "receipt_ref": ref(kind="artifact", digit="7")},
            "active_normal_claims": {"expected_count": 0, "observed_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="8"), "receipt_ref": ref(kind="artifact", digit="9")},
        },
        "abort": {"before_migration": "restore-reviewed-pre-migration-state", "after_migration": "restore-uid-attested-backup", "forbidden": ["delete-migration-ledger", "edit-released-migration", "recovery-request-authority", "unreviewed-commit"]},
        "recovery_policy": {"record_kinds": ["observation", "requested-command"], "authority": "none", "forbidden_uses": ["advance", "approve", "bind-jit", "claim", "grant", "publish", "reconcile"]},
        "audit_reconciliation": {"incident_correlation_required": True, "immutable_receipts": ["abort", "command", "effect", "jit-binding", "publication", "reconciliation", "review", "start-gate"], "required_outcomes": ["aborted", "accepted", "duplicate", "expired", "incomplete", "rejected"], "redact": ["capability-secrets", "claim-tokens", "credentials"], "retention": "content-addressed-export", "export_required": True, "independent_review_required": True},
    }
    value["command_registry_ref"] = "artifact:sha256:" + hashlib.sha256(json.dumps(value["command_registry"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def reconciliation_bundle():
    return {
        "incident_ref": ref(kind="artifact", digit="1"),
        "export_ref": ref(kind="artifact", digit="2"),
        "review_ref": ref(digit="3"),
        "receipts": {name: ref(kind="artifact", digit=str(index)) for index, name in enumerate(["abort", "command", "effect", "jit-binding", "publication", "reconciliation", "review", "start-gate"], 1)},
    }


@pytest.fixture
def store(conn):
    return SQLiteMaintenanceCapabilityStore(conn)


def prepare(store, value=None):
    return store.prepare(capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=value or envelope(), actor="operator", at=AT)


def transition(store, state, action, **kwargs):
    if action in {"attest", "reconcile", "abort", "revoke"}:
        kwargs.setdefault("effect_ref", "sha256:" + "0" * 64)
    if action == "reconcile":
        kwargs.setdefault("reconciliation", reconciliation_bundle())
    return store.transition(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), action=action,
        expected_revision=state["revision"], actor="operator", at=AT, **kwargs,
    )


def test_exact_lifecycle_is_cas_fenced_and_terminal(store):
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    active = transition(store, attested, "activate", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)
    reconciled = transition(store, active, "reconcile")
    assert [prepared["state"], attested["state"], active["state"], reconciled["state"]] == ["prepared", "attested", "active", "reconciled"]
    with pytest.raises(MaintenanceCapabilityError, match="invalid"):
        transition(store, reconciled, "abort")


def test_governing_contract_is_exact_and_unknown_or_incomplete_fields_reject(store):
    for mutation in (
        lambda e: e.__setitem__("unknown_authority", True),
        lambda e: e.__setitem__("abort", {}),
        lambda e: e["jit_fields"].pop(),
        lambda e: e.__setitem__("audit_reconciliation", {}),
    ):
        value = envelope()
        mutation(value)
        with pytest.raises(MaintenanceCapabilityError):
            prepare(store, value)


def test_replay_is_idempotent_but_changed_request_is_rejected(store):
    request_id = str(uuid4())
    kwargs = dict(capability_id=CAPABILITY_ID, request_id=request_id, envelope=envelope(), actor="operator", at=AT)
    first = store.prepare(**kwargs)
    assert store.prepare(**kwargs) == first | {"duplicate": True}
    assert store.prepare(**(kwargs | {"at": "2026-08-02T20:01:00Z"})) == first | {
        "duplicate": True
    }
    changed = copy.deepcopy(envelope())
    changed["operator"]["identity"] = "other"
    with pytest.raises(MaintenanceCapabilityError, match="changed immutable"):
        store.prepare(**(kwargs | {"envelope": changed}))


def test_stale_revision_has_no_effect(store):
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    with pytest.raises(StaleCapabilityRevision):
        transition(store, prepared, "activate", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)
    assert store.get(CAPABILITY_ID)["state"] == attested["state"]


def test_transition_response_loss_replay_ignores_later_authority_clock(store):
    prepared = prepare(store)
    request_id = str(uuid4())
    arguments = dict(
        capability_id=CAPABILITY_ID,
        request_id=request_id,
        action="attest",
        expected_revision=prepared["revision"],
        actor="operator",
        effect_ref="sha256:" + "0" * 64,
    )
    first = store.transition(**arguments, at=AT)
    replay = store.transition(**arguments, at="2026-08-02T20:01:00Z")
    assert replay == first | {"duplicate": True}


def test_activation_requires_zero_live_ordinary_claims(store, conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "work")
    item = db.create_work_item(conn, active_sprint["id"], track, "ordinary")
    db.create_claim(conn, item, "worker", ttl_seconds=3600)
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    with pytest.raises(MaintenanceCapabilityError, match="zero live ordinary claims"):
        transition(store, attested, "activate", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)


def test_capability_is_nonrenewable_and_expiry_terminalizes(store):
    prepared = prepare(store)
    with pytest.raises(MaintenanceCapabilityError, match="cannot be renewed"):
        prepare(store)
    expired = store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at="2026-08-03T00:00:00Z")
    assert expired["state"] == expired["outcome"] == "expired"


def test_activation_rejects_before_window_and_step_cursor_cannot_jump_or_reverse(store):
    value = envelope()
    second = copy.deepcopy(value["steps"][0])
    second.update(id="migrate", sequence=2, base_commit="b" * 40, commit="c" * 40, depends_on=["attest-backup"])
    value["steps"].append(second)
    prepared = store.prepare(capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=value, actor="operator", at="2026-08-02T18:00:00Z")
    attested = store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at="2026-08-02T18:00:00Z", effect_ref="sha256:" + "0" * 64)
    with pytest.raises(MaintenanceCapabilityError, match="not_before"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at="2026-08-02T18:00:00Z", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)
    with pytest.raises(MaintenanceCapabilityError, match="forward sequence"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="migrate", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)
    active = store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)
    with pytest.raises(MaintenanceCapabilityError, match="forward sequence"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="observe", expected_revision=active["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "3" * 64, effect_ref="sha256:" + "4" * 64)


@pytest.mark.parametrize("mutation,message", [
    (lambda e: e.__setitem__("plan_ref", "main"), "artifact"),
    (lambda e: e.__setitem__("plan_ref", "sha256:" + "a" * 64), "artifact"),
    (lambda e: e["repositories"][0].update(url="https://user:password@example.com/repo.git"), "credential-free"),
    (lambda e: e["command_registry"][0]["argv"].append("; reboot"), "safe exact"),
    (lambda e: e.__setitem__("command_registry_ref", "artifact:sha256:" + "0" * 64), "bind canonical"),
    (lambda e: e["steps"][0]["reviews"][0].update(reviewer="author"), "independent"),
    (lambda e: e["start_gate"]["active_normal_claims"].update(expected_count=1), "require zero"),
    (lambda e: e["recovery_policy"].update(authority="grant"), "non-authoritative"),
    (lambda e: e["jit_bindings"][0].update(bound_at="2026-08-02T20:01:00Z"), "deadline"),
    (lambda e: (e["jit_fields"][0].update(pattern="^<backup>$"), e["jit_bindings"][0].update(value="<backup>")), "credential-free text"),
    (lambda e: (e["jit_fields"][0].update(pattern="^[0-9]+$"), e["jit_bindings"][0].update(value=1234)), "credential-free text"),
    (lambda e: e["steps"][0].update(phase="arbitrary"), "phase"),
    (lambda e: e["steps"][0]["reviews"][0].update(authority=True), "fields must be exact"),
    (lambda e: e["start_gate"]["active_normal_claims"].update(observed_at="2026-08-02T18:59:00Z"), "inside the maintenance window"),
    (lambda e: e["operations"][0].update(allowed_commands=["verify-backup", "verify-backup"]), "sorted unique"),
    (lambda e: e["operations"][0].update(allowed_paths=["clusters//main/vuoro"]), "normalized"),
])
def test_envelope_authority_mutations_fail_closed(store, mutation, message):
    value = envelope()
    mutation(value)
    with pytest.raises(MaintenanceCapabilityError, match=message):
        prepare(store, value)


def test_wrong_step_command_and_missing_receipts_fail_closed(store):
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    with pytest.raises(MaintenanceCapabilityError, match="outside"):
        transition(store, attested, "activate", step_id="attest-backup", command_id="arbitrary", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)
    with pytest.raises(MaintenanceCapabilityError, match="requires exact"):
        transition(store, attested, "activate", step_id="attest-backup", command_id="verify-backup")


def test_wrong_actor_and_stale_start_evidence_fail_closed(store):
    prepared = prepare(store)
    with pytest.raises(MaintenanceCapabilityError, match="frozen operator"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest", expected_revision=prepared["revision"], actor="other", at=AT, effect_ref="sha256:" + "0" * 64)
    attested = transition(store, prepared, "attest")
    with pytest.raises(MaintenanceCapabilityError, match="fresh start-gate"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at="2026-08-02T20:06:00Z", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)


def test_recovery_requested_command_is_audited_without_authority(store):
    prepared = prepare(store)
    result = store.append_recovery_record(capability_id=CAPABILITY_ID, record_id=str(uuid4()), kind="requested-command", payload_ref="artifact:sha256:" + "1" * 64, actor="recovery", at=AT)
    assert result["authority"] == "none"
    assert store.get(CAPABILITY_ID)["state"] == prepared["state"]
    record_id = str(uuid4())
    store.append_recovery_record(capability_id=CAPABILITY_ID, record_id=record_id, kind="observation", payload_ref="artifact:sha256:" + "2" * 64, actor="recovery", at=AT)
    with pytest.raises(MaintenanceCapabilityError, match="changed immutable"):
        store.append_recovery_record(capability_id=CAPABILITY_ID, record_id=record_id, kind="requested-command", payload_ref="artifact:sha256:" + "3" * 64, actor="recovery", at=AT)
    with pytest.raises(MaintenanceCapabilityError, match="observation or requested-command"):
        store.append_recovery_record(capability_id=CAPABILITY_ID, record_id=str(uuid4()), kind="activate", payload_ref="artifact:sha256:" + "1" * 64, actor="recovery", at=AT)


def test_receipt_and_recovery_rows_are_database_immutable(store):
    prepared = prepare(store)
    with pytest.raises(Exception, match="immutable"):
        store.conn.execute("UPDATE maintenance_capability_receipt SET actor='tampered' WHERE capability_id=?", (CAPABILITY_ID,))
    store.conn.rollback()
    with pytest.raises(Exception, match="immutable"):
        store.conn.execute("DELETE FROM maintenance_capability_receipt WHERE capability_id=?", (CAPABILITY_ID,))
    store.conn.rollback()
    assert store.get(CAPABILITY_ID)["state"] == prepared["state"]


def test_reconcile_requires_complete_audit_bundle(store):
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    active = transition(store, attested, "activate", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)
    with pytest.raises(MaintenanceCapabilityError, match="complete frozen audit bundle"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="reconcile", expected_revision=active["revision"], actor="operator", at=AT, effect_ref="sha256:" + "3" * 64, reconciliation={})
