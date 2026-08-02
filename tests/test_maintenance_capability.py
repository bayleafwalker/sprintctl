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
        ],
        "jit_bindings": [
            {"name": "backup_name", "value": "backup-0001", "observed_at": "2026-08-02T19:58:00Z", "bound_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
        ],
        "start_gate": {
            "plan": "plan-1",
            "dependent_implementation_sessions": {"expected_count": 0, "observed_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="6"), "receipt_ref": ref(kind="artifact", digit="7")},
            "active_normal_claims": {"expected_count": 0, "observed_at": "2026-08-02T19:59:00Z", "evidence_ref": ref(digit="8"), "receipt_ref": ref(kind="artifact", digit="9")},
        },
        "abort": {},
        "recovery_policy": {"record_kinds": ["observation", "requested-command"], "authority": "none", "forbidden_uses": ["advance"]},
        "audit_reconciliation": {},
    }
    value["command_registry_ref"] = "artifact:sha256:" + hashlib.sha256(json.dumps(value["command_registry"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


@pytest.fixture
def store(conn):
    return SQLiteMaintenanceCapabilityStore(conn)


def prepare(store, value=None):
    return store.prepare(capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=value or envelope(), actor="operator", at=AT)


def transition(store, state, action, **kwargs):
    if action in {"attest", "reconcile", "abort", "revoke"}:
        kwargs.setdefault("effect_ref", "sha256:" + "0" * 64)
    return store.transition(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), action=action,
        expected_revision=state["revision"], actor="operator", at=AT, **kwargs,
    )


def test_exact_lifecycle_is_cas_fenced_and_terminal(store):
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    active = transition(store, attested, "activate", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)
    observed = transition(store, active, "observe", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "e" * 64, effect_ref="sha256:" + "f" * 64)
    reconciled = transition(store, observed, "reconcile")
    assert [prepared["state"], attested["state"], active["state"], observed["state"], reconciled["state"]] == ["prepared", "attested", "active", "observing", "reconciled"]
    with pytest.raises(MaintenanceCapabilityError, match="invalid"):
        transition(store, reconciled, "abort")


def test_replay_is_idempotent_but_changed_request_is_rejected(store):
    request_id = str(uuid4())
    kwargs = dict(capability_id=CAPABILITY_ID, request_id=request_id, envelope=envelope(), actor="operator", at=AT)
    first = store.prepare(**kwargs)
    assert store.prepare(**kwargs) == first | {"duplicate": True}
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


@pytest.mark.parametrize("mutation,message", [
    (lambda e: e.__setitem__("plan_ref", "main"), "immutable"),
    (lambda e: e["command_registry"][0]["argv"].append("; reboot"), "safe exact"),
    (lambda e: e.__setitem__("command_registry_ref", "artifact:sha256:" + "0" * 64), "bind canonical"),
    (lambda e: e["steps"][0]["reviews"][0].update(reviewer="author"), "independent"),
    (lambda e: e["start_gate"]["active_normal_claims"].update(expected_count=1), "require zero"),
    (lambda e: e["recovery_policy"].update(authority="grant"), "non-authoritative"),
    (lambda e: e["jit_bindings"][0].update(bound_at="2026-08-02T20:01:00Z"), "deadline"),
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
