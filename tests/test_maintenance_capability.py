from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
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
from sprintctl.maintenance_resource import MaintenanceResourceStore


CAPABILITY_ID = "mcap:12345678-1234-4234-8234-123456789abc"

# `at` is the caller-supplied event time. Since 0.2.17 it is audit data and
# never decides admissibility, so it is deliberately left as a fixed instant
# unrelated to the window: any test that still passes with it is proving the
# database clock is in charge.
AT = "2026-08-02T20:00:00Z"


def _stamp(delta: timedelta) -> str:
    """A window instant relative to the clock the database will observe.

    Windows must never be pinned to fixed calendar dates: admissibility is
    decided against the database clock, so a fixed window silently stops
    exercising these paths the moment wall-clock time passes it.
    """
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        "issued_at": _stamp(timedelta(minutes=-30)),
        "window": {"not_before": _stamp(timedelta(minutes=-30)), "expires_at": _stamp(timedelta(hours=4))},
        "operator": {"identity": "operator", "decision_ref": ref(kind="sprint-event")} | {"decision_ref": {"kind": "sprint-event", "source": "sprintctl:decision", "revision": "event:2253"}},
        "repositories": [{"id": "appservice", "url": "https://github.com/example/appservice.git", "commit": "a" * 40}],
        "command_registry_ref": "",
        "command_registry": [{"id": "verify-backup", "argv": ["verify-backup", "--exact"]}],
        "operations": [{"id": "targeted-maintenance", "owner_repository": "appservice", "command_id": "verify-backup", "allowed_paths": ["clusters/main/vuoro"], "allowed_commands": ["verify-backup"]}],
        "steps": [step],
        "jit_fields": [
            {"name": "backup_name", "source": "backup-observation", "pattern": "^backup-[0-9]{4}$", "bind_before_step": "attest-backup", "bind_by": _stamp(timedelta(hours=1)), "required": True},
            {"name": "backup_uid", "source": "backup-observation", "pattern": "^[0-9a-f-]{36}$", "bind_before_step": "attest-backup", "bind_by": _stamp(timedelta(hours=1)), "required": True},
            {"name": "drain_boundary_utc", "source": "clock-observation", "pattern": "^[0-9TZ:-]{20}$", "bind_before_step": "attest-backup", "bind_by": _stamp(timedelta(hours=1)), "required": True},
        ],
        "jit_bindings": [
            {"name": "backup_name", "value": "backup-0001", "observed_at": _stamp(timedelta(minutes=-4)), "bound_at": _stamp(timedelta(minutes=-3)), "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
            {"name": "backup_uid", "value": "12345678-1234-1234-1234-123456789abc", "observed_at": _stamp(timedelta(minutes=-4)), "bound_at": _stamp(timedelta(minutes=-3)), "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
            {"name": "drain_boundary_utc", "value": _stamp(timedelta(minutes=-20)), "observed_at": _stamp(timedelta(minutes=-4)), "bound_at": _stamp(timedelta(minutes=-3)), "evidence_ref": ref(digit="4"), "receipt_ref": ref(kind="artifact", digit="5")},
        ],
        "start_gate": {
            "plan": "plan-1",
            "dependent_implementation_sessions": {"expected_count": 0, "observed_at": _stamp(timedelta(minutes=-1)), "evidence_ref": ref(digit="6"), "receipt_ref": ref(kind="artifact", digit="7")},
            "active_normal_claims": {"expected_count": 0, "observed_at": _stamp(timedelta(minutes=-1)), "evidence_ref": ref(digit="8"), "receipt_ref": ref(kind="artifact", digit="9")},
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


def shifted_envelope(offset: timedelta):
    """An envelope whose whole window is moved relative to the database clock.

    Expiry and not-before are properties of the window, not of the caller's
    `at`, so tests that want those states have to move the window.
    """
    value = envelope()
    not_before = offset
    expires_at = offset + timedelta(hours=1)
    value["issued_at"] = _stamp(not_before - timedelta(minutes=5))
    value["window"] = {"not_before": _stamp(not_before), "expires_at": _stamp(expires_at)}
    bind_by = offset + timedelta(minutes=50)
    observed = offset + timedelta(minutes=1)
    bound = offset + timedelta(minutes=2)
    for definition in value["jit_fields"]:
        definition["bind_by"] = _stamp(bind_by)
    for binding in value["jit_bindings"]:
        binding["observed_at"] = _stamp(observed)
        binding["bound_at"] = _stamp(bound)
        if binding["name"] == "drain_boundary_utc":
            binding["value"] = _stamp(observed)
    for name in ("dependent_implementation_sessions", "active_normal_claims"):
        value["start_gate"][name]["observed_at"] = _stamp(observed)
    return value


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
    # The first durable receipt wins even when response recovery happens after
    # the frozen execution window has expired.
    assert store.prepare(**(kwargs | {"at": "2026-08-03T00:00:01Z"})) == first | {
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


def test_resource_prepare_response_loss_returns_original_binding_atomically(store, conn):
    request_id = str(uuid4())
    arguments = dict(
        capability_id=CAPABILITY_ID, request_id=request_id, envelope=envelope(),
        actor="operator", at=AT, resource=True,
    )
    first = store.prepare(**arguments)
    reference = MaintenanceResourceStore(store).reference_envelope(CAPABILITY_ID)
    replay = store.prepare(**(arguments | {"at": "2026-08-02T20:01:00Z"}))
    assert replay == first | {"duplicate": True}
    assert MaintenanceResourceStore(store).reference_envelope(CAPABILITY_ID) == reference
    assert conn.execute("SELECT count(*) FROM maintenance_resource").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM maintenance_resource_event").fetchone()[0] == 1


def test_expiry_sweep_commits_terminal_projection_with_owner_state(store, monkeypatch):
    store.prepare(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=envelope(),
        actor="operator", at=AT, resource=True,
    )
    binding = MaintenanceResourceStore(store).reference_envelope(CAPABILITY_ID)
    monkeypatch.setattr(store, "_decision_time", lambda: datetime.now(timezone.utc) + timedelta(days=1))
    assert store.sweep_expired(CAPABILITY_ID, at=AT) is True
    snapshot = MaintenanceResourceStore(store).snapshot(binding["reference"])
    assert snapshot["state"]["state"] == "expired"
    assert snapshot["terminal"] is True
    assert snapshot["cursor"] == "sprintctl-maintenance-cursor-2"


def test_activation_requires_zero_live_ordinary_claims(store, conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "work")
    item = db.create_work_item(conn, active_sprint["id"], track, "ordinary")
    db.create_claim(conn, item, "worker", ttl_seconds=3600)
    prepared = prepare(store)
    attested = transition(store, prepared, "attest")
    with pytest.raises(MaintenanceCapabilityError, match="zero live ordinary claims"):
        transition(store, attested, "activate", step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)


def test_capability_is_nonrenewable_and_expiry_terminalizes(store):
    prepare(store)
    with pytest.raises(MaintenanceCapabilityError, match="cannot be renewed"):
        prepare(store)


def test_expiry_terminalizes_on_the_database_clock(store):
    """The window decides expiry, not the caller's `at`."""
    prepared = prepare(store, shifted_envelope(timedelta(hours=-3)))
    expired = store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT)
    assert expired["state"] == expired["outcome"] == "expired"


def test_activation_before_the_window_is_rejected_on_the_database_clock(store):
    """A future window is not admissible however recent the caller's `at` is."""
    prepared = prepare(store, shifted_envelope(timedelta(hours=2)))
    attested = store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT, effect_ref="sha256:" + "0" * 64)
    with pytest.raises(MaintenanceCapabilityError, match="not_before"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=AT, step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64)


def test_step_cursor_cannot_jump_or_reverse(store):
    value = envelope()
    second = copy.deepcopy(value["steps"][0])
    second.update(id="migrate", sequence=2, base_commit="b" * 40, commit="c" * 40, depends_on=["attest-backup"])
    value["steps"].append(second)
    prepared = store.prepare(capability_id=CAPABILITY_ID, request_id=str(uuid4()), envelope=value, actor="operator", at=AT)
    attested = store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at=AT, effect_ref="sha256:" + "0" * 64)
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
    assert attested["state"] == "attested"


def test_stale_start_gate_evidence_is_judged_against_the_database_clock(store):
    """Start-gate freshness is measured from the database clock.

    Before 0.2.17 a caller could hold evidence fresh indefinitely by supplying
    an `at` near its observation time, which is the whole point of the gate.
    """
    stale = envelope()
    for name in ("dependent_implementation_sessions", "active_normal_claims"):
        stale["start_gate"][name]["observed_at"] = _stamp(timedelta(minutes=-20))
    prepared = prepare(store, stale)
    attested = transition(store, prepared, "attest")
    with pytest.raises(MaintenanceCapabilityError, match="fresh start-gate"):
        store.transition(capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate", expected_revision=attested["revision"], actor="operator", at=_stamp(timedelta(minutes=-19)), step_id="attest-backup", command_id="verify-backup", command_ref="sha256:" + "c" * 64, effect_ref="sha256:" + "d" * 64)


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


# ---------------------------------------------------------------------------
# #2093 -- database time authorizes transitions; caller `at` is audit data.
#
# Claim admission filters live capabilities on the database clock. If a caller
# could authorize a transition with its own timestamp, a delayed, retried, or
# replayed request could drive a capability into a state that admission no
# longer honors, and the mutual exclusion Plan 1 depends on would not hold.
# ---------------------------------------------------------------------------


def _attested(store, value=None):
    prepared = prepare(store, value)
    return store.transition(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="attest",
        expected_revision=prepared["revision"], actor="operator", at=AT,
        effect_ref="sha256:" + "0" * 64,
    )


def _receipts(store, *, action=None):
    rows = store.conn.execute(
        "SELECT action, outcome, created_at FROM maintenance_capability_receipt "
        "WHERE capability_id = ? ORDER BY rowid", (CAPABILITY_ID,)
    ).fetchall()
    return [dict(row) for row in rows if action is None or row["action"] == action]


def _activate(store, attested, at):
    return store.transition(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate",
        expected_revision=attested["revision"], actor="operator", at=at,
        step_id="attest-backup", command_id="verify-backup",
        command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64,
    )


def test_stale_pre_expiry_at_cannot_activate_after_database_expiry(store):
    attested = _attested(store, shifted_envelope(timedelta(hours=-3)))
    # `at` sits comfortably inside the window that has since closed.
    result = _activate(store, attested, at=_stamp(timedelta(hours=-2, minutes=-30)))
    assert result["state"] == result["outcome"] == "expired"
    assert store.get(CAPABILITY_ID)["state"] == "expired"


def test_future_at_cannot_make_a_transition_admissible_early(store):
    attested = _attested(store, shifted_envelope(timedelta(hours=2)))
    # `at` claims to be inside the window that has not opened yet.
    with pytest.raises(MaintenanceCapabilityError, match="not_before"):
        _activate(store, attested, at=_stamp(timedelta(hours=2, minutes=30)))
    assert store.get(CAPABILITY_ID)["state"] == "attested"


def test_replay_after_expiry_is_rejected(store):
    attested = _attested(store, shifted_envelope(timedelta(hours=-3)))
    first = _activate(store, attested, at=AT)
    assert first["outcome"] == "expired"
    # A replay of the same intent after expiry must not resurrect the window.
    replay = store.transition(
        capability_id=CAPABILITY_ID, request_id=str(uuid4()), action="activate",
        expected_revision=first["revision"], actor="operator", at=AT,
        step_id="attest-backup", command_id="verify-backup",
        command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64,
    )
    assert replay["outcome"] == "expired"
    assert store.get(CAPABILITY_ID)["state"] == "expired"


def test_expiry_boundary_is_deterministic(store):
    """A window that closed a moment ago is expired, not admissible."""
    attested = _attested(store, shifted_envelope(timedelta(hours=-1, seconds=-2)))
    result = _activate(store, attested, at=AT)
    assert result["state"] == result["outcome"] == "expired"


def test_rejected_activation_leaves_no_misleading_active_state(store):
    attested = _attested(store, shifted_envelope(timedelta(hours=2)))
    with pytest.raises(MaintenanceCapabilityError):
        _activate(store, attested, at=AT)
    current = store.get(CAPABILITY_ID)
    assert current["state"] == "attested"
    # No activate receipt at all: the rejection left no partial trace.
    assert _receipts(store, action="activate") == []


def test_caller_time_and_database_decision_time_are_independently_observable(store):
    """`at` is retained as the recorded event time and does not gate anything."""
    attested = _attested(store)
    active = _activate(store, attested, at=AT)
    assert active["outcome"] == "accepted"
    # The caller's `at` is what got recorded, unchanged and still auditable...
    receipt = _receipts(store, action="activate")[-1]
    assert receipt["created_at"] == AT
    # ...even though it is nowhere near the database clock that admitted it,
    # which is precisely the separation this change establishes.
    recorded = datetime.fromisoformat(receipt["created_at"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - recorded).total_seconds()) > 3600
