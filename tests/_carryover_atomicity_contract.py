"""One retained semantic oracle for both real carryover backends."""
from dataclasses import dataclass
import json
import threading

import pytest

from sprintctl import contracts, maintain, maintaincore, trackcore, workitemcore


class InjectedCarryoverFailure(RuntimeError):
    pass


@dataclass
class CarryoverDriver:
    backend: object
    store: object
    source: int
    target: int
    item: int
    independent: object
    arm_wait: object = None
    await_wait: object = None

    def snapshot(self, store):
        m = self.backend
        return {
            "source": m.get_work_item(store, self.item),
            "target_tracks": m.list_tracks(store, self.target),
            "target_items": m.list_work_items(store, sprint_id=self.target),
            "source_events": sorted(m.list_events(store, self.source), key=lambda row: row["id"]),
            "target_events": sorted(m.list_events(store, self.target), key=lambda row: row["id"]),
            "decisions": m.list_decisions(store, self.item),
            "releases": m.list_releases(store, self.item),
            "reservations": [
                {key: value for key, value in row.items() if key not in {"stale", "activity_age_seconds"}}
                for row in m.list_reservations(store, self.item, active_only=False)
            ],
        }

    def connection(self, store):
        return getattr(store, "conn", store)

    def prepare(self, *, existing_track=False, release=False):
        if existing_track:
            self.backend.get_or_create_track(self.store, self.target, "carry track")
        if release:
            self.backend.reserve(self.store, self.item, actor="owner", session_id="carry-session")


