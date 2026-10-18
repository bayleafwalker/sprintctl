"""Actual-backend oracle for public kind CAS and generic status, not CRUD."""
from dataclasses import dataclass
import json
import threading

import pytest

from sprintctl import db, sprintcore
from tests.test_sprint_kind import exercise_kind_changes, exercise_archive_activation_race


@dataclass
class SprintMutationDriver:
    backend: object
    store: object
    sprint: int
    independent: object
    open_store: object
    close_store: object
    abort_update: object
    arm_wait: object
    await_wait: object
    isolation: str

    def snapshot(self, store):
        return {"sprint": self.backend.get_sprint(store, self.sprint),
                "events": self.backend.list_events(store, self.sprint)}

    def basis(self, operation):
        row = self.backend.get_sprint(self.store, self.sprint)
        return sprintcore.sprint_kind_revision(row) if operation == "kind" else self.backend.sprint_status_revision(row)

    def invoke(self, operation, value, *, store=None, basis=None, sprint=None):
        fn = self.backend.set_sprint_kind if operation == "kind" else self.backend.set_sprint_status
        return fn(self.store if store is None else store, self.sprint if sprint is None else sprint,
                  value, expected_revision=basis)

    def transaction_state(self):
        conn = getattr(self.store, "conn", self.store)
        return {"in_transaction": conn.in_transaction} if hasattr(conn, "in_transaction") else {"transaction_status": conn.info.transaction_status.name}

    def assert_idle(self, actual):
        assert actual == ({"in_transaction": False} if self.backend.__name__.endswith(".db") else {"transaction_status": "IDLE"})


