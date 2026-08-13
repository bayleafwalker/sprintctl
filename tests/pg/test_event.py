"""PostgreSQL integration tests: Event, Takeup.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    contracts,
    pg,
    _receipt_bytes,
    _receipt_payload,
    PG_MARKS,
    json,
)

pytestmark = PG_MARKS


class TestEvent:
    def test_generic_api_cannot_forge_close_boundary(self, store, sprint_id):
        with pytest.raises(ValueError, match="reserved; use the atomic sprint close"):
            pg.create_event(
                store,
                sprint_id,
                "forger",
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                payload={"previous_status": "active", "status": "closed"},
            )

    def test_capability_pointer_requires_closed_sprint_and_local_boundary(
        self,
        store,
        sprint_id,
    ):
        receipt_bytes = _receipt_bytes(store, sprint_id, 1)
        with pytest.raises(ValueError, match="requires a closed sprint"):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=_receipt_payload(store, receipt_bytes),
            )

    def test_create_and_list(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "hi"})
        events = pg.list_events(store, sprint_id)
        assert any(e["event_type"] == "note" for e in events)

    def test_payload_is_string(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "str-check"})
        events = pg.list_events(store, sprint_id)
        note = next(e for e in reversed(events) if e["event_type"] == "note")
        assert isinstance(note["payload"], str)
        assert json.loads(note["payload"])["summary"] is not None

    def test_capability_receipt_pointer_matches_sqlite_contract(
        self,
        store,
        sprint_id,
        monkeypatch,
    ):
        boundary_event_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_event_id)
        payload = _receipt_payload(store, receipt_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: receipt_bytes,
        )

        event_id = pg.create_event(
            store,
            sprint_id,
            "drafting-agent",
            "capability-receipt-drafted",
            payload=payload,
        )

        event = next(
            event
            for event in pg.list_events(store, sprint_id)
            if event["id"] == event_id
        )
        assert json.loads(event["payload"]) == payload

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda payload: payload.update(receipt_id="other.receipt"), "must start"),
            (lambda payload: payload.update(receipt_path="/tmp/receipt.json"), "must be exactly"),
            (lambda payload: payload.update(receipt_sha256="A" * 64), "lowercase hexadecimal"),
            (lambda payload: payload.update(receipt_body={"private": True}), "unknown fields"),
            (
                lambda payload: payload.update(
                    project="other",
                    receipt_id="other.receipt",
                    receipt_path=(
                        "/projects/dev/_artifacts/other/capability/receipts/"
                        "other.receipt.json"
                    ),
                ),
                "owning repository",
            ),
        ],
    )
    def test_malformed_capability_pointer_matches_sqlite_rejection(
        self,
        store,
        sprint_id,
        mutate,
        message,
    ):
        receipt_bytes = _receipt_bytes(store, sprint_id, 1)
        payload = _receipt_payload(store, receipt_bytes)
        mutate(payload)

        with pytest.raises(ValueError, match=message):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )

    def test_capability_pointer_file_and_boundary_are_verified_before_insert(
        self,
        store,
        sprint_id,
        monkeypatch,
    ):
        boundary_event_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        wrong_revision_bytes = _receipt_bytes(
            store,
            sprint_id,
            boundary_event_id,
            boundary={
                "kind": "sprint-close",
                "ref": {
                    "kind": "sprint-event",
                    "source": f"sprintctl:{store.repo_id}:sprint:{sprint_id}",
                    "revision": f"event:{boundary_event_id + 1}",
                },
            },
        )
        payload = _receipt_payload(store, wrong_revision_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: wrong_revision_bytes,
        )

        with pytest.raises(ValueError, match="boundary.ref.revision"):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )
        assert not any(
            event["event_type"] == contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
            for event in pg.list_events(store, sprint_id)
        )

    def test_list_events_limited(self, store, sprint_id):
        for _ in range(4):
            pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                            payload={"summary": "x"})
        limited = pg.list_events_limited(store, sprint_id, limit=2)
        assert len(limited) == 2

    def test_list_knowledge_candidates(self, store, sprint_id, work_item_id):
        pg.create_event(store, sprint_id, "ag", "decision", source_type="actor",
                        work_item_id=work_item_id, payload={"summary": "chose X"})
        candidates = pg.list_knowledge_candidates(store, sprint_id)
        assert any(c["event_type"] == "decision" for c in candidates)
        assert all(isinstance(c["payload"], dict) for c in candidates)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

class TestTakeup:
    def test_list_takeup_history_structure(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "sprint-taken-up", source_type="actor",
                        payload={"summary": "up", "detail": None, "actor_kind": "agent",
                                 "hostname": "h", "pid": 1, "instance_id": "i1",
                                 "runtime_session_id": None})
        history = pg.list_takeup_history(store, sprint_id)
        assert "active_takeups" in history
        assert isinstance(history["active_takeups"], list)

    def test_list_active_takeups(self, store, sprint_id):
        result = pg.list_active_takeups(store, sprint_id)
        assert isinstance(result, list)
