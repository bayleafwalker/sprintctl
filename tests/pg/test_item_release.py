"""PostgreSQL mirror of tests/test_item_release.py (agentops#2431).

Same active/blocked -> pending release transition, exercised against the
PostgreSQL backend directly (see tests/pg/_shared.py for the store/sprint_id/
track_id/work_item_id fixtures, skip machinery, and helpers).
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    authority,
    contracts,
    db,
    outbox,
    pg,
    InvalidTransition,
    _append_authority_command,
    _uid,
    PG_MARKS,
    json,
)

pytestmark = PG_MARKS


class TestReleaseTransitionTable:
    def test_active_to_pending_with_reason_succeeds(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        pg.set_work_item_status(store, work_item_id, "pending", reason="rework")
        assert pg.get_work_item(store, work_item_id)["status"] == "pending"

    def test_blocked_to_pending_with_reason_succeeds(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        pg.set_work_item_status(store, work_item_id, "blocked")
        pg.set_work_item_status(store, work_item_id, "pending", reason="abandoned")
        assert pg.get_work_item(store, work_item_id)["status"] == "pending"

    def test_pending_release_without_reason_rejected(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, work_item_id, "pending")
        assert pg.get_work_item(store, work_item_id)["status"] == "active"

    def test_pending_release_with_unknown_reason_rejected(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, work_item_id, "pending", reason="because")

    def test_pending_to_done_still_rejected(self, store, work_item_id):
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, work_item_id, "done")

    def test_done_stays_terminal(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        pg.record_decision(store, work_item_id, "accept", actor="closer")
        assert pg.get_work_item(store, work_item_id)["status"] == "done"
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, work_item_id, "pending", reason="rework")


class TestReleaseReservation:
    def test_release_drops_callers_reservation(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        reservation = pg.reserve(store, work_item_id, actor="worker", session_id="s-1")
        pg.set_work_item_status(
            store, work_item_id, "pending", reason="rework", session_id="s-1"
        )
        [row] = pg.list_reservations(store, work_item_id, active_only=False)
        assert row["id"] == reservation["id"]
        assert row["state"] == "released"

    def test_release_with_no_reservation_still_succeeds(self, store, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        pg.set_work_item_status(
            store, work_item_id, "pending", reason="rework", session_id="no-such-session"
        )
        assert pg.get_work_item(store, work_item_id)["status"] == "pending"


class TestReleaseEvent:
    def test_item_released_event_carries_reason_and_previous_status(
        self, store, sprint_id, work_item_id
    ):
        pg.set_work_item_status(store, work_item_id, "active")
        pg.set_work_item_status(
            store, work_item_id, "pending", reason="partial", actor="releaser"
        )
        events = pg.list_events(store, sprint_id)
        released = [e for e in events if e["event_type"] == "item-released"]
        assert len(released) == 1
        payload = json.loads(released[0]["payload"])
        assert payload["reason"] == "partial"
        assert payload["previous_status"] == "active"
        assert released[0]["actor"] == "releaser"


class TestReleaseReadyQueue:
    def test_released_item_lists_again_in_next_work(self, store, sprint_id, work_item_id):
        pg.set_work_item_status(store, work_item_id, "active")
        pg.set_work_item_status(store, work_item_id, "pending", reason="rework")
        ready = pg.get_ready_items(store, sprint_id)
        assert any(item["id"] == work_item_id for item in ready)


# ---------------------------------------------------------------------------
# Served path: authority.arbitrate_command over item.transition (agentops#2431)
# ---------------------------------------------------------------------------


class TestServedReleaseTransition:
    def test_active_to_pending_with_reason_applies_and_releases_reservation(
        self, store, sprint_id, work_item_id, tmp_path
    ):
        pg.set_work_item_status(store, work_item_id, "active")
        reservation = pg.reserve(
            store, work_item_id, actor="served-caller", session_id="served-session"
        )
        item = pg.get_work_item(store, work_item_id)
        producer = outbox.open_outbox(tmp_path / "release.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "pending", "reason": "rework"},
                actor="served-caller",
            )
            decision = authority.arbitrate_command(store, command)
        finally:
            producer.close()

        assert decision.accepted is True
        assert decision.effect["status"] == "pending"
        assert pg.get_work_item(store, work_item_id)["status"] == "pending"

        [row] = pg.list_reservations(store, work_item_id, active_only=False)
        assert row["id"] == reservation["id"]
        assert row["state"] == "released"

        events = pg.list_events(store, sprint_id)
        released = [e for e in events if e["event_type"] == "item-released"]
        assert len(released) == 1
        payload = json.loads(released[0]["payload"])
        assert payload["reason"] == "rework"
        assert payload["previous_status"] == "active"

    def test_pending_without_reason_is_rejected_by_the_contract(
        self, store, work_item_id, tmp_path
    ):
        pg.set_work_item_status(store, work_item_id, "active")
        item = pg.get_work_item(store, work_item_id)
        producer = outbox.open_outbox(tmp_path / "release-no-reason.db")
        try:
            with pytest.raises(ValueError, match="payload.reason is required"):
                _append_authority_command(
                    producer,
                    store,
                    record_type="item.transition",
                    aggregate_type="item",
                    aggregate_uuid=item["aggregate_uuid"],
                    basis_revision=authority.item_revision(item),
                    payload={"to_status": "pending"},
                )
        finally:
            producer.close()
        assert pg.get_work_item(store, work_item_id)["status"] == "active"

    def test_reason_on_a_non_pending_target_is_rejected_by_the_contract(
        self, store, work_item_id, tmp_path
    ):
        item = pg.get_work_item(store, work_item_id)
        producer = outbox.open_outbox(tmp_path / "release-non-pending.db")
        try:
            with pytest.raises(ValueError, match="only accepted when"):
                _append_authority_command(
                    producer,
                    store,
                    record_type="item.transition",
                    aggregate_type="item",
                    aggregate_uuid=item["aggregate_uuid"],
                    basis_revision=authority.item_revision(item),
                    payload={"to_status": "active", "reason": "rework"},
                )
        finally:
            producer.close()

    def test_item_done_with_a_reason_is_rejected_by_the_contract(
        self, store, work_item_id, tmp_path
    ):
        pg.set_work_item_status(store, work_item_id, "active")
        item = pg.get_work_item(store, work_item_id)
        producer = outbox.open_outbox(tmp_path / "release-done-reason.db")
        try:
            with pytest.raises(ValueError, match="only accepted when"):
                _append_authority_command(
                    producer,
                    store,
                    record_type="item.done",
                    aggregate_type="item",
                    aggregate_uuid=item["aggregate_uuid"],
                    basis_revision=authority.item_revision(item),
                    payload={"to_status": "done", "reason": "rework"},
                )
        finally:
            producer.close()