class SprintKindStatusCases:
    def test_existing_shared_kind_change_examples(self, mutation_driver):
        d = mutation_driver
        exercise_kind_changes(d.backend, d.store)

    def test_existing_archive_activation_race(self, mutation_driver):
        d = mutation_driver
        original = d.backend.get_sprint(d.store, d.sprint)
        exercise_archive_activation_race(d.backend, d.open_store, d.close_store, d.sprint, original)
        assert d.backend.list_events(d.store, d.sprint) == []

    @pytest.mark.parametrize("status", ["planned", "active", "closed"])
    def test_kind_exact_response_preserves_lifecycle_and_status_basis(self, mutation_driver, status):
        d = mutation_driver
        sprint = d.backend.create_sprint(d.store, "response " + status, status=status)
        before = d.backend.get_sprint(d.store, sprint)
        old_kind_basis = sprintcore.sprint_kind_revision(before)
        status_basis = d.backend.sprint_status_revision(before)
        result = d.invoke("kind", "backlog", sprint=sprint, basis=old_kind_basis)
        expected = {**before, "kind": "backlog"}
        expected["kind_revision"] = sprintcore.sprint_kind_revision(expected)
        assert result == expected
        assert result["kind_revision"] != old_kind_basis
        assert d.backend.sprint_status_revision(result) == status_basis
        assert d.backend.get_sprint(d.store, sprint) == {**before, "kind": "backlog"}
        assert d.backend.list_events(d.store, sprint) == []

    @pytest.mark.parametrize("case", ["id", "kind", "basis"])
    def test_kind_pure_validation_precedence(self, mutation_driver, case):
        d = mutation_driver
        before = d.snapshot(d.store)
        ident, kind, basis = ((True, "invalid", "bad") if case == "id" else
                              (999999999, "invalid", "bad") if case == "kind" else (999999999, "archive", "bad"))
        with pytest.raises(ValueError) as caught:
            d.invoke("kind", kind, sprint=ident, basis=basis)
        expected = {"id": "sprint_id must be a positive signed 64-bit integer",
                    "kind": "Invalid kind 'invalid'", "basis": "expected_revision must be a valid sprint kind revision"}
        assert str(caught.value) == expected[case]
        assert d.snapshot(d.store) == before

    @pytest.mark.parametrize("operation,value", [("kind", "archive"), ("status", "active")])
    def test_missing_sprint_uses_existing_lookup_error(self, mutation_driver, operation, value):
        d = mutation_driver
        ident = 999999999 if operation == "kind" else 0
        with pytest.raises(ValueError) as caught:
            d.invoke(operation, value, sprint=ident)
        assert str(caught.value) == f"Sprint #{ident} not found"

    def test_status_basis_prevalidation_precedes_missing_sprint(self, mutation_driver):
        with pytest.raises(ValueError, match="expected_revision must be a valid sprint status revision"):
            mutation_driver.invoke("status", "invalid", sprint=0, basis="bad")

    def test_generic_status_returns_none_and_closes_without_boundary(self, mutation_driver):
        d = mutation_driver
        assert d.invoke("status", "active", basis=d.basis("status")) is None
        assert d.invoke("status", "closed", basis=d.basis("status")) is None
        assert d.backend.get_sprint(d.store, d.sprint)["status"] == "closed"
        assert d.backend.list_events(d.store, d.sprint) == []

    @pytest.mark.parametrize("value", ["planned", "closed", "invalid"])
    def test_invalid_or_same_status_transition_is_not_a_noop(self, mutation_driver, value):
        d = mutation_driver
        before = d.snapshot(d.store)
        with pytest.raises(db.InvalidTransition) as caught:
            d.invoke("status", value, basis=d.basis("status"))
        assert str(caught.value) == f"cannot transition sprint planned -> {value}. Allowed: ['active']"
        assert d.snapshot(d.store) == before

    def test_stale_status_basis_precedes_transition_error(self, mutation_driver):
        d = mutation_driver
        stale = d.basis("status").replace("@status:planned", "@status:active")
        with pytest.raises(db.StatusConflict, match="status revision mismatch"):
            d.invoke("status", "invalid", basis=stale)
        assert d.backend.get_sprint(d.store, d.sprint)["status"] == "planned"
        assert d.backend.list_events(d.store, d.sprint) == []

    def test_archive_transition_error_precedes_archive_activation_rule(self, mutation_driver):
        d = mutation_driver
        d.invoke("kind", "archive")
        with pytest.raises(db.InvalidTransition, match="sprint planned -> closed"):
            d.invoke("status", "closed")
        with pytest.raises(db.InvalidTransition) as caught:
            d.invoke("status", "active")
        assert str(caught.value) == f"cannot activate sprint {d.sprint}: kind is archive"
        active_archive = d.backend.create_sprint(d.store, "active archive", status="active", kind="archive")
        assert d.invoke("status", "closed", sprint=active_archive) is None
        assert d.backend.get_sprint(d.store, active_archive)["kind"] == "archive"
        assert d.backend.list_events(d.store, active_archive) == []

    @pytest.mark.parametrize("operation,value", [("kind", "backlog"), ("kind", "active_sprint"), ("status", "active")])
    @pytest.mark.parametrize("timing", ["BEFORE", "AFTER"])
    def test_real_update_fault_rolls_back_before_fixture_cleanup_and_retry(self, mutation_driver, operation, value, timing, record_property, request):
        d = mutation_driver
        basis = d.basis(operation)
        before = d.snapshot(d.store)
        with d.abort_update(operation, timing):
            with pytest.raises(Exception, match="injected sprint update fault"):
                d.invoke(operation, value, basis=basis)
            transaction = d.transaction_state()
            d.assert_idle(transaction)
            owner = d.snapshot(d.store)
            with d.independent() as other:
                durable = d.snapshot(other)
            assert owner == durable == before
        result = d.invoke(operation, value, basis=basis)
        with d.independent() as other:
            final = d.snapshot(other)
        assert final["sprint"][operation] == value and final["events"] == []
        assert (isinstance(result, dict) if operation == "kind" else result is None)
        record_property("state_history", json.dumps({"history": f"{operation}-{value}-{timing.lower()}-update-abort-retry", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": [{"step": "before", "state": before}, {"step": "after-product-error-before-fixture-cleanup", "transaction": transaction, "owner": owner, "independent": durable}, {"step": "retry-returned", "result": result, "independent": final}]}, sort_keys=True))

    def test_kind_result_fault_after_actual_update_rolls_back(self, mutation_driver, monkeypatch, record_property, request):
        d = mutation_driver
        before = d.snapshot(d.store)
        basis = d.basis("kind")
        original, inserted = sprintcore.sprint_kind_revision, []

        def fail_result(row):
            if row["id"] == d.sprint and row["kind"] == "backlog":
                owner = d.snapshot(d.store)
                assert owner["sprint"]["kind"] == "backlog"
                inserted.append({"transaction": d.transaction_state(), "owner": owner})
                raise RuntimeError("injected kind result construction fault")
            return original(row)

        with monkeypatch.context() as fault:
            fault.setattr(sprintcore, "sprint_kind_revision", fail_result)
            with pytest.raises(RuntimeError, match="kind result construction"):
                d.invoke("kind", "backlog", basis=basis)
        assert len(inserted) == 1
        transaction = d.transaction_state()
        d.assert_idle(transaction)
        owner = d.snapshot(d.store)
        with d.independent() as other:
            durable = d.snapshot(other)
        assert owner == durable == before
        result = d.invoke("kind", "backlog", basis=basis)
        with d.independent() as other:
            final = d.snapshot(other)
        assert final["sprint"]["kind"] == "backlog" and final["events"] == []
        record_property("state_history", json.dumps({"history": "kind-result-construction-fault", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "observations": [{"step": "before", "state": before}, {"step": "actual-update-before-result-exception", **inserted[0]}, {"step": "after-product-rollback", "transaction": transaction, "owner": owner, "independent": durable}, {"step": "retry-returned", "result": result, "independent": final}]}, sort_keys=True))

    @pytest.mark.parametrize("operation,value", [("kind", "backlog"), ("kind", "active_sprint"), ("status", "active")])
    def test_observable_competing_cas_and_same_kind_basis(self, mutation_driver, monkeypatch, operation, value, record_property, request):
        d = mutation_driver
        same_kind = operation == "kind" and value == "active_sprint"
        basis = d.basis(operation)
        hook_module = sprintcore if operation == "kind" else d.backend
        hook_name = "sprint_kind_revision" if operation == "kind" else "sprint_status_revision"
        original = getattr(hook_module, hook_name)
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        local, holders, outcomes, observations = threading.local(), {}, {}, []

        def pause_locked_basis(row):
            revision = original(row)
            if row["id"] == d.sprint and hasattr(local, "store") and not entered.is_set():
                observations.append({"step": "winner-locked-basis-read", "owner": d.snapshot(local.store), "basis": revision})
                entered.set()
                assert release.wait(timeout=10)
            return revision

        def worker(label):
            try:
                with d.independent() as other:
                    local.store, holders[label] = other, other
                    if label == "loser":
                        holders["wait"] = d.arm_wait(other)
                        ready.set()
                    result = d.invoke(operation, value, store=other, basis=basis)
                    outcomes[label] = {"result": result}
                    observations.append({"step": label + "-returned", "result": result})
            except Exception as error:
                outcomes[label] = {"error": error}
                observations.append({"step": label + "-refused", "type": type(error).__name__, "error": str(error)})

        winner = threading.Thread(target=worker, args=("winner",))
        loser = threading.Thread(target=worker, args=("loser",))
        with monkeypatch.context() as hook:
            hook.setattr(hook_module, hook_name, pause_locked_basis)
            try:
                winner.start()
                assert entered.wait(timeout=5), outcomes
                loser.start()
                assert ready.wait(timeout=5)
                assert d.await_wait(d.store, holders["wait"], holders["winner"]), outcomes
                assert "loser" not in outcomes
                wait = {"step": "second-caller-wait-observed", "evidence": "SQLite BEGIN IMMEDIATE attempted while first holds writer lock" if d.backend.__name__.endswith(".db") else "pg_blocking_pids includes first caller"}
                if not d.backend.__name__.endswith(".db"):
                    wait.update({"first_pid": holders["winner"].conn.info.backend_pid, "second_pid": holders["wait"]})
                observations.append(wait)
            finally:
                release.set()
                winner.join(timeout=10)
                if loser.ident is not None:
                    loser.join(timeout=10)
        assert not winner.is_alive() and not loser.is_alive()
        assert "result" in outcomes["winner"], outcomes
        if same_kind:
            assert "result" in outcomes["loser"], outcomes
            assert outcomes["winner"] == outcomes["loser"]
        else:
            assert isinstance(outcomes["loser"].get("error"), db.StatusConflict), outcomes
        with d.independent() as other:
            final = d.snapshot(other)
        assert final["sprint"][operation] == value and final["events"] == []
        if same_kind:
            assert sprintcore.sprint_kind_revision(final["sprint"]) == basis
        observations.append({"step": "final-independent-read", "state": final})
        record_property("state_history", json.dumps({"history": f"{operation}-{value}-competing-current-basis", "backend": d.backend.__name__, "node": request.node.nodeid, "isolation": d.isolation, "basis_unchanged_same_kind": same_kind, "limits": "State basis only, no monotonic or ABA guarantee", "observations": observations}, sort_keys=True))

    def test_kind_basis_includes_status_but_status_basis_excludes_kind(self, mutation_driver):
        d = mutation_driver
        original_kind_basis = d.basis("kind")
        assert d.invoke("status", "active", basis=d.basis("status")) is None
        with pytest.raises(db.StatusConflict, match="kind revision mismatch"):
            d.invoke("kind", "backlog", basis=original_kind_basis)
        status_basis = d.basis("status")
        d.invoke("kind", "archive", basis=d.basis("kind"))
        assert d.basis("status") == status_basis
        assert d.invoke("status", "closed", basis=status_basis) is None
        assert d.backend.list_events(d.store, d.sprint) == []
