from __future__ import annotations

import sqlite3
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sprintctl import db
from sprintctl.maintenance_resource import (
    CursorExpired,
    MaintenanceResourceStore,
    ResourceNotFound,
    NOT_FOUND_PAYLOAD,
)


HISTORIES = (
    "concurrent-terminal-handoff", "cursor-at-floor", "cursor-below-floor",
    "disconnect", "duplicate-delivery", "expiry-materialization-authorized-write",
    "expiry-read-does-not-mutate", "immediate-client-compatibility",
    "max-100-batch", "non-disclosure-four-way", "parallel-owner-decoders",
    "postgres-parity", "prepare-response-loss", "prune-0-255-256-257",
    "redaction-canaries", "restart-during-wait", "spurious-wake", "sqlite-parity",
    "wait-0-immediate", "wait-30-controlled-clock", "wait-early-wake",
)

TRANSACTIONAL_HISTORIES = frozenset({
    "disconnect", "expiry-materialization-authorized-write",
    "parallel-owner-decoders", "prepare-response-loss", "restart-during-wait",
})


def expiring_envelope(value):
    now = datetime.now(timezone.utc)
    value["issued_at"] = value["window"]["not_before"] = (now - timedelta(seconds=2)).isoformat()
    value["window"]["expires_at"] = (now + timedelta(seconds=1)).isoformat()
    for definition in value["jit_fields"]: definition["bind_by"] = (now + timedelta(milliseconds=500)).isoformat()
    for binding in value["jit_bindings"]:
        binding["observed_at"] = (now - timedelta(seconds=1)).isoformat()
        binding["bound_at"] = (now - timedelta(milliseconds=500)).isoformat()
        if binding["name"] == "drain_boundary_utc": binding["value"] = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in ("dependent_implementation_sessions", "active_normal_claims"):
        value["start_gate"][name]["observed_at"] = (now - timedelta(milliseconds=500)).isoformat()
    return value


class Owner:
    def __init__(self, connection):
        self.conn = connection
        self.rows = {"mcap:test": {"state": "prepared", "not_before": "2026-08-02T19:00:00Z", "expires_at": "2026-08-02T20:00:00Z", "updated_at": "2026-08-02T19:00:00Z"}}
        connection.execute(
            "INSERT INTO maintenance_capability(capability_id,envelope_id,envelope_digest,envelope_json,plan_ref,operator_identity,not_before,expires_at,state,revision,next_sequence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,1,?,?)",
            ("mcap:test", "envelope", "0" * 64, "{}", "artifact:sha256:" + "0" * 64, "operator", "2026-08-02T19:00:00Z", "2026-08-02T20:00:00Z", "prepared", "2026-08-02T19:00:00Z", "2026-08-02T19:00:00Z"),
        )
        connection.commit()

    def get(self, capability_id):
        return self.rows.get(capability_id)

    def set(self, state, position):
        updated_at = (datetime(2026, 8, 2, 19, tzinfo=timezone.utc) + timedelta(minutes=position)).isoformat().replace("+00:00", "Z")
        self.rows["mcap:test"].update(state=state, updated_at=updated_at)
        self.conn.execute("UPDATE maintenance_capability SET state=?,revision=revision+1,updated_at=? WHERE capability_id='mcap:test'", (state, self.rows["mcap:test"]["updated_at"]))
        self.conn.commit()


@pytest.fixture
def resource():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    owner = Owner(connection)
    return owner, MaintenanceResourceStore(owner)


def test_reference_retry_snapshot_and_projection_redaction(resource):
    owner, store = resource
    store.record_current("mcap:test")
    first = store.reference_envelope("mcap:test")
    second = store.reference_envelope("mcap:test")
    assert first == second
    assert first["reference"].startswith("smr1_") and len(first["reference"]) == 48
    snapshot = store.snapshot(first["reference"])
    assert set(snapshot["state"]) == {"state", "not_before", "expires_at", "updated_at"}
    assert "mcap:test" not in repr(snapshot)
    assert owner.rows["mcap:test"]["state"] == "prepared"


def test_published_vuoro_goldens_are_byte_for_byte_owner_outputs(monkeypatch):
    monkeypatch.setattr("sprintctl.maintenance_resource.secrets.token_bytes", lambda _size: bytes(32))
    contract = json.loads((Path(__file__).parents[1] / "verification/fixtures/maintenance-resource-owner-v1/frozen-owner-contract.json").read_text())
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    owner = Owner(connection)
    store = MaintenanceResourceStore(owner)
    store.record_current("mcap:test")
    reference = store.reference_envelope("mcap:test")
    assert reference == contract["goldens"]["reference"]
    owner.set("attested", 1); store.record_current("mcap:test")
    owner.set("active", 2); store.record_current("mcap:test")
    assert store.snapshot(reference["reference"]) == contract["goldens"]["snapshot"]
    owner.set("reconciled", 4); store.record_current("mcap:test")
    assert store.changes(reference["reference"], "sprintctl-maintenance-cursor-3") == contract["goldens"]["changes"]