class CarryoverAtomicityCases:
    def test_actual_blocked_writer_cannot_create_before_source_lock(self, carryover_driver):
        d = carryover_driver
        ready = threading.Event()
        holder = {}
        outcomes = []

        def competing():
            try:
                with d.independent() as other:
                    holder["wait"] = d.arm_wait(other)
                    ready.set()
                    outcomes.append(maintain.carryover(other, d.source, d.target, _m=d.backend))
            except Exception as error:
                outcomes.append(error)

        worker = threading.Thread(target=competing)
        try:
            with d.backend._carryover_transaction(d.store) as participant:
                assert participant.lock_item(d.item)["status"] == "pending"
                worker.start()
                assert ready.wait(timeout=5)
                # SQLite's traced BEGIN is attempted while the first writer
                # owns its lock; PostgreSQL proves an actual lock wait using
                # pg_blocking_pids on this disposable cluster.
                assert d.await_wait(d.store, holder["wait"])
                assert outcomes == []
                assert d.backend.list_work_items(d.store, sprint_id=d.target) == []
                copied = maintaincore.carryover_item(participant, d.source, d.target, d.item)
        finally:
            if worker.ident is not None:
                worker.join(timeout=10)
        assert not worker.is_alive()
        assert outcomes == [[]]
        [decision] = d.backend.list_decisions(d.store, d.item)
        assert decision["superseded_by_item_id"] == copied["id"]
        assert len(d.backend.list_work_items(d.store, sprint_id=d.target)) == 1

    def test_generic_event_admission_failure_rolls_back_whole_item(self, carryover_driver, monkeypatch):
        d = carryover_driver
        before = d.snapshot(d.store)
        original = contracts.require_generic_event_write_allowed
        seen = []

        def reject(kind):
            seen.append(kind)
            original(kind)
            if kind == "carried-out":
                raise ValueError("injected generic admission refusal")

        with monkeypatch.context() as injected:
            injected.setattr(contracts, "require_generic_event_write_allowed", reject)
            with pytest.raises(ValueError, match="generic admission"):
                maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        assert seen == ["carryover", "carried-out"]
        with d.independent() as other:
            assert d.snapshot(other) == before

    @pytest.mark.parametrize("target_status", ["planned", "active", "closed"])
    def test_target_status_compatibility_is_unchanged(self, carryover_driver, target_status):
        d = carryover_driver
        if target_status != "planned":
            d.backend.set_sprint_status(d.store, d.target, "active")
        if target_status == "closed":
            d.backend.set_sprint_status(d.store, d.target, "closed")
        [copied] = maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        assert copied["sprint_id"] == d.target
        assert d.backend.get_sprint(d.store, d.target)["status"] == target_status

    @pytest.mark.parametrize("existing_track", [False, True])
    @pytest.mark.parametrize("release", [False, True])
    @pytest.mark.parametrize("event_type", ["carryover", "item-decided", "carried-out"])
    def test_event_failure_rolls_back_copy_and_retry_moves_once(self, carryover_driver, monkeypatch, event_type, existing_track, release):
        d = carryover_driver
        d.prepare(existing_track=existing_track, release=release)
        before = d.snapshot(d.store)
        original = d.backend._insert_event

        def fail_event(store, sprint, actor, kind, *args, **kwargs):
            result = original(store, sprint, actor, kind, *args, **kwargs)
            if kind == event_type:
                raise InjectedCarryoverFailure(kind)
            return result

        with monkeypatch.context() as injected:
            injected.setattr(d.backend, "_insert_event", fail_event)
            with pytest.raises(InjectedCarryoverFailure, match=event_type):
                maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        with d.independent() as other:
            assert d.snapshot(other) == before
        [copied] = maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        assert len(d.backend.list_work_items(d.store, sprint_id=d.target)) == 1
        [decision] = d.backend.list_decisions(d.store, d.item)
        assert decision["kind"] == "supersede"
        assert decision["superseded_by_item_id"] == copied["id"]
        if release:
            assert decision["release_digest"] == before["releases"][0]["release_digest"]
        assert maintain.carryover(d.store, d.source, d.target, _m=d.backend) == []

    @pytest.mark.parametrize("existing_track", [False, True])
    @pytest.mark.parametrize("release", [False, True])
    @pytest.mark.parametrize("stage", ["track", "item", "decision", "result"])
    def test_participant_failure_restores_before_state(self, carryover_driver, monkeypatch, existing_track, release, stage):
        d = carryover_driver
        d.prepare(existing_track=existing_track, release=release)
        before = d.snapshot(d.store)
        module, name = {
            "track": (trackcore, "get_or_create_track"),
            "item": (workitemcore, "create_work_item"),
            "decision": (d.backend, "_decide_locked"),
            "result": (d.backend, "get_work_item"),
        }[stage]
        original = getattr(module, name)

        def fail_after(*args, **kwargs):
            result = original(*args, **kwargs)
            if stage != "result" or args[1] != d.item:
                raise InjectedCarryoverFailure(stage)
            return result

        with monkeypatch.context() as injected:
            injected.setattr(module, name, fail_after)
            with pytest.raises(InjectedCarryoverFailure, match=stage):
                maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        with d.independent() as other:
            assert d.snapshot(other) == before
        [copied] = maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        assert len(d.backend.list_work_items(d.store, sprint_id=d.target)) == 1
        assert d.backend.get_work_item(d.store, d.item)["terminal_decision_id"] == d.backend.list_decisions(d.store, d.item)[0]["id"]
        assert copied["status"] == "pending"

    @pytest.mark.parametrize("status", ["pending", "active", "blocked"])
    def test_success_retains_existing_fields_and_three_audit_events(self, carryover_driver, status):
        d = carryover_driver
        if status != "pending":
            d.backend.set_work_item_status(d.store, d.item, "active")
        if status == "blocked":
            d.backend.set_work_item_status(d.store, d.item, "blocked")
        before = d.snapshot(d.store)
        [copied] = maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        assert (copied["title"], copied["description"], copied["assignee"], copied["status"]) == (
            "Carry once", "Keep detail", "owner", "pending")
        assert copied["aggregate_uuid"] != before["source"]["aggregate_uuid"]
        assert d.backend.get_track(d.store, copied["track_id"])["name"] == "carry track"
        source = d.backend.get_work_item(d.store, d.item)
        assert (source["status"], source["resolution"]) == ("done", "superseded")
        after = d.snapshot(d.store)
        source_events = after["source_events"][len(before["source_events"]):]
        assert [e["event_type"] for e in source_events] == ["item-decided", "carried-out"]
        [target_event] = after["target_events"]
        assert target_event["event_type"] == "carryover"
        assert json.loads(target_event["payload"])["original_status"] == status
        assert source_events[0]["id"] < source_events[1]["id"]

    @pytest.mark.parametrize("change", ["terminal", "deleted", "moved", "edited"])
    def test_stale_selection_is_re_read_before_copy(self, carryover_driver, monkeypatch, change):
        d = carryover_driver
        original = d.backend.list_work_items
        changed = []

        def selected(store, *args, **kwargs):
            items = original(store, *args, **kwargs)
            if kwargs.get("sprint_id") == d.source and not changed:
                changed.append(True)
                with d.independent() as other:
                    if change == "terminal":
                        d.backend.record_decision(other, d.item, "withdraw", actor="other", rationale="Retired concurrently")
                    elif change == "edited":
                        item, revision = d.backend.get_work_item_with_edit_revision(other, d.item)
                        d.backend.update_work_item_description(other, d.item, "Current description", expected_revision=revision)
                    else:
                        conn = d.connection(other)
                        ph = "%s" if hasattr(other, "repo_id") else "?"
                        if change == "deleted":
                            sql, params = f"DELETE FROM work_item WHERE id={ph}", (d.item,)
                        else:
                            track = d.backend.get_or_create_track(other, d.target, "moved")
                            sql, params = f"UPDATE work_item SET sprint_id={ph},track_id={ph} WHERE id={ph}", (d.target, track, d.item)
                        conn.execute(sql, params)
                        conn.commit()
            return items

        with monkeypatch.context() as injected:
            injected.setattr(d.backend, "list_work_items", selected)
            result = maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        if change == "edited":
            assert len(result) == 1 and result[0]["description"] == "Current description"
        else:
            assert result == []
            assert d.backend.list_events(d.store, d.target) == []
            assert len(d.backend.list_work_items(d.store, sprint_id=d.target)) == int(change == "moved")

    def test_later_item_failure_preserves_earlier_commit_and_retry(self, carryover_driver, monkeypatch):
        d = carryover_driver
        first = d.backend.get_work_item(d.store, d.item)
        second = d.backend.create_work_item(d.store, d.source, first["track_id"], "Second item")
        original = d.backend._insert_event
        count = []

        def second_target_event(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == "carryover":
                count.append(True)
                if len(count) == 2:
                    raise InjectedCarryoverFailure("second item")
            return result

        with monkeypatch.context() as injected:
            injected.setattr(d.backend, "_insert_event", second_target_event)
            with pytest.raises(InjectedCarryoverFailure):
                maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        with d.independent() as other:
            assert d.backend.get_work_item(other, d.item)["resolution"] == "superseded"
            assert d.backend.get_work_item(other, second)["status"] == "pending"
            [earlier] = d.backend.list_work_items(other, sprint_id=d.target)
        [later] = maintain.carryover(d.store, d.source, d.target, _m=d.backend)
        assert earlier["id"] != later["id"]
        assert len(d.backend.list_work_items(d.store, sprint_id=d.target)) == 2

    @pytest.mark.parametrize("different_target", [False, True])
    def test_competing_carries_have_one_linked_copy(self, carryover_driver, monkeypatch, different_target):
        d = carryover_driver
        alternate = d.backend.create_sprint(d.store, "alternate target") if different_target else d.target
        original = d.backend.list_work_items
        selected = threading.Barrier(2)
        outcomes = []
        # Both independent callers have selected the same open source before
        # either enters the per-item writer transaction.
        def overlap(store, *args, **kwargs):
            rows = original(store, *args, **kwargs)
            if kwargs.get("sprint_id") == d.source:
                selected.wait(timeout=10)
            return rows

        def run(target):
            try:
                with d.independent() as store:
                    outcomes.append(maintain.carryover(store, d.source, target, _m=d.backend))
            except Exception as error:
                outcomes.append(error)

        with monkeypatch.context() as injected:
            injected.setattr(d.backend, "list_work_items", overlap)
            threads = [threading.Thread(target=run, args=(target,)) for target in (d.target, alternate)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            assert all(not thread.is_alive() for thread in threads)
        assert len(outcomes) == 2 and all(isinstance(row, list) for row in outcomes), outcomes
        assert sorted(map(len, outcomes)) == [0, 1]
        copies = {row["id"]: row for target in {d.target, alternate}
                  for row in d.backend.list_work_items(d.store, sprint_id=target)}
        [copy] = copies.values()
        [decision] = d.backend.list_decisions(d.store, d.item)
        assert decision["superseded_by_item_id"] == copy["id"]
