"""One behavioral oracle reused against real SQLite and PostgreSQL drivers."""
from dataclasses import dataclass
import json

import pytest


@dataclass
class ReservationDriver:
    api: object
    handle: object
    sprint_id: int
    item_id: int
    independent: object

    def reserve(self, **kwargs):
        return self.api.reserve(self.handle, self.item_id, actor="operator",
                                session_id="session", **kwargs)

    def snapshot(self):
        # An independent connection observes only durable state, including
        # original timestamps. Display-only staleness/age is deliberately omitted.
        with self.independent() as handle:
            reservations = self.api.list_reservations(handle, self.item_id, active_only=False)
            stable = [{k: v for k, v in row.items() if k not in ("stale", "activity_age_seconds")}
                      for row in reservations]
            events = [e for e in self.api.list_events(handle, self.sprint_id)
                      if e["work_item_id"] == self.item_id]
            return {"reservations": sorted(stable, key=lambda r: r["id"]),
                    "releases": self.api.list_releases(handle, self.item_id),
                    "events": sorted(events, key=lambda e: e["id"])}

    def fail_after_event(self, monkeypatch, position):
        original = self.api._insert_event
        seen = []

        def insert(*args, **kwargs):
            result = original(*args, **kwargs)
            event_type = args[3] if len(args) > 3 else kwargs["event_type"]
            if event_type.startswith("reservation."):
                seen.append(event_type)
                if len(seen) == position:
                    raise RuntimeError("injected lifecycle insert failure")
            return result

        monkeypatch.setattr(self.api, "_insert_event", insert)
        return seen


class ReservationAtomicityCases:
    @pytest.mark.parametrize("existing_release", [False, True])
    def test_initial_event_failure_is_fully_rolled_back(self, reservation_driver, monkeypatch, existing_release):
        d = reservation_driver
        if existing_release:
            old = d.reserve()
            d.api.release_reservation(d.handle, old["id"], actor="operator")
        before = d.snapshot()
        seen = d.fail_after_event(monkeypatch, 1)
        with pytest.raises(RuntimeError, match="injected lifecycle"):
            d.reserve()
        assert seen == ["reservation.reserved"]
        assert d.snapshot() == before

    @pytest.mark.parametrize("new_release", [False, True])
    @pytest.mark.parametrize("failure_position", [1, 2, 3])
    def test_takeover_failure_restores_all_prior_state(self, reservation_driver, monkeypatch, new_release, failure_position):
        d = reservation_driver
        d.reserve()
        d.reserve()
        d.reserve(role="observation")
        d.reserve(role="verification")
        if new_release:
            d.api.add_ref(d.handle, d.item_id, "doc", "docs/new-context.md")
        before = d.snapshot()
        seen = d.fail_after_event(monkeypatch, failure_position)
        with pytest.raises(RuntimeError, match="injected lifecycle"):
            d.reserve(interrupt_existing=True)
        assert len(seen) == failure_position
        assert d.snapshot() == before

    def test_successful_takeover_preserves_non_execution_and_event_contract(self, reservation_driver):
        d = reservation_driver
        execution = [d.reserve(), d.reserve()]
        preserved = [d.reserve(role="observation"), d.reserve(role="verification")]
        before = d.snapshot()
        result = d.reserve(interrupt_existing=True)
        after = d.snapshot()
        states = {r["id"]: r["state"] for r in after["reservations"]}
        assert all(states[r["id"]] == "interrupted" for r in execution)
        assert all(states[r["id"]] == "active" for r in preserved)
        assert states[result["id"]] == "active"
        assert {r["id"] for r in result["conflicting_reservations"]} == {r["id"] for r in preserved}
        assert result["conflict"] is True
        assert result["conflict_severity"] == "informational"
        events = after["events"][len(before["events"]):]
        assert [e["event_type"] for e in events] == ["reservation.interrupted", "reservation.interrupted", "reservation.reserved"]
        for event, old in zip(events[:2], execution):
            assert json.loads(event["payload"]) == {"reservation_id": old["id"], "reason": "explicit-takeover", "replacement_id": result["id"]}
        payload = json.loads(events[-1]["payload"])
        assert payload["reservation_id"] == result["id"]
        assert payload["release_digest"] == result["release_digest"]
        assert set(payload["conflicting_reservation_ids"]) == {r["id"] for r in preserved}
        assert len(after["releases"]) == 1

    def test_lost_unkeyed_response_does_not_undo_or_deduplicate_commit(self, reservation_driver):
        d = reservation_driver
        d.reserve()  # Caller loses the response after commit.
        committed = d.snapshot()
        assert len(committed["reservations"]) == len(committed["releases"]) == 1
        assert len(committed["events"]) == 1
        repeated = d.reserve()
        assert repeated["conflict"] is True
        assert len(d.snapshot()["reservations"]) == 2
        assert len(d.snapshot()["releases"]) == 1