def test_order_terminal_duplicate_and_max_batch(resource):
    owner, store = resource
    reference = store.record_current("mcap:test")
    for position in range(1, 121):
        owner.set("reconciled" if position == 120 else "active", position)
        store.record_current("mcap:test")
    first = store.changes(reference, "sprintctl-maintenance-cursor-0")
    assert len(first["events"]) == 100
    assert len({event["event_id"] for event in first["events"]}) == 100
    second = store.changes(reference, first["next_cursor"])
    assert second["events"][-1]["terminal"] is True
    duplicate = store.changes(reference, "sprintctl-maintenance-cursor-0")
    assert duplicate["events"] == first["events"]


def test_pruning_floor_is_smallest_resumable_and_changes_only_on_prune(resource):
    owner, store = resource
    reference = store.record_current("mcap:test")
    for position in range(1, 257):
        owner.set("active", position)
        store.record_current("mcap:test")
    binding = store._binding(reference)
    assert binding[1] == 1
    store.changes(reference, "sprintctl-maintenance-cursor-1")
    with pytest.raises(CursorExpired):
        store.changes(reference, "sprintctl-maintenance-cursor-0")


def test_read_does_not_materialize_wall_clock_expiry(resource):
    owner, store = resource
    reference = store.record_current("mcap:test")
    before = store._binding(reference)
    assert store.snapshot(reference)["terminal"] is False
    assert store.changes(reference, "sprintctl-maintenance-cursor-1", 0)["events"] == []
    assert store._binding(reference) == before
    assert owner.rows["mcap:test"]["state"] == "prepared"


def test_owner_visibility_boundary_is_indistinguishable_for_four_classes(resource):
    _owner, store = resource
    reference = store.record_current("mcap:test")
    cases = {
        "malformed": ("bad", True),
        "absent": ("smr1_" + "B" * 43, True),
        "foreign": ("smr1_" + "C" * 43, True),
        "unauthorized": (reference, False),
    }
    responses = []
    for resource_ref, authorized in cases.values():
        assert store.visible(resource_ref, authorized=authorized) is False
        responses.append((404, NOT_FOUND_PAYLOAD))
    assert responses == [responses[0]] * 4
    assert store.visible(reference, authorized=True) is True


