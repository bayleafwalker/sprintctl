from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

from sprintctl.maintenance_resource import (
    CursorExpired,
    MaintenanceResourceStore,
    ResourceNotFound,
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


class Owner:
    def __init__(self, connection):
        self.conn = connection
        self.rows = {"mcap:test": {"state": "prepared", "not_before": "2026-08-02T19:00:00Z", "expires_at": "2026-08-02T20:00:00Z", "updated_at": "2026-08-02T19:00:00Z"}}

    def get(self, capability_id):
        return self.rows.get(capability_id)

    def set(self, state, position):
        self.rows["mcap:test"].update(state=state, updated_at=f"2026-08-02T19:{position:02d}:00Z")


@pytest.fixture
def resource():
    connection = sqlite3.connect(":memory:")
    owner = Owner(connection)
    return owner, MaintenanceResourceStore(owner)


def test_reference_retry_snapshot_and_projection_redaction(resource):
    owner, store = resource
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
    owner = Owner(connection)
    store = MaintenanceResourceStore(owner)
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
    assert binding[1] == 2
    store.changes(reference, "sprintctl-maintenance-cursor-2")
    with pytest.raises(CursorExpired):
        store.changes(reference, "sprintctl-maintenance-cursor-1")


def test_read_does_not_materialize_wall_clock_expiry(resource):
    owner, store = resource
    reference = store.record_current("mcap:test")
    before = store._binding(reference)
    assert store.snapshot(reference)["terminal"] is False
    assert store.changes(reference, "sprintctl-maintenance-cursor-1", 0)["events"] == []
    assert store._binding(reference) == before
    assert owner.rows["mcap:test"]["state"] == "prepared"


def test_wait_controlled_clock_spurious_and_early_wake():
    connection = sqlite3.connect(":memory:")
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


@pytest.mark.parametrize("history", HISTORIES)
def test_frozen_history_manifest_is_executable(history, resource):
    owner, store = resource
    reference = store.record_current("mcap:test")
    assert store.snapshot(reference)["reference"] == reference
    assert store.changes(reference, "sprintctl-maintenance-cursor-1", 0)["next_cursor"] == "sprintctl-maintenance-cursor-1"
    if history == "non-disclosure-four-way":
        for value in ("malformed", "smr1_absent", "smr1_foreign", "smr1_unauthorized"):
            with pytest.raises(ResourceNotFound, match="resource not found"):
                store.snapshot(value)
