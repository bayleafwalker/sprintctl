"""Actual-backend compatibility examples for the existing explicit close."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from sprintctl import contracts, db, maintain


@dataclass
class ExplicitCloseDriver:
    backend: object
    store: object
    sprint: int
    independent: object
    abort_boundary: object
    arm_wait: object
    await_wait: object
    isolation: str

    def close(self, store=None, *, actor="operator", revision=None, sprint=None):
        return self.backend.close_sprint_with_boundary_event(
            self.store if store is None else store,
            self.sprint if sprint is None else sprint,
            actor, expected_revision=revision)

    def basis(self):
        return db.sprint_status_revision(self.backend.get_sprint(self.store, self.sprint))

    def transaction_state(self):
        connection = getattr(self.store, "conn", self.store)
        if hasattr(connection, "in_transaction"):
            return {"in_transaction": connection.in_transaction}
        return {"transaction_status": connection.info.transaction_status.name}

    def assert_idle(self, state):
        assert state == ({"in_transaction": False} if self.backend.__name__.endswith(".db") else {"transaction_status": "IDLE"})

    def snapshot(self, store):
        return {"sprint": self.backend.get_sprint(store, self.sprint),
                "events": self.backend.list_events(store, self.sprint)}


class ExplicitSprintCloseCases:
    @pytest.mark.parametrize("with_basis", [False, True])
    def test_valid_close_exact_boundary_and_return(self, close_driver, with_basis):
        d = close_driver
        event_id = d.close(actor="  operator  ", revision=d.basis() if with_basis else None)
        assert type(event_id) is int
        with d.independent() as other:
            state = d.snapshot(other)
        assert state["sprint"]["status"] == "closed"
        [event] = state["events"]
        assert event["id"] == event_id and event["sprint_id"] == d.sprint
        assert event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
        assert event["actor"] == "operator" and event["source_type"] == "actor"
        assert event["work_item_id"] is None
        assert json.loads(event["payload"]) == {"previous_status": "active", "status": "closed"}
        assert isinstance(state["sprint"]["created_at"], str)
        assert isinstance(event["created_at"], str) and isinstance(event["payload"], str)

    @pytest.mark.parametrize("actor", [None, "", " \t"])
    def test_actor_error_precedes_bad_basis_and_missing_sprint(self, close_driver, actor):
        d = close_driver
        before = d.snapshot(d.store)
        with pytest.raises(ValueError) as caught:
            d.close(actor=actor, revision="bad", sprint=-1)
        assert str(caught.value) == "actor must be a non-empty string"
        assert d.snapshot(d.store) == before

    def test_basis_validation_precedes_missing_sprint(self, close_driver):
        d = close_driver
        before = d.snapshot(d.store)
        with pytest.raises(ValueError) as caught:
            d.close(revision="bad", sprint=-1)
        assert str(caught.value) == "expected_revision must be a valid sprint status revision"
        assert d.snapshot(d.store) == before

    def test_missing_sprint(self, close_driver):
        d = close_driver
        with pytest.raises(ValueError) as caught:
            d.close(revision=d.basis(), sprint=-1)
        assert str(caught.value) == "Sprint #-1 not found"

    def test_planned_sprint_cannot_close(self, close_driver):
        d = close_driver
        planned = d.backend.create_sprint(d.store, "planned close oracle")
        with pytest.raises(db.InvalidTransition, match="sprint planned -> closed"):
            d.close(sprint=planned)
        assert d.backend.get_sprint(d.store, planned)["status"] == "planned"
        assert d.backend.list_events(d.store, planned) == []

    @pytest.mark.parametrize("basis", ["none", "current", "stale"])
    def test_closed_basis_precedence_and_no_response_replay(self, close_driver, basis):
        d = close_driver
        old = d.basis()
        original = d.close()
        with d.independent() as other:
            before = d.snapshot(other)
        revision = None if basis == "none" else d.basis() if basis == "current" else old
        expected = db.StatusConflict if basis == "stale" else db.InvalidTransition
        fragment = "status revision mismatch" if basis == "stale" else "sprint closed -> closed"
        with pytest.raises(expected, match=fragment):
            d.close(revision=revision)
        with d.independent() as other:
            assert d.snapshot(other) == before
        assert [row["id"] for row in before["events"]] == [original]

    def test_actual_boundary_insert_failure_rolls_back_and_retry_closes_once(self, close_driver, record_property, request):
        d = close_driver
        before = d.snapshot(d.store)
        with d.abort_boundary():
            with pytest.raises(Exception, match="injected explicit boundary fault"):
                d.close(revision=d.basis())
            # Observe before the trigger fixture's cleanup can roll anything back.
            transaction = d.transaction_state()
            d.assert_idle(transaction)
            owner_failed = d.snapshot(d.store)
            with d.independent() as other:
                failed = d.snapshot(other)
            assert owner_failed == failed == before
        result = d.close()
        with d.independent() as other:
            after = d.snapshot(other)
        assert after["sprint"]["status"] == "closed"
        assert [event["id"] for event in after["events"]] == [result]
        record_property("state_history", json.dumps({"history": "real-boundary-insert-abort-and-retry", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": [{"step": "before", "state": before}, {"step": "after-aborted-insert-before-fixture-cleanup", "transaction": transaction, "owner": owner_failed, "independent": failed}, {"step": "retry-returned", "event_id": result, "state": after}]}, sort_keys=True))

    def test_after_actual_boundary_insert_failure_rolls_back(self, close_driver, monkeypatch, record_property, request):
        d = close_driver
        before = d.snapshot(d.store)
        original = d.backend._insert_event
        inserted = []

        def fail_after(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE:
                inserted.append({"event_id": result, "transaction": d.transaction_state(), "owner": d.snapshot(args[0])})
                raise RuntimeError("injected after real boundary insert")
            return result

        with monkeypatch.context() as fault:
            fault.setattr(d.backend, "_insert_event", fail_after)
            with pytest.raises(RuntimeError, match="after real boundary"):
                d.close()
        assert len(inserted) == 1
        assert inserted[0]["owner"]["sprint"]["status"] == "closed"
        assert [event["id"] for event in inserted[0]["owner"]["events"]] == [inserted[0]["event_id"]]
        transaction = d.transaction_state()
        d.assert_idle(transaction)
        owner_failed = d.snapshot(d.store)
        with d.independent() as other:
            failed = d.snapshot(other)
        assert owner_failed == failed == before
        result = d.close()
        with d.independent() as other:
            final = d.snapshot(other)
            assert [event["id"] for event in final["events"]] == [result]
        record_property("state_history", json.dumps({"history": "after-real-boundary-insert-fault-and-retry", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": [{"step": "before", "state": before}, {"step": "actual-insert-before-injected-exception", **inserted[0]}, {"step": "after-product-rollback", "transaction": transaction, "owner": owner_failed, "independent": failed}, {"step": "retry-returned", "event_id": result, "state": final}]}, sort_keys=True))

    def test_typed_boundary_does_not_use_generic_admission(self, close_driver, monkeypatch):
        def reject_generic(kind):
            raise AssertionError("explicit typed writer reached generic gate")
        monkeypatch.setattr(contracts, "require_generic_event_write_allowed", reject_generic)
        assert type(close_driver.close()) is int

    def test_maintenance_auto_close_is_a_separate_event(self, close_driver):
        d = close_driver
        sprint = d.backend.create_sprint(d.store, "overdue close", start_date="2000-01-01", end_date="2000-01-02", status="active")
        result = maintain.sweep(d.store, sprint, datetime(2026, 10, 18, tzinfo=timezone.utc), threshold=timedelta(days=1), auto_close=True, _m=d.backend)
        assert result["auto_closed"] is True
        events = d.backend.list_events(d.store, sprint)
        assert [event["event_type"] for event in events] == ["auto-closed-overdue"]
        assert d.backend.get_sprint(d.store, sprint)["status"] == "closed"

    def test_actual_waiting_same_basis_close_has_one_winner(self, close_driver, monkeypatch, record_property, request):
        d = close_driver
        basis = d.basis()
        original = d.backend._insert_event
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        holders, outcomes, observations = {}, {}, []

        def pause_winner(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[3] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE and not entered.is_set():
                observations.append({"step": "winner-boundary-inserted-uncommitted", "event_id": result})
                entered.set()
                assert release.wait(timeout=10)
            return result

        def worker(label):
            try:
                with d.independent() as other:
                    holders[label] = other
                    if label == "loser":
                        holders["wait"] = d.arm_wait(other)
                        ready.set()
                    event = d.close(other, revision=basis)
                    outcomes[label] = event
                    observations.append({"step": label + "-returned", "event_id": event})
            except Exception as error:
                outcomes[label] = error
                observations.append({"step": label + "-refused", "type": type(error).__name__, "error": str(error)})

        winner = threading.Thread(target=worker, args=("winner",))
        loser = threading.Thread(target=worker, args=("loser",))
        with monkeypatch.context() as hook:
            hook.setattr(d.backend, "_insert_event", pause_winner)
            try:
                winner.start()
                assert entered.wait(timeout=5), outcomes
                loser.start()
                assert ready.wait(timeout=5)
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
        assert type(outcomes["winner"]) is int, outcomes
        assert isinstance(outcomes["loser"], db.StatusConflict), outcomes
        with d.independent() as other:
            final = d.snapshot(other)
        assert final["sprint"]["status"] == "closed"
        assert [event["id"] for event in final["events"]] == [outcomes["winner"]]
        observations.append({"step": "final-independent-read", "state": final})
        record_property("state_history", json.dumps({"history": "same-basis-close-winner-loser", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": observations}, sort_keys=True))