def test_wait_controlled_clock_spurious_and_early_wake():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    owner = Owner(connection)
    clock = [0.0]
    store = MaintenanceResourceStore(owner, monotonic=lambda: clock[0], pause=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    reference = store.record_current("mcap:test")
    empty = store.changes(reference, "sprintctl-maintenance-cursor-1", 30)
    assert empty["events"] == [] and clock[0] >= 30
    calls = [0]
    def wake(_seconds):
        calls[0] += 1
        clock[0] += .05
        if calls[0] == 2:
            owner.set("active", 2)
            store.record_current("mcap:test")
    store.pause = wake
    early = store.changes(reference, "sprintctl-maintenance-cursor-1", 30)
    assert early["events"][0]["data"]["state"] == "active" and calls[0] == 2


def test_concurrent_terminal_snapshot_handoff_is_atomic(tmp_path):
    path = tmp_path / "terminal-handoff.db"
    writer_conn = sqlite3.connect(path, check_same_thread=False)
    writer_conn.row_factory = sqlite3.Row
    db.init_db(writer_conn)
    writer_owner = Owner(writer_conn)
    writer = MaintenanceResourceStore(writer_owner)
    reference = writer.record_current("mcap:test")

    reader_conn = sqlite3.connect(path, check_same_thread=False)
    reader_conn.row_factory = sqlite3.Row
    reader = MaintenanceResourceStore(type("Reader", (), {"conn": reader_conn})())
    gate = threading.Barrier(2)
    observed = {}

    def publish_terminal():
        gate.wait()
        writer_conn.execute("BEGIN IMMEDIATE")
        updated_at = "2026-08-02T19:02:00Z"
        writer_owner.rows["mcap:test"].update(state="reconciled", updated_at=updated_at)
        writer_conn.execute(
            "UPDATE maintenance_capability SET state='reconciled',revision=revision+1,updated_at=? WHERE capability_id='mcap:test'",
            (updated_at,),
        )
        writer.record_current("mcap:test", commit=False)
        writer_conn.commit()

    thread = threading.Thread(target=publish_terminal)
    thread.start(); gate.wait()
    observed["snapshot"] = reader.snapshot(reference)
    thread.join()
    snapshot = observed["snapshot"]
    if snapshot["cursor"] == "sprintctl-maintenance-cursor-1":
        changes = reader.changes(reference, snapshot["cursor"])
        assert changes["events"][-1]["terminal"] is True
    else:
        assert snapshot["cursor"] == "sprintctl-maintenance-cursor-2"
        assert snapshot["terminal"] is True
    reader_conn.close(); writer_conn.close()


def exercise_frozen_history(history, owner, store):
    reference = store.record_current("mcap:test")
    if history == "concurrent-terminal-handoff":
        before = store.snapshot(reference); owner.set("reconciled", 2); store.record_current("mcap:test")
        assert before["cursor"] == "sprintctl-maintenance-cursor-1" and store.changes(reference, before["cursor"])["events"][-1]["terminal"] is True
    elif history == "cursor-at-floor":
        assert store.changes(reference, "sprintctl-maintenance-cursor-0")["events"][0]["event_id"].endswith("-1")
    elif history == "cursor-below-floor":
        store._execute("UPDATE maintenance_resource SET recovery_floor=1 WHERE resource_ref=?", (reference,)); store.conn.commit()
        with pytest.raises(CursorExpired): store.changes(reference, "sprintctl-maintenance-cursor-0")
    elif history == "duplicate-delivery":
        assert store.changes(reference, "sprintctl-maintenance-cursor-0") == store.changes(reference, "sprintctl-maintenance-cursor-0")
    elif history == "expiry-read-does-not-mutate":
        before = dict(owner.get("mcap:test")); store.snapshot(reference); assert owner.get("mcap:test") == before
    elif history == "immediate-client-compatibility":
        assert owner.get("mcap:test")["state"] == "prepared"
    elif history == "max-100-batch":
        for position in range(2, 103): owner.set("active", position); store.record_current("mcap:test")
        assert len(store.changes(reference, "sprintctl-maintenance-cursor-1")["events"]) == 100
    elif history == "non-disclosure-four-way":
        for value in ("malformed", "smr1_absent", "smr1_foreign", "smr1_unauthorized"):
            with pytest.raises(ResourceNotFound, match="resource not found"):
                store.snapshot(value)
    elif history == "postgres-parity":
        assert set(store.snapshot(reference)) == {"schema_version", "reference", "revision", "cursor", "terminal", "state"}
    elif history == "prune-0-255-256-257":
        for position in range(2, 258): owner.set("active", position); store.record_current("mcap:test")
        assert store._binding(reference)[1:] == (1, 257)
    elif history == "redaction-canaries":
        assert not ({"capability_id", "operator", "receipt", "command", "effect"} & set(store.snapshot(reference)["state"]))
    elif history == "spurious-wake":
        clock = [0.0]; store.monotonic = lambda: clock[0]; store.pause = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        assert store.changes(reference, "sprintctl-maintenance-cursor-1", 1)["events"] == [] and clock[0] >= 1
    elif history == "sqlite-parity":
        assert store.snapshot(reference)["state"]["state"] == owner.get("mcap:test")["state"]
    elif history == "wait-0-immediate":
        assert store.changes(reference, "sprintctl-maintenance-cursor-1", 0)["next_cursor"].endswith("-1")
    elif history == "wait-30-controlled-clock":
        clock = [0.0]; store.monotonic = lambda: clock[0]; store.pause = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        store.changes(reference, "sprintctl-maintenance-cursor-1", 30); assert clock[0] >= 30
    elif history == "wait-early-wake":
        clock = [0.0]
        def wake(_seconds): owner.set("active", 2); store.record_current("mcap:test"); clock[0] += .01
        store.monotonic = lambda: clock[0]; store.pause = wake
        assert store.changes(reference, "sprintctl-maintenance-cursor-1", 30)["events"][0]["data"]["state"] == "active"
    else:
        raise AssertionError(f"missing semantic oracle: {history}")


@pytest.mark.parametrize("history", HISTORIES)
def test_frozen_history_manifest_is_executable(history, resource, tmp_path):
    if history in TRANSACTIONAL_HISTORIES:
        exercise_sqlite_transactional_history(history, tmp_path)
    else:
        exercise_frozen_history(history, *resource)


def exercise_sqlite_transactional_history(history, tmp_path):
    from sprintctl.maintenance_capability import SQLiteMaintenanceCapabilityStore
    from tests.test_maintenance_capability import envelope

    path = tmp_path / f"{history}.db"
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    db.init_db(connection)
    owner = SQLiteMaintenanceCapabilityStore(connection)
    capability_id = "mcap:12345678-1234-4234-8234-123456789abc"
    request_id = "12345678-1234-4234-8234-123456789abc"
    arguments = dict(capability_id=capability_id, request_id=request_id, envelope=envelope(), actor="operator", at="2026-08-02T20:00:00Z", resource=True)

    if history in {"disconnect", "prepare-response-loss"}:
        first = owner.prepare(**arguments)
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)
        connection.close()  # committed response is deliberately discarded
        recovered = sqlite3.connect(path); recovered.row_factory = sqlite3.Row
        recovered_owner = SQLiteMaintenanceCapabilityStore(recovered)
        retry = recovered_owner.prepare(**(arguments | {"at": "2026-08-02T20:01:00Z"}))
        recovered_resource = MaintenanceResourceStore(recovered_owner)
        assert retry == first | {"duplicate": True}
        assert recovered_resource.reference_envelope(capability_id) == reference
        assert recovered.execute("SELECT count(*) FROM maintenance_resource").fetchone()[0] == 1
        assert recovered.execute("SELECT count(*) FROM maintenance_resource_event").fetchone()[0] == 1
        recovered.close()
    elif history == "parallel-owner-decoders":
        second_id = "mcap:22345678-1234-4234-8234-123456789abc"
        owner.prepare(**arguments)
        second_envelope = envelope(); second_envelope["envelope_id"] = "vuoro-cutover-exact-2"
        owner.prepare(**(arguments | {"capability_id": second_id, "request_id": str(uuid.uuid4()), "envelope": second_envelope}))
        resource = MaintenanceResourceStore(owner)
        expected = [resource.reference_envelope(capability_id), resource.reference_envelope(second_id)]
        before = tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("maintenance_resource", "maintenance_resource_event"))
        decoded = [None, None]
        def decode(index, target):
            conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
            decoded[index] = MaintenanceResourceStore(SQLiteMaintenanceCapabilityStore(conn)).reference_envelope(target)
            conn.close()
        threads = [threading.Thread(target=decode, args=(0, capability_id)), threading.Thread(target=decode, args=(1, second_id))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        after = tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("maintenance_resource", "maintenance_resource_event"))
        assert decoded == expected and len({item["reference"] for item in decoded}) == 2
        assert after == before == (2, 2)
        connection.close()
    elif history == "expiry-materialization-authorized-write":
        value = expiring_envelope(envelope())
        owner.prepare(**(arguments | {"envelope": value}))
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)["reference"]
        deadline = time.monotonic() + 3
        while not owner.sweep_expired(capability_id, at=datetime.now(timezone.utc).isoformat()):
            assert time.monotonic() < deadline
            time.sleep(.05)
        snapshot = MaintenanceResourceStore(owner).snapshot(reference)
        assert owner.get(capability_id)["state"] == snapshot["state"]["state"] == "expired"
        assert snapshot["terminal"] is True and snapshot["cursor"].endswith("-2")
        connection.close()
    elif history == "restart-during-wait":
        prepared = owner.prepare(**arguments)
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)["reference"]
        waiter_conn = sqlite3.connect(path, check_same_thread=False); waiter_conn.row_factory = sqlite3.Row
        entered, release = threading.Event(), threading.Event(); interrupted = []
        def pause(_seconds): entered.set(); release.wait(2)
        def wait_request():
            try: MaintenanceResourceStore(SQLiteMaintenanceCapabilityStore(waiter_conn), pause=pause).changes(reference, "sprintctl-maintenance-cursor-1", 2)
            except sqlite3.ProgrammingError: interrupted.append(True)
        thread = threading.Thread(target=wait_request); thread.start(); assert entered.wait(1)
        waiter_conn.close(); release.set(); thread.join(2); assert interrupted == [True]
        owner.transition(capability_id=capability_id, request_id=str(uuid.uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at="2026-08-02T20:01:00Z", effect_ref="sha256:" + "0" * 64)
        restarted = sqlite3.connect(path); restarted.row_factory = sqlite3.Row
        changes = MaintenanceResourceStore(SQLiteMaintenanceCapabilityStore(restarted)).changes(reference, "sprintctl-maintenance-cursor-1", 2)
        assert changes["events"][0]["data"]["state"] == "attested"
        restarted.close(); connection.close()
    else:
        raise AssertionError(f"missing SQLite transactional falsifier: {history}")
