"""Shared real-backend examples for one candidate's item/link transaction."""
from dataclasses import dataclass
import json

import pytest


@dataclass
class BacklogSeedDriver:
    backend: object
    store: object
    source: int
    target: int
    item: int
    independent: object
    abort_link: object
    arm_wait: object = None
    await_wait: object = None
    hold_sprints: object = None
    mutate_event: object = None
    isolation: str = ""

    def candidate(self, summary="candidate", kind="pattern-noted", **kwargs):
        return self.backend.create_event(self.store, self.source, "author", event_type=kind,
                                         work_item_id=self.item, payload={"summary": summary}, **kwargs)

    def seed(self, store=None, **kwargs):
        return self.backend.backlog_seed_from_candidates(self.store if store is None else store,
                                                         self.source, self.target, **kwargs)

    def snapshot(self, store):
        return {"tracks": self.backend.list_tracks(store, self.target),
                "items": self.backend.list_work_items(store, sprint_id=self.target),
                "events": self.backend.list_events(store, self.target)}


class BacklogSeedAtomicityCases:
    def test_actual_link_insert_abort_leaves_no_copy_and_retry_seeds_once(self, seed_driver):
        d = seed_driver
        candidate = d.candidate()
        before = d.snapshot(d.store)
        with d.abort_link():
            with pytest.raises(Exception, match="injected backlog linkage fault"):
                d.seed()
        with d.independent() as other:
            failed = d.snapshot(other)
        retry = d.seed()
        with d.independent() as other:
            final = d.snapshot(other)
        trace = {"source_event_id": candidate, "before": before, "after_failure": failed,
                 "retry_return_count": len(retry), "after_retry": final}
        assert failed == before, json.dumps(trace, default=str, sort_keys=True)
        assert len(retry) == 1
        assert len(final["items"]) == 1
        assert len(final["events"]) == 1
        assert d.seed() == []

    @pytest.mark.parametrize("stage", ["after-link", "result"])
    def test_injected_failure_restores_candidate_and_retry_once(self, seed_driver, monkeypatch, stage):
        d = seed_driver
        d.candidate()
        before = d.snapshot(d.store)
        name = "_insert_event" if stage == "after-link" else "get_work_item"
        original = getattr(d.backend, name)

        def fail_after(*args, **kwargs):
            result = original(*args, **kwargs)
            matches = args[3] == "backlog-seeded" if stage == "after-link" else result and result["sprint_id"] == d.target
            if matches:
                raise RuntimeError("injected candidate " + stage)
            return result

        with monkeypatch.context() as fault:
            fault.setattr(d.backend, name, fail_after)
            with pytest.raises(RuntimeError, match="injected candidate"):
                d.seed()
        with d.independent() as other:
            assert d.snapshot(other) == before
        assert len(d.seed()) == 1
        assert d.seed() == []

    def test_earlier_candidate_remains_committed_on_later_failure(self, seed_driver, monkeypatch):
        d = seed_driver
        first, second = d.candidate("first"), d.candidate("second")
        original = d.backend._insert_event

        def fail_second(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == "backlog-seeded" and kwargs["payload"]["source_event_id"] == second:
                raise RuntimeError("injected second candidate")
            return result

        with monkeypatch.context() as fault:
            fault.setattr(d.backend, "_insert_event", fail_second)
            with pytest.raises(RuntimeError, match="second candidate"):
                d.seed()
        with d.independent() as other:
            state = d.snapshot(other)
        assert [row["title"] for row in state["items"]] == ["[knowledge] first"]
        assert [json.loads(row["payload"])["source_event_id"] for row in state["events"]] == [first]
        assert [row["title"] for row in d.seed()] == ["[knowledge] second"]
        assert d.seed() == []

    def test_exact_order_defaults_and_source_linkage(self, seed_driver):
        d = seed_driver
        first = d.candidate("first", kind="decision")
        second = d.backend.create_event(d.store, d.source, "author", event_type="lesson-learned", payload={})
        rows = d.seed(actor="seed owner")
        assert [row["title"] for row in rows] == ["[knowledge] first", f"[knowledge] Knowledge item from event #{second}"]
        assert all(row["status"] == "pending" and row["sprint_id"] == d.target for row in rows)
        assert all(row["description"] == "" and row["assignee"] is None and row["priority"] is None for row in rows)
        assert all(d.backend.get_track(d.store, row["track_id"])["name"] == "knowledge" for row in rows)
        events = d.backend.list_events(d.store, d.target)
        assert len(events) == 2
        for event, row, event_id, source_item, kind in zip(events, rows, [first, second], [d.item, None], ["decision", "lesson-learned"]):
            assert event["event_type"] == "backlog-seeded"
            assert event["actor"] == "seed owner" and event["source_type"] == "system"
            assert event["work_item_id"] == row["id"]
            assert json.loads(event["payload"]) == {"source_sprint_id": d.source, "source_item_id": source_item, "source_event_id": event_id, "source_event_type": kind}
        assert d.seed() == []

    @pytest.mark.parametrize("summary", [None, "", 3])
    def test_present_summary_is_not_replaced_by_fallback(self, seed_driver, summary):
        d = seed_driver
        d.candidate(summary)
        assert [row["title"] for row in d.seed()] == [f"[knowledge] {summary}"]

    def test_empty_candidates_do_not_create_track(self, seed_driver):
        d = seed_driver
        before = d.snapshot(d.store)
        assert d.seed() == []
        assert d.snapshot(d.store) == before

    def test_same_source_and_target_is_supported(self, seed_driver):
        d = seed_driver
        candidate = d.candidate()
        rows = d.backend.backlog_seed_from_candidates(d.store, d.source, d.source)
        assert len(rows) == 1 and rows[0]["sprint_id"] == d.source
        assert d.backend.backlog_seed_from_candidates(d.store, d.source, d.source) == []
        events = d.backend.list_events(d.store, d.source)
        links = [json.loads(e["payload"]) for e in events if e["event_type"] == "backlog-seeded"]
        assert [link["source_event_id"] for link in links] == [candidate]

    def test_same_event_can_seed_two_targets(self, seed_driver):
        d = seed_driver
        d.candidate()
        other = d.backend.create_sprint(d.store, "second target")
        [first] = d.seed()
        [second] = d.backend.backlog_seed_from_candidates(d.store, d.source, other)
        assert first["sprint_id"] == d.target and second["sprint_id"] == other
        assert first["id"] != second["id"]
        assert d.seed() == []
        assert d.backend.backlog_seed_from_candidates(d.store, d.source, other) == []

    @pytest.mark.parametrize("missing", ["source", "target", "both"])
    def test_missing_sprints_preserve_error_order(self, seed_driver, missing):
        d = seed_driver
        source = -1 if missing in {"source", "both"} else d.source
        target = -2 if missing in {"target", "both"} else d.target
        label = "Source" if missing in {"source", "both"} else "Target"
        before = d.snapshot(d.store)
        with pytest.raises(ValueError, match=label + " sprint"):
            d.backend.backlog_seed_from_candidates(d.store, source, target)
        assert d.snapshot(d.store) == before

    def test_generic_admission_failure_rolls_back_candidate(self, seed_driver, monkeypatch):
        from sprintctl import contracts
        d = seed_driver
        d.candidate()
        before = d.snapshot(d.store)
        original = contracts.require_generic_event_write_allowed

        def reject(kind):
            original(kind)
            if kind == "backlog-seeded":
                raise ValueError("injected seed admission refusal")

        with monkeypatch.context() as fault:
            fault.setattr(contracts, "require_generic_event_write_allowed", reject)
            with pytest.raises(ValueError, match="seed admission"):
                d.seed()
        with d.independent() as other:
            assert d.snapshot(other) == before
        assert len(d.seed()) == 1

    def test_all_linked_missing_track_does_not_create_track(self, seed_driver):
        d = seed_driver
        event = d.candidate()
        d.backend.create_event(d.store, d.target, "historical", event_type="backlog-seeded", payload={"source_event_id": event})
        before = d.snapshot(d.store)
        assert before["tracks"] == []
        assert d.seed() == []
        assert d.snapshot(d.store) == before

    def test_actual_waiting_seed_rechecks_winner_link(self, seed_driver, monkeypatch, record_property, request):
        import threading
        d = seed_driver
        d.candidate()
        entered, release, loser_ready = threading.Event(), threading.Event(), threading.Event()
        original = d.backend._insert_event
        holders, outcomes, observations = {}, {}, []

        def pause_winner(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == "backlog-seeded" and not entered.is_set():
                observations.append({"step": "winner-link-insert-returned", "item_id": kwargs["work_item_id"]})
                entered.set()
                assert release.wait(timeout=10), "winner release timed out"
            return result

        def worker(label):
            try:
                with d.independent() as other:
                    holders[label] = other
                    if label == "loser":
                        holders["wait"] = d.arm_wait(other)
                        loser_ready.set()
                    outcomes[label] = d.seed(other)
                    observations.append({"step": label + "-call-returned", "item_ids": [row["id"] for row in outcomes[label]]})
            except Exception as error:
                outcomes[label] = error

        winner = threading.Thread(target=worker, args=("winner",))
        loser = threading.Thread(target=worker, args=("loser",))
        with monkeypatch.context() as hook:
            hook.setattr(d.backend, "_insert_event", pause_winner)
            try:
                winner.start()
                assert entered.wait(timeout=5), outcomes
                loser.start()
                assert loser_ready.wait(timeout=5)
                assert d.await_wait(d.store, holders["wait"], holders["winner"]), outcomes
                assert "loser" not in outcomes
                wait = {"step": "loser-wait-observed", "evidence": "SQLite BEGIN IMMEDIATE attempted while winner uncommitted" if d.backend.__name__.endswith(".db") else "pg_blocking_pids includes winner"}
                if not d.backend.__name__.endswith(".db"):
                    wait.update({"winner_pid": holders["winner"].conn.info.backend_pid, "loser_pid": holders["wait"]})
                observations.append(wait)
            finally:
                release.set()
                winner.join(timeout=10)
                if loser.ident is not None:
                    loser.join(timeout=10)
        assert not winner.is_alive() and not loser.is_alive()
        assert isinstance(outcomes["winner"], list) and len(outcomes["winner"]) == 1, outcomes
        assert outcomes["loser"] == [], outcomes
        with d.independent() as other:
            state = d.snapshot(other)
        assert len(state["items"]) == len(state["events"]) == 1
        observations.append({"step": "final-independent-read", "items": len(state["items"]), "links": len(state["events"])})
        record_property("state_history", json.dumps({"history": "same-target-winner-loser", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))

    @pytest.mark.parametrize("change", ["deleted", "moved", "reclassified"])
    def test_selected_event_rechecked_after_actual_lock_wait(self, seed_driver, change, record_property, request):
        import threading
        d = seed_driver
        event = d.candidate()
        ready, outcomes, holder = threading.Event(), [], {}
        observations = []

        def worker():
            try:
                with d.independent() as other:
                    holder["wait"] = d.arm_wait(other)
                    ready.set()
                    outcomes.append(d.seed(other))
            except Exception as error:
                outcomes.append(error)

        thread = threading.Thread(target=worker)
        try:
            with d.hold_sprints():
                thread.start()
                assert ready.wait(timeout=5)
                assert d.await_wait(d.store, holder["wait"], d.store)
                observations.append({"step": "selected-reader-wait-observed", "evidence": "SQLite BEGIN IMMEDIATE attempted" if d.backend.__name__.endswith(".db") else "pg_blocking_pids includes holder"})
                d.mutate_event(event, change)
                observations.append({"step": "holder-event-change-before-commit", "change": change, "event_id": event})
        finally:
            if thread.ident is not None:
                thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcomes == [[]], outcomes
        observations.append({"step": "waiter-returned", "result": outcomes[0]})
        with d.independent() as other:
            assert d.backend.list_tracks(other, d.target) == []
            assert d.backend.list_work_items(other, sprint_id=d.target) == []
            assert not [e for e in d.backend.list_events(other, d.target) if e["event_type"] == "backlog-seeded"]
        observations.append({"step": "final-independent-read", "items": 0, "links": 0, "tracks": 0})
        record_property("state_history", json.dumps({"history": "selected-event-" + change, "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))

    def test_new_arrival_waits_for_next_invocation(self, seed_driver, monkeypatch):
        d = seed_driver
        d.candidate("initial")
        original = d.backend.list_knowledge_candidates
        arrivals = []

        def arrive_after_selection(store, source):
            selected = original(store, source)
            if not arrivals:
                arrivals.append(d.candidate("new arrival"))
            return selected

        with monkeypatch.context() as boundary:
            boundary.setattr(d.backend, "list_knowledge_candidates", arrive_after_selection)
            assert [row["title"] for row in d.seed()] == ["[knowledge] initial"]
        assert [row["title"] for row in d.seed()] == ["[knowledge] new arrival"]
        assert d.seed() == []

    def test_receipt_identity_is_not_coerced(self, seed_driver):
        d = seed_driver
        event = d.candidate()
        d.backend.create_event(d.store, d.target, "historical", event_type="backlog-seeded", payload={"source_event_id": str(event)})
        assert len(d.seed()) == 1
        assert d.seed() == []

    def test_opposite_sprint_directions_finish_without_lock_cycle(self, seed_driver, record_property, request):
        import threading
        d = seed_driver
        d.candidate("forward")
        d.backend.create_event(d.store, d.target, "author", event_type="pattern-noted", payload={"summary": "reverse"})
        barrier, outcomes, observations = threading.Barrier(2), [], []

        def worker(source, target):
            try:
                with d.independent() as other:
                    observations.append({"step": "caller-at-barrier", "source": source, "target": target})
                    barrier.wait(timeout=5)
                    rows = d.backend.backlog_seed_from_candidates(other, source, target)
                    outcomes.append(rows)
                    observations.append({"step": "call-returned", "source": source, "target": target, "item_ids": [row["id"] for row in rows]})
            except Exception as error:
                outcomes.append(error)

        threads = [threading.Thread(target=worker, args=pair) for pair in [(d.source, d.target), (d.target, d.source)]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert len(outcomes) == 2 and all(isinstance(rows, list) and len(rows) == 1 for rows in outcomes), outcomes
        assert sorted(rows[0]["title"] for rows in outcomes) == ["[knowledge] forward", "[knowledge] reverse"]
        with d.independent() as other:
            counts = []
            for sprint in [d.source, d.target]:
                links = [e for e in d.backend.list_events(other, sprint) if e["event_type"] == "backlog-seeded"]
                assert len(links) == 1
                copied = [d.backend.get_work_item(other, link["work_item_id"]) for link in links]
                assert all(row is not None and row["sprint_id"] == sprint for row in copied)
                counts.append({"sprint": sprint, "link_count": len(links), "seeded_item_count": len(copied)})
        observations.append({"step": "final-independent-link-counts", "sprints": counts})
        record_property("state_history", json.dumps({"history": "opposite-directions", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": observations, "scope": "actual calls finish; exact sorted lock statement separately source-inspected"}, sort_keys=True))

    def test_initial_malformed_receipt_errors_before_candidate_writes(self, seed_driver):
        d = seed_driver
        d.candidate("first")
        d.candidate("second")
        d.backend.create_event(d.store, d.target, "historical", event_type="backlog-seeded", payload={"source_event_id": []})
        before = d.snapshot(d.store)
        with pytest.raises(TypeError, match="unhashable"):
            d.seed()
        with d.independent() as other:
            assert d.snapshot(other) == before

    @pytest.mark.parametrize("label", ["Source", "Target"])
    def test_selected_sprint_disappearance_revalidated_after_wait(self, seed_driver, label, record_property, request):
        import threading
        d = seed_driver
        event = d.candidate()
        ready, outcomes, holder, observations = threading.Event(), [], {}, []

        def worker():
            try:
                with d.independent() as other:
                    holder["wait"] = d.arm_wait(other)
                    ready.set()
                    outcomes.append(d.seed(other))
            except Exception as error:
                outcomes.append(error)

        thread = threading.Thread(target=worker)
        try:
            with d.hold_sprints():
                thread.start()
                assert ready.wait(timeout=5)
                assert d.await_wait(d.store, holder["wait"], d.store)
                observations.append({"step": "selected-reader-wait-observed", "evidence": "SQLite BEGIN IMMEDIATE attempted" if d.backend.__name__.endswith(".db") else "pg_blocking_pids includes holder"})
                d.mutate_event(event, label.lower() + "-sprint-deleted")
                observations.append({"step": "holder-deleted-sprint-before-commit", "label": label})
        finally:
            if thread.ident is not None:
                thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(outcomes) == 1 and isinstance(outcomes[0], ValueError), outcomes
        assert str(outcomes[0]) == f"{label} sprint #{d.source if label == 'Source' else d.target} not found"
        with d.independent() as other:
            assert d.backend.list_tracks(other, d.target) == []
            assert d.backend.list_work_items(other, sprint_id=d.target) == []
            assert d.backend.list_events(other, d.target) == []
        observations.append({"step": "waiter-refused", "error": str(outcomes[0]), "target_tracks": 0, "target_items": 0, "target_links": 0})
        record_property("state_history", json.dumps({"history": label.lower() + "-sprint-disappeared", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))
