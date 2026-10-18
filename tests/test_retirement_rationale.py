"""Fresh retirement admission, with historical request replay preserved."""
from __future__ import annotations

from contextlib import closing
import threading
from uuid import uuid4

import pytest

from sprintctl import db, decisions
from sprintctl.application import ApplicationRejection
from tests.test_served_decisions import _context
from tests.test_work_application import _application


def new_item(backend, store, status="pending"):
    sprint = backend.create_sprint(store, f"Retirement-{uuid4().hex[:8]}", status="active")
    track = backend.get_or_create_track(store, sprint, "retirement")
    item = backend.create_work_item(store, sprint, track, "Retire this work")
    if status in {"active", "blocked"}:
        backend.set_work_item_status(store, item, "active")
    if status == "blocked":
        backend.set_work_item_status(store, item, "blocked")
    successor = backend.create_work_item(store, sprint, track, "Replacement work")
    return sprint, item, successor


class RetirementRationaleContract:
    @pytest.mark.parametrize("kind", ["reject", "withdraw", "supersede"])
    def test_served_decision_boundary_refuses_blank_retirement(self, owner, owner_application, kind):
        backend, store = owner
        sprint, item, successor = new_item(backend, store)
        arguments = {"item_id": item, "kind": kind, "rationale": " \t", "evidence_digests": []}
        if kind == "supersede":
            arguments["superseded_by_item_id"] = successor
        events = backend.list_events(store, sprint)
        with pytest.raises(ApplicationRejection, match="non-empty rationale") as rejection:
            owner_application.invoke("work.decision.record", arguments, _context("reviewer", uuid4().hex))
        assert rejection.value.code == "invalid-transition"
        assert backend.get_work_item(store, item)["status"] == "pending"
        assert backend.list_decisions(store, item) == []
        assert backend.list_events(store, sprint) == events

    @pytest.mark.parametrize("kind", ["reject", "withdraw", "supersede"])
    @pytest.mark.parametrize("status", ["pending", "active", "blocked"])
    @pytest.mark.parametrize("rationale", [None, "", " \t\n"])
    def test_blank_fresh_retirement_is_refused_without_writes(self, owner, kind, status, rationale):
        backend, store = owner
        sprint, item, successor = new_item(backend, store, status)
        before = backend.get_work_item(store, item)
        events = backend.list_events(store, sprint)
        with pytest.raises(ValueError, match="non-empty rationale"):
            backend.record_decision_keyed(
                store, item, kind, actor="reviewer", rationale=rationale,
                superseded_by_item_id=successor if kind == "supersede" else None,
                idempotency_key=uuid4().hex,
            )
        assert backend.get_work_item(store, item) == before
        assert backend.list_decisions(store, item) == []
        assert backend.list_events(store, sprint) == events

    @pytest.mark.parametrize("kind", ["reject", "withdraw", "supersede"])
    @pytest.mark.parametrize("status", ["pending", "active", "blocked"])
    def test_meaningful_retirement_closes_every_open_state(self, owner, kind, status):
        backend, store = owner
        _sprint, item, successor = new_item(backend, store, status)
        rationale = "  Work is no longer required: café 東京.  "
        decision = backend.record_decision(
            store, item, kind, actor="reviewer", rationale=rationale,
            superseded_by_item_id=successor if kind == "supersede" else None,
        )
        current = backend.get_work_item(store, item)
        assert current["status"] == "done"
        assert current["resolution"] == decisions.resolution_for(kind)
        assert current["terminal_decision_id"] == decision["id"]
        assert decision["rationale"] == rationale

    @pytest.mark.parametrize("kind", ["reject", "withdraw", "supersede"])
    @pytest.mark.parametrize("rationale", ["", " \t"])
    def test_historical_blank_request_replays_before_new_admission(self, owner, monkeypatch, kind, rationale):
        backend, store = owner
        sprint, item, successor = new_item(backend, store)
        key = uuid4().hex
        arguments = dict(actor="reviewer", rationale=rationale,
                         superseded_by_item_id=successor if kind == "supersede" else None,
                         idempotency_key=key,
                         expected_revision=backend.item_status_revision(backend.get_work_item(store, item)))
        # Seed a historically valid request through the actual writer using its
        # pre-correction admission rule. No historical row is rewritten.
        with monkeypatch.context() as historical:
            historical.setattr(decisions, "decision_error", lambda row, value:
                decisions.transition_error(value["kind"], int(row["id"]), row["status"]))
            original, replayed = backend.record_decision_keyed(store, item, kind, **arguments)
        assert replayed is False
        events = backend.list_events(store, sprint)
        repeated, replayed = backend.record_decision_keyed(store, item, kind, **arguments)
        assert replayed is True
        assert repeated == original
        with pytest.raises(decisions.IdempotencyConflict):
            backend.record_decision_keyed(store, item, kind, **{**arguments, "rationale": "Changed"})
        assert backend.list_decisions(store, item) == [original]
        assert backend.list_events(store, sprint) == events

    def test_accept_alias_and_revise_keep_optional_rationale(self, owner):
        backend, store = owner
        _sprint, item, _successor = new_item(backend, store, "active")
        revised = backend.record_decision(store, item, "revise", actor="reviewer")
        assert revised["rationale"] == ""
        backend.set_work_item_status(store, item, "done", actor="reviewer")
        assert backend.get_work_item(store, item)["resolution"] == "accepted"
        assert backend.list_decisions(store, item)[-1]["rationale"] == ""

    @pytest.mark.parametrize("kind", ["reject", "withdraw", "supersede"])
    def test_concurrent_same_key_keeps_one_reasoned_retirement(self, owner, owner_factory, kind):
        backend, store = owner
        sprint, item, successor = new_item(backend, store)
        key = uuid4().hex
        barrier = threading.Barrier(2)
        outcomes = []

        def decide():
            with owner_factory() as independent:
                barrier.wait(timeout=10)
                try:
                    outcomes.append(backend.record_decision_keyed(
                        independent, item, kind, actor="reviewer",
                        rationale="Reviewed retirement", idempotency_key=key,
                        superseded_by_item_id=successor if kind == "supersede" else None,
                    ))
                except Exception as error:
                    outcomes.append(error)

        threads = [threading.Thread(target=decide) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
        assert len(outcomes) == 2 and all(isinstance(value, tuple) for value in outcomes), outcomes
        assert sorted(value[1] for value in outcomes) == [False, True]
        assert outcomes[0][0] == outcomes[1][0]
        assert backend.list_decisions(store, item) == [outcomes[0][0]]
        assert sum(event["event_type"] == "item-decided" for event in backend.list_events(store, sprint)) == 1


@pytest.fixture
def owner(conn):
    return db, conn


@pytest.fixture
def owner_factory(db_path):
    return lambda: closing(db.get_connection(db_path))


@pytest.fixture
def owner_application(conn):
    return _application(store=conn, backend=db)


class TestSQLiteRetirementRationale(RetirementRationaleContract):
    @pytest.mark.parametrize("rationale", ["", " \t"])
    def test_historical_blank_snapshot_remains_recoverable(self, conn, tmp_path, monkeypatch, rationale):
        from tests.test_decisions import _rows

        _sprint, item, _successor = new_item(db, conn)
        key = uuid4().hex
        arguments = dict(actor="reviewer", rationale=rationale, idempotency_key=key)
        with monkeypatch.context() as historical:
            historical.setattr(decisions, "decision_error", lambda row, value:
                decisions.transition_error(value["kind"], int(row["id"]), row["status"]))
            original = db.record_decision(conn, item, "withdraw", **arguments)
        snapshot = {
            table: _rows(conn, table, "event_id" if table == "work_legacy_evidence" else "id")
            for table in db._RECOVERY_TABLE_ORDER
        }
        with closing(db.get_connection(tmp_path / "recovered.db")) as recovered:
            db.init_db(recovered)
            db.write_recovery_snapshot(recovered, snapshot)
            assert db.list_decisions(recovered, item) == [original]
            assert db.record_decision_keyed(recovered, item, "withdraw", **arguments) == (original, True)
