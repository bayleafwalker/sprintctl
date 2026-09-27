"""PostgreSQL integration tests: the exclusive durable work lease
(agentops#2520, E2b: ``work.lease.acquire-v1`` / ``heartbeat-v1`` /
``complete-v1`` / ``read-v1``, schema 18).

Like tests/pg/test_run_evidence.py these go through ``WorkApplication.invoke``
so identity binding, the write-tool ledger and the rejection mapping are
covered, and they race real connections for the exclusivity claims.  Time is
moved by backdating ``heartbeat_at`` in the database, never by sleeping.
"""
from __future__ import annotations

import threading
import time
import uuid
from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl import pg, pg_migrations
from sprintctl.application import ApplicationRejection, WorkApplication
from sprintctl.maintenance_capability import (
    MaintenanceCapabilityError,
    PostgresMaintenanceCapabilityStore,
)
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

from tests.pg._shared import (
    AT,
    PG_MARKS,
    _PG_URL,
    _anchor_capability_window_to_db_clock,
    _uid,
    assert_disposable_connection,
    dict_row,
    envelope,
    psycopg,
)

pytestmark = PG_MARKS

N_RACERS = 8
_CONTRACTS = {contract.name: contract for contract in WORK_OPERATION_CONTRACTS}


def _context(principal_id: str = "github:1:0", *, workspace_id: str = "ws-1",
             client_id: str | None = None, grant_id: str | None = None):
    identity = SimpleNamespace(
        actor=f"actor-{principal_id}",
        environment="vuoro-dev",
        authorities=frozenset({"work:evidence", "work:claim", "work:read"}),
        principal_id=principal_id,
        workspace_id=workspace_id,
        client_id=client_id,
        grant_id=grant_id,
    )
    return SimpleNamespace(
        identity=identity, request_id="request-1", basis_revision=None,
        catalog_revision="catalog-1", idempotency_requirement="not-allowed",
        idempotency_key=None,
    )


A = _context("github:100:0")
B = _context("github:200:0")


def _app(store) -> WorkApplication:
    return WorkApplication.postgres(store)


def _invoke(store, operation: str, arguments: dict, context) -> dict:
    """Invoke and hold the result to the operation's published contract."""
    result = _app(store).invoke(operation, arguments, context)
    jsonschema.validate(result, _CONTRACTS[operation].result_schema)
    return result


def _run(store, context, key: str | None = None) -> str:
    result = _app(store).invoke("work.run.register-v1", {
        "harness_id": "claude-code", "harness_build": "1.0.0", "model_id": "m",
        "recipe_id": "r",
        "observed_profile": {"instruction_digest": "sha256:" + "a" * 64, "skill_digests": []},
        "idempotency_key": key or f"run-{uuid.uuid4().hex}",
    }, context)
    return result["run"]["run_id"]


def _items(store, count: int = 1) -> list[int]:
    sprint_id = pg.create_sprint(store, f"Lease-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")
    track_id = pg.get_or_create_track(store, sprint_id, "lease")
    return [pg.create_work_item(store, sprint_id, track_id, f"item {i}") for i in range(count)]


def _claim(store, context, item_id: int, run_id: str, key: str, **extra) -> dict:
    return _invoke(store, "work.lease.acquire-v1", {
        "item_id": item_id, "run_id": run_id, "idempotency_key": key, **extra,
    }, context)


def _heartbeat(store, context, lease_id: str, run_id: str) -> dict:
    return _invoke(store, "work.lease.heartbeat-v1", {"lease_id": lease_id, "run_id": run_id}, context)


def _complete(store, context, lease_id: str, run_id: str, key: str, *, outcome="succeeded",
              checks=None, payload=None, summary="done") -> dict:
    return _invoke(store, "work.lease.complete-v1", {
        "lease_id": lease_id, "run_id": run_id, "outcome": outcome, "summary": summary,
        "payload": payload if payload is not None else {"result": "ok"},
        "checks": checks if checks is not None else [{"name": "tests", "status": "passed"}],
        "idempotency_key": key,
    }, context)


def _read(store, item_id: int) -> dict:
    return _invoke(store, "work.lease.read-v1", {"item_id": item_id}, A)


def _refused(call) -> ApplicationRejection:
    with pytest.raises(ApplicationRejection) as excinfo:
        call()
    return excinfo.value


def _backdate(store, lease_id: str, seconds: int) -> None:
    """Move a lease's last heartbeat into the past: time passing, no sleep."""
    with store.conn.cursor() as cur:
        cur.execute(
            "UPDATE work_lease SET heartbeat_at = heartbeat_at - %s * interval '1 second' "
            "WHERE repo_id = %s AND lease_id = %s",
            (seconds, store.repo_id, lease_id),
        )
    store.conn.commit()


def _scalar(store, sql: str, params: tuple):
    with store.conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    store.conn.rollback()
    return next(iter(row.values()))


def _lease_rows(store, item_id: int, state: str | None = None) -> int:
    sql = "SELECT count(*) FROM work_lease WHERE repo_id = %s AND work_item_id = %s"
    params: tuple = (store.repo_id, item_id)
    if state:
        sql += " AND state = %s"
        params += (state,)
    return _scalar(store, sql, params)


def _reports(store, item_id: int) -> int:
    return _scalar(
        store, "SELECT count(*) FROM work_outcome_report WHERE repo_id = %s AND work_item_id = %s",
        (store.repo_id, item_id),
    )


def _events(store, item_id: int, event_type: str) -> list[dict]:
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM event WHERE repo_id = %s AND work_item_id = %s "
            "AND event_type = %s ORDER BY id",
            (store.repo_id, item_id, event_type),
        )
        rows = [row["payload"] for row in cur.fetchall()]
    store.conn.rollback()
    return rows


def _status(store, item_id: int) -> str:
    return pg.get_work_item(store, item_id)["status"]


def _sibling(store) -> pg.PgStore:
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    return pg.PgStore(conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid)


def _stampede(store, calls) -> list:
    """Each ``store -> result`` call on its own thread and connection,
    released together by a barrier; returns each result or exception."""
    stores = [_sibling(store) for _ in calls]
    barrier = threading.Barrier(len(calls))
    outcomes: list = [None] * len(calls)

    def run(index, call):
        try:
            barrier.wait(timeout=10)
            outcomes[index] = call(stores[index])
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcomes[index] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        for s in stores:
            s.conn.close()
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


def _split(outcomes):
    ok = [o for o in outcomes if isinstance(o, dict)]
    refused = sorted(o.code for o in outcomes if isinstance(o, ApplicationRejection))
    other = [o for o in outcomes if not isinstance(o, (dict, ApplicationRejection))]
    assert not other, [repr(o) for o in other]
    return ok, refused


class TestAcquire:
    def test_a_claim_leases_the_item_to_the_callers_run_and_activates_it(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        result = _claim(store, A, item, run, "claim-acquire-1")
        lease = result["lease"]
        assert lease["lease_id"].startswith("lease_") and lease["state"] == "active"
        assert (lease["item_id"], lease["run_id"], lease["principal_id"]) == (item, run, "github:100:0")
        assert lease["ttl_seconds"] == 300
        assert result["resumed"] is False and result["took_over"] is None
        assert _status(store, item) == "active"
        (event,) = _events(store, item, "lease.acquired")
        assert event["lease_id"] == lease["lease_id"] and event["previous_status"] == "pending"
        current = _read(store, item)["current_lease"]
        assert current["lease_id"] == lease["lease_id"] and current["stale"] is False

    def test_a_second_holder_is_refused_while_the_lease_is_fresh(self, store):
        (item,) = _items(store)
        _claim(store, A, item, _run(store, A), "claim-held-a")
        refused = _refused(lambda: _claim(store, B, item, _run(store, B), "claim-held-b"))
        assert (refused.code, refused.http_status) == ("lease-held", 409)
        assert _lease_rows(store, item) == 1

    def test_a_refused_claim_commits_nothing_so_its_key_can_be_retried(self, store):
        (item,) = _items(store)
        first = _claim(store, A, item, _run(store, A), "claim-retry-a")["lease"]
        run_b = _run(store, B)
        assert _refused(lambda: _claim(store, B, item, run_b, "claim-retry-b")).code == "lease-held"
        _backdate(store, first["lease_id"], 301)
        taken = _claim(store, B, item, run_b, "claim-retry-b")
        assert taken["took_over"] == first["lease_id"]

    def test_the_same_holder_with_another_key_is_refused_too(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        _claim(store, A, item, run, "claim-own-1")
        assert _refused(lambda: _claim(store, A, item, run, "claim-own-2")).code == "lease-held"

    def test_unknown_settled_and_blocked_work_is_refused(self, store):
        blocker, blocked, done = _items(store, 3)
        pg.add_dep(store, blocker, blocked)
        pg.set_work_item_status(store, done, "active")
        pg.record_decision(store, done, "accept", actor="operator")
        run = _run(store, A)
        assert _refused(lambda: _claim(store, A, 10**9, run, "claim-unknown")).code == "work-not-found"
        assert _refused(lambda: _claim(store, A, done, run, "claim-settled")).code == "work-settled"
        assert _refused(lambda: _claim(store, A, blocked, run, "claim-blocked")).code == "work-blocked"
        assert _status(store, blocked) == "pending"

    def test_an_item_in_blocked_status_is_refused(self, store):
        (item,) = _items(store)
        pg.set_work_item_status(store, item, "active")
        pg.set_work_item_status(store, item, "blocked")
        assert _refused(lambda: _claim(store, A, item, _run(store, A), "claim-status-blocked")).code == "work-blocked"

    def test_a_run_that_is_not_the_callers_is_run_not_found(self, store):
        (item,) = _items(store)
        run_b = _run(store, B)
        assert _refused(lambda: _claim(store, A, item, run_b, "claim-foreign-run")).code == "run-not-found"
        grant_ctx = _context("github:100:0", client_id="c1", grant_id="g1")
        other_grant = _context("github:100:0", client_id="c1", grant_id="g2")
        run_g1 = _run(store, grant_ctx)
        assert _refused(lambda: _claim(store, other_grant, item, run_g1, "claim-other-grant")).code == "run-not-found"
        assert _lease_rows(store, item) == 0

    @pytest.mark.parametrize("ttl", [29, 3601, "60", True, 1.5])
    def test_ttl_outside_bounds_is_invalid(self, store, ttl):
        (item,) = _items(store)
        refused = _refused(lambda: _claim(store, A, item, _run(store, A), "claim-ttl-bad", ttl_seconds=ttl))
        assert (refused.code, refused.http_status) == ("invalid-arguments", 422)

    def test_a_claim_may_name_its_ttl_and_the_default_comes_from_the_environment(self, store, monkeypatch):
        one, two = _items(store, 2)
        assert _claim(store, A, one, _run(store, A), "claim-ttl-own", ttl_seconds=45)["lease"]["ttl_seconds"] == 45
        monkeypatch.setenv("SPRINTCTL_LEASE_TTL_SECONDS", "120")
        assert _claim(store, A, two, _run(store, A), "claim-ttl-env")["lease"]["ttl_seconds"] == 120

    def test_a_malformed_idempotency_key_is_invalid(self, store):
        (item,) = _items(store)
        assert _refused(lambda: _claim(store, A, item, _run(store, A), "short")).code == "invalid-arguments"

    def test_the_same_key_for_another_item_is_an_idempotency_conflict(self, store):
        one, two = _items(store, 2)
        run = _run(store, A)
        _claim(store, A, one, run, "claim-conflict-1")
        assert _refused(lambda: _claim(store, A, two, run, "claim-conflict-1")).code == "idempotency-conflict"
        assert _lease_rows(store, two) == 0


class TestExclusivityRaces:
    def test_eight_racing_holders_leave_exactly_one_lease(self, store):
        (item,) = _items(store)
        contexts = [_context(f"github:race-{i}:0") for i in range(N_RACERS)]
        runs = [_run(store, ctx) for ctx in contexts]
        calls = [
            (lambda s, i=i: _claim(s, contexts[i], item, runs[i], f"race-claim-{i}"))
            for i in range(N_RACERS)
        ]
        ok, refused = _split(_stampede(store, calls))
        assert len(ok) == 1
        assert refused == ["lease-held"] * (N_RACERS - 1)
        assert _lease_rows(store, item) == 1 and _lease_rows(store, item, "active") == 1
        assert len(_events(store, item, "lease.acquired")) == 1

    def test_eight_racing_takeovers_of_a_stale_lease_leave_exactly_one_holder(self, store):
        (item,) = _items(store)
        stale = _claim(store, A, item, _run(store, A), "race-stale-a")["lease"]
        _backdate(store, stale["lease_id"], 400)
        contexts = [_context(f"github:taker-{i}:0") for i in range(N_RACERS)]
        runs = [_run(store, ctx) for ctx in contexts]
        calls = [
            (lambda s, i=i: _claim(s, contexts[i], item, runs[i], f"race-take-{i}"))
            for i in range(N_RACERS)
        ]
        ok, refused = _split(_stampede(store, calls))
        assert len(ok) == 1 and ok[0]["took_over"] == stale["lease_id"]
        assert refused == ["lease-held"] * (N_RACERS - 1)
        assert _lease_rows(store, item, "active") == 1
        assert _lease_rows(store, item, "superseded") == 1

    def test_eight_retries_of_one_claim_resolve_to_one_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        calls = [(lambda s: _claim(s, A, item, run, "race-same-key"))] * N_RACERS
        ok, refused = _split(_stampede(store, calls))
        assert refused == []
        assert len({o["lease"]["lease_id"] for o in ok}) == 1
        assert _lease_rows(store, item) == 1

    def test_eight_racing_resumes_of_one_expired_claim_reclaim_it_once(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "race-resume-key")["lease"]
        _backdate(store, first["lease_id"], 400)
        calls = [(lambda s: _claim(s, A, item, run, "race-resume-key"))] * N_RACERS
        ok, refused = _split(_stampede(store, calls))
        assert refused == []
        assert len({o["lease"]["lease_id"] for o in ok}) == 1
        assert all(o["resumed"] for o in ok)
        assert ok[0]["lease"]["takeover_of"] == first["lease_id"]
        assert _lease_rows(store, item) == 2 and _lease_rows(store, item, "active") == 1

    def test_a_heartbeat_racing_a_takeover_of_a_stale_lease_never_revives_it(self, store):
        (item,) = _items(store)
        run_a = _run(store, A)
        stale = _claim(store, A, item, run_a, "race-hb-a")["lease"]
        _backdate(store, stale["lease_id"], 400)
        run_b = _run(store, B)
        outcomes = _stampede(store, [
            lambda s: _heartbeat(s, A, stale["lease_id"], run_a),
            lambda s: _claim(s, B, item, run_b, "race-hb-b"),
        ])
        assert isinstance(outcomes[0], ApplicationRejection)
        assert outcomes[0].code in ("lease-expired", "lease-superseded")
        assert isinstance(outcomes[1], dict) and outcomes[1]["took_over"] == stale["lease_id"]

    def test_a_fresh_heartbeat_racing_a_claim_keeps_the_lease(self, store):
        (item,) = _items(store)
        run_a = _run(store, A)
        lease = _claim(store, A, item, run_a, "race-fresh-a")["lease"]
        run_b = _run(store, B)
        outcomes = _stampede(store, [
            lambda s: _heartbeat(s, A, lease["lease_id"], run_a),
            lambda s: _claim(s, B, item, run_b, "race-fresh-b"),
        ])
        assert isinstance(outcomes[0], dict) and outcomes[0]["lease"]["state"] == "active"
        assert isinstance(outcomes[1], ApplicationRejection) and outcomes[1].code == "lease-held"


class TestHeartbeat:
    def test_a_heartbeat_moves_the_clock_forward(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "hb-move-1")["lease"]
        _backdate(store, lease["lease_id"], 100)
        before = _read(store, item)["current_lease"]["heartbeat_at"]
        after = _heartbeat(store, A, lease["lease_id"], run)["lease"]
        assert after["heartbeat_at"] > before and after["lease_id"] == lease["lease_id"]

    def test_an_expired_lease_cannot_heartbeat_even_when_nobody_took_it(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "hb-expired-1")["lease"]
        _backdate(store, lease["lease_id"], 301)
        assert _refused(lambda: _heartbeat(store, A, lease["lease_id"], run)).code == "lease-expired"

    def test_someone_elses_or_an_unknown_lease_is_lease_not_found(self, store):
        (item,) = _items(store)
        run_a = _run(store, A)
        lease = _claim(store, A, item, run_a, "hb-foreign-1")["lease"]
        run_b = _run(store, B)
        assert _refused(lambda: _heartbeat(store, B, lease["lease_id"], run_b)).code == "lease-not-found"
        assert _refused(lambda: _heartbeat(store, A, lease["lease_id"], _run(store, A))).code == "lease-not-found"
        unknown = "lease_" + "0" * 26
        refused = _refused(lambda: _heartbeat(store, A, unknown, run_a))
        assert (refused.code, refused.http_status) == ("lease-not-found", 404)


class TestTakeoverAndStaleCompletion:
    """The product proof's differentiated scenario, steps 1-8."""

    def test_the_settlement_scenario(self, store):
        item_x, item_y = _items(store, 2)
        pg.add_dep(store, item_x, item_y)
        sprint_id = pg.get_work_item(store, item_x)["sprint_id"]
        run_a = _run(store, A)
        lease_a = _claim(store, A, item_x, run_a, "scenario-claim-a")["lease"]
        _heartbeat(store, A, lease_a["lease_id"], run_a)
        # A disappears; its heartbeat lapses.
        _backdate(store, lease_a["lease_id"], 301)
        run_b = _run(store, B)
        taken = _claim(store, B, item_x, run_b, "scenario-claim-b")
        lease_b = taken["lease"]
        assert taken["took_over"] == lease_a["lease_id"]
        (takeover,) = _events(store, item_x, "lease.taken-over")
        assert takeover["previous_lease_id"] == lease_a["lease_id"]
        assert takeover["previous_principal_id"] == "github:100:0"
        assert takeover["reason"] == "taken-over"

        # A comes back and reports success: rejected, retained, nothing settled.
        late_payload = {"diff": "a's work", "commit": "a" * 40}
        refused = _refused(lambda: _complete(
            store, A, lease_a["lease_id"], run_a, "scenario-complete-a", payload=late_payload,
        ))
        assert (refused.code, refused.http_status) == ("lease-superseded", 409)
        assert "retained as evidence" in refused.message
        assert _status(store, item_x) == "active"
        state = _read(store, item_x)
        (report_a,) = state["outcome_reports"]
        assert report_a["disposition"] == "rejected"
        assert report_a["reason_code"] == "lease-superseded"
        assert report_a["payload"] == late_payload and report_a["principal_id"] == "github:100:0"
        assert report_a["decision_id"] is None
        assert {l["lease_id"]: l["state"] for l in state["leases"]} == {
            lease_a["lease_id"]: "superseded", lease_b["lease_id"]: "active",
        }
        assert item_y not in {i["id"] for i in pg.get_ready_items(store, sprint_id)}

        # B's result passes the configured profile (checked) and settles X.
        settled = _complete(store, B, lease_b["lease_id"], run_b, "scenario-complete-b")
        assert settled["settled"] is True
        report_b = settled["report"]
        assert report_b["disposition"] == "settled"
        assert report_b["verification_profile"] == "checked"
        (decision,) = pg.list_decisions(store, item_x)
        assert decision["id"] == report_b["decision_id"]
        assert decision["kind"] == "accept" and decision["actor"] == pg.SETTLEMENT_ACTOR
        assert decision["rationale"].startswith("accepted under verification profile checked")
        assert decision["evidence_digests"] == [report_b["payload_digest"]]
        assert _status(store, item_x) == "done"
        assert item_y in {i["id"] for i in pg.get_ready_items(store, sprint_id)}
        final = _read(store, item_x)
        assert final["current_lease"] is None
        assert [r["disposition"] for r in final["outcome_reports"]] == ["rejected", "settled"]

    def test_a_replayed_late_completion_refuses_the_same_way_with_one_report(self, store):
        (item,) = _items(store)
        run_a = _run(store, A)
        lease_a = _claim(store, A, item, run_a, "replay-late-a")["lease"]
        _backdate(store, lease_a["lease_id"], 301)
        _claim(store, B, item, _run(store, B), "replay-late-b")
        for _ in range(2):
            assert _refused(lambda: _complete(store, A, lease_a["lease_id"], run_a, "replay-late-c")).code == "lease-superseded"
        assert _reports(store, item) == 1
        assert _refused(lambda: _heartbeat(store, A, lease_a["lease_id"], run_a)).code == "lease-superseded"

    def test_a_completion_on_an_expired_lease_is_refused_and_retained(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "expired-complete-a")["lease"]
        _backdate(store, lease["lease_id"], 301)
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "expired-complete-c")).code == "lease-expired"
        assert _read(store, item)["outcome_reports"][0]["reason_code"] == "lease-expired"
        assert _status(store, item) == "active"

    def test_a_strangers_completion_is_not_found_and_leaves_no_evidence(self, store):
        (item,) = _items(store)
        lease = _claim(store, A, item, _run(store, A), "stranger-a")["lease"]
        refused = _refused(lambda: _complete(store, B, lease["lease_id"], _run(store, B), "stranger-c"))
        assert (refused.code, refused.http_status) == ("lease-not-found", 404)
        assert _reports(store, item) == 0

    def test_completing_an_already_settled_lease_again_is_lease_ended(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "ended-lease-a")["lease"]
        first = _complete(store, A, lease["lease_id"], run, "ended-c1")
        assert _complete(store, A, lease["lease_id"], run, "ended-c1") == first
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "ended-c2")).code == "lease-ended"
        assert len(pg.list_decisions(store, item)) == 1

    def test_an_item_settled_by_someone_else_ends_the_holders_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "operator-settled-a")["lease"]
        pg.record_decision(store, item, "accept", actor="operator")
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "operator-settled-c")).code == "lease-ended"
        assert _refused(lambda: _heartbeat(store, A, lease["lease_id"], run)).code == "lease-ended"
        assert _reports(store, item) == 1
        assert len(pg.list_decisions(store, item)) == 1

    def test_an_item_moved_off_active_refuses_settlement(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "moved-item-a")["lease"]
        pg.set_work_item_status(store, item, "blocked")
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "moved-item-c")).code == "work-not-active"
        assert _status(store, item) == "blocked"

    def test_the_same_completion_key_with_other_arguments_is_a_conflict(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "complete-conflict-a")["lease"]
        _complete(store, A, lease["lease_id"], run, "complete-conflict-c", outcome="failed")
        refused = _refused(lambda: _complete(store, A, lease["lease_id"], run, "complete-conflict-c", summary="other"))
        assert refused.code == "idempotency-conflict"
        assert _reports(store, item) == 1


class TestResume:
    def test_a_restarted_holder_resumes_its_own_fresh_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A, "resume-run-fresh")
        first = _claim(store, A, item, run, "resume-fresh-1")
        _backdate(store, first["lease"]["lease_id"], 200)
        # The worker restarts: same principal, same run key, same claim key.
        again_run = _run(store, A, "resume-run-fresh")
        assert again_run == run
        resumed = _claim(store, A, item, run, "resume-fresh-1")
        assert resumed["resumed"] is True
        assert resumed["lease"]["lease_id"] == first["lease"]["lease_id"]
        assert resumed["lease"]["heartbeat_at"] > _iso_minus(first["lease"]["heartbeat_at"], 200)
        assert _lease_rows(store, item) == 1
        done = _complete(store, A, resumed["lease"]["lease_id"], run, "resume-fresh-c")
        assert done["settled"] is True and done["report"]["run_id"] == run
        assert len(pg.list_decisions(store, item)) == 1
        assert all(r["disposition"] != "rejected" for r in _read(store, item)["outcome_reports"])

    def test_a_restarted_holder_reclaims_its_own_expired_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "resume-expired-1")["lease"]
        _backdate(store, first["lease_id"], 301)
        resumed = _claim(store, A, item, run, "resume-expired-1")
        assert resumed["resumed"] is True and resumed["took_over"] is None
        second = resumed["lease"]
        assert second["lease_id"] != first["lease_id"] and second["takeover_of"] == first["lease_id"]
        old = next(l for l in _read(store, item)["leases"] if l["lease_id"] == first["lease_id"])
        assert (old["state"], old["end_reason"]) == ("superseded", "reclaimed-by-holder")
        # The re-presented claim keeps resolving to the newest lease.
        assert _claim(store, A, item, run, "resume-expired-1")["lease"]["lease_id"] == second["lease_id"]
        assert _complete(store, A, second["lease_id"], run, "resume-expired-c")["settled"] is True

    def test_a_restarted_holder_whose_lease_was_taken_over_is_told_so(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "resume-lost-1")["lease"]
        _backdate(store, first["lease_id"], 301)
        _claim(store, B, item, _run(store, B), "resume-lost-b")
        assert _refused(lambda: _claim(store, A, item, run, "resume-lost-1")).code == "lease-superseded"

    def test_resuming_after_settlement_returns_the_settled_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "resume-settled-1")["lease"]
        _complete(store, A, lease["lease_id"], run, "resume-settled-c")
        again = _claim(store, A, item, run, "resume-settled-1")
        assert again["resumed"] is True and again["lease"]["state"] == "settled"

    def test_another_principal_reusing_the_key_gets_nothing_of_the_first(self, store):
        (item,) = _items(store)
        _claim(store, A, item, _run(store, A), "resume-shared-key")
        assert _refused(lambda: _claim(store, B, item, _run(store, B), "resume-shared-key")).code == "lease-held"


def _iso_minus(value: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    moved = datetime.fromisoformat(value.replace("Z", "+00:00")) - timedelta(seconds=seconds)
    return moved.isoformat().replace("+00:00", "Z")


class TestVerification:
    def _lease(self, store, key: str):
        (item,) = _items(store)
        run = _run(store, A)
        return item, run, _claim(store, A, item, run, key)["lease"]["lease_id"]

    def test_checked_needs_a_reported_check(self, store):
        item, run, lease = self._lease(store, "verify-none-a")
        refused = _refused(lambda: _complete(store, A, lease, run, "verify-none-c", checks=[]))
        assert (refused.code, refused.http_status) == ("verification-unsatisfied", 422)
        assert _status(store, item) == "active" and _lease_rows(store, item, "active") == 1
        assert _complete(store, A, lease, run, "verify-none-c2")["settled"] is True

    def test_checked_refuses_a_failed_check(self, store):
        item, run, lease = self._lease(store, "verify-failed-a")
        checks = [{"name": "tests", "status": "passed"}, {"name": "lint", "status": "failed"}]
        assert _refused(lambda: _complete(store, A, lease, run, "verify-failed-c", checks=checks)).code == "verification-unsatisfied"
        assert _read(store, item)["outcome_reports"][0]["checks"] == sorted(checks, key=lambda c: c["name"])

    def test_the_release_names_the_required_checks_and_the_decision_binds_it(self, store):
        (item,) = _items(store)
        reservation = pg.reserve(
            store, item, actor="operator", session_id="release-session", role="execution",
            acceptance_contract={"evidence_obligations": ["tests", "review"]},
        )
        pg.release_reservation(store, reservation["id"])
        run = _run(store, A)
        lease = _claim(store, A, item, run, "verify-release-a")["lease"]["lease_id"]
        assert _read(store, item)["verification"] == {
            "profile": "checked", "required_checks": ["review", "tests"],
        }
        refused = _refused(lambda: _complete(store, A, lease, run, "verify-release-c1"))
        assert refused.code == "verification-unsatisfied" and "review" in refused.message
        checks = [{"name": "tests", "status": "passed"}, {"name": "review", "status": "passed", "ref": "pr#1"}]
        settled = _complete(store, A, lease, run, "verify-release-c2", checks=checks)
        (decision,) = pg.list_decisions(store, item)
        assert decision["release_digest"] == reservation["release_digest"]
        assert settled["report"]["decision_id"] == decision["id"]
        assert pg.unmet_obligations(store, reservation["release_digest"]) == []

    def test_a_self_reported_profile_settles_without_checks(self, store):
        (item,) = _items(store)
        reservation = pg.reserve(
            store, item, actor="operator", session_id="self-session", role="execution",
            acceptance_contract={"verification_profile": "self-reported"},
        )
        pg.release_reservation(store, reservation["id"])
        run = _run(store, A)
        lease = _claim(store, A, item, run, "verify-self-a")["lease"]["lease_id"]
        report = _complete(store, A, lease, run, "verify-self-c", checks=[])["report"]
        assert (report["disposition"], report["verification_profile"]) == ("settled", "self-reported")
        assert pg.list_decisions(store, item)[0]["rationale"].startswith(
            "accepted under verification profile self-reported"
        )

    @pytest.mark.parametrize("profile", ["role-separated", "identity-separated", "human-authorized"])
    def test_a_profile_needing_a_verifier_waits_for_one(self, store, profile):
        (item,) = _items(store)
        reservation = pg.reserve(
            store, item, actor="operator", session_id=f"{profile}-session", role="execution",
            acceptance_contract={"verification_profile": profile},
        )
        pg.release_reservation(store, reservation["id"])
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"verify-{profile}-a")["lease"]["lease_id"]
        result = _complete(store, A, lease, run, f"verify-{profile}-c")
        assert result["settled"] is False
        assert result["report"]["disposition"] == "awaiting-verification"
        assert _status(store, item) == "active" and pg.list_decisions(store, item) == []
        assert _lease_rows(store, item, "active") == 1

    def test_a_failed_outcome_is_recorded_and_frees_the_item(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "verify-fail-a")["lease"]["lease_id"]
        result = _complete(store, A, lease, run, "verify-fail-c", outcome="failed")
        assert (result["settled"], result["report"]["disposition"]) == (False, "recorded")
        assert _status(store, item) == "active"
        assert _read(store, item)["current_lease"] is None
        assert _claim(store, B, item, _run(store, B), "verify-fail-b")["took_over"] is None

    @pytest.mark.parametrize("bad", [
        {"outcome": "done"},
        {"checks": [{"name": "t", "status": "ok"}]},
        {"checks": [{"name": "t", "status": "passed"}, {"name": "t", "status": "passed"}]},
        {"checks": [{"name": "t", "status": "passed", "extra": 1}]},
        {"payload": ["not", "an", "object"]},
        {"payload": {"blob": "x" * (64 * 1024)}},
        {"summary": "s" * 4001},
    ])
    def test_malformed_reports_are_invalid_and_leave_nothing(self, store, bad):
        item, run, lease = self._lease(store, f"verify-bad-{uuid.uuid4().hex[:8]}")
        arguments = {
            "lease_id": lease, "run_id": run, "outcome": "succeeded", "summary": "s",
            "payload": {}, "checks": [], "idempotency_key": "verify-bad-c", **bad,
        }
        refused = _refused(lambda: _app(store).invoke("work.lease.complete-v1", arguments, A))
        assert refused.code == "invalid-arguments"
        assert _reports(store, item) == 0


class TestCrashes:
    def test_a_crash_mid_claim_commits_nothing_and_the_retry_succeeds(self, store, monkeypatch):
        (item,) = _items(store)
        run = _run(store, A)
        with monkeypatch.context() as patch:
            def crash(*_a, **_k):
                raise RuntimeError("simulated crash before the ledger result was recorded")

            patch.setattr(pg, "_record_idempotent_result", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                _claim(store, A, item, run, "crash-claim-1")
        assert _lease_rows(store, item) == 0 and _status(store, item) == "pending"
        assert _events(store, item, "lease.acquired") == []
        assert _claim(store, A, item, run, "crash-claim-1")["resumed"] is False

    def test_a_killed_connection_mid_claim_leaves_no_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        victim = _sibling(store)
        killer = _sibling(store)
        try:
            with victim.conn.cursor() as cur:
                pg.acquire_lease(
                    victim, work_item_id=item, run_id=run, principal_id="github:100:0",
                    workspace_id="ws-1", client_id=None, grant_id=None,
                    claim_key="crash-kill-1", ttl_seconds=300, actor="victim", cur=cur,
                )
                cur.execute("SELECT pg_backend_pid() AS pid")
                pid = cur.fetchone()["pid"]
            with killer.conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s) AS ok", (pid,))
                assert cur.fetchone()["ok"] is True
            killer.conn.commit()
        finally:
            killer.conn.close()
            try:
                victim.conn.close()
            except Exception:
                pass
        for _ in range(50):
            if _lease_rows(store, item) == 0:
                break
            time.sleep(0.05)
        assert _lease_rows(store, item) == 0 and _status(store, item) == "pending"
        assert _claim(store, B, item, _run(store, B), "crash-kill-b")["took_over"] is None

    def test_a_crash_mid_completion_settles_nothing_and_the_retry_settles(self, store, monkeypatch):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "crash-complete-a")["lease"]["lease_id"]
        with monkeypatch.context() as patch:
            def crash(*_a, **_k):
                raise RuntimeError("simulated crash before the ledger result was recorded")

            patch.setattr(pg, "_record_idempotent_result", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                _complete(store, A, lease, run, "crash-complete-c")
        assert _reports(store, item) == 0 and pg.list_decisions(store, item) == []
        assert _status(store, item) == "active" and _lease_rows(store, item, "active") == 1
        assert _complete(store, A, lease, run, "crash-complete-c")["settled"] is True


class TestMaintenance:
    @staticmethod
    def _attested(scoped: pg.PgStore) -> tuple:
        lifecycle = PostgresMaintenanceCapabilityStore(scoped)
        prepared = lifecycle.prepare(
            capability_id=f"mcap:{uuid.uuid4()}", request_id=str(uuid.uuid4()),
            envelope=envelope(), actor="operator", at=AT,
        )
        _anchor_capability_window_to_db_clock(scoped.conn, scoped.repo_id, prepared["capability_id"])
        attested = lifecycle.transition(
            capability_id=prepared["capability_id"], request_id=str(uuid.uuid4()),
            action="attest", expected_revision=prepared["revision"], actor="operator", at=AT,
            effect_ref="sha256:" + "0" * 64,
        )
        return lifecycle, prepared["capability_id"], attested["revision"]

    @staticmethod
    def _activate(lifecycle, capability_id: str, revision) -> None:
        lifecycle.transition(
            capability_id=capability_id, request_id=str(uuid.uuid4()),
            action="activate", expected_revision=revision, actor="operator", at=AT,
            step_id="attest-backup", command_id="verify-backup",
            command_ref="sha256:" + "1" * 64, effect_ref="sha256:" + "2" * 64,
        )

    def _scoped(self, store, pg_test_scope) -> pg.PgStore:
        return pg.PgStore(conn=store.conn, repo_id=pg_test_scope("lease-maintenance"))

    def test_claims_are_refused_while_maintenance_is_active(self, store, pg_test_scope):
        scoped = self._scoped(store, pg_test_scope)
        (item,) = _items(scoped)
        run = _run(scoped, A)
        self._activate(*self._attested(scoped))
        refused = _refused(lambda: _claim(scoped, A, item, run, "maint-claim-1"))
        assert refused.code == "maintenance-active"

    def test_a_live_lease_blocks_activation_and_a_stale_one_does_not(self, store, pg_test_scope):
        scoped = self._scoped(store, pg_test_scope)
        (item,) = _items(scoped)
        lease = _claim(scoped, A, item, _run(scoped, A), "maint-live-1")["lease"]
        attested = self._attested(scoped)
        with pytest.raises(MaintenanceCapabilityError, match="live work leases"):
            self._activate(*attested)
        scoped.conn.rollback()
        _backdate(scoped, lease["lease_id"], 301)
        self._activate(*attested)


class TestSchema18Migration:
    def test_a_fresh_ladder_creates_exactly_the_pinned_shape_and_reruns_cleanly(self, store):
        schema = "migration_18_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            applied = pg_migrations.migrate_schema(pg.PgStore(conn, "migration-18"))
            assert applied["applied_versions"][-1] == 18 and applied["to_version"] == 18
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                assert pg._foreign_relations(cur, pg._SCHEMA_18_TABLES, pg._SCHEMA_18_INDEXES) == []
                # Additive and idempotent: re-applying over its own shape is a no-op.
                pg._apply_schema_version_18(cur)
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    @pytest.mark.parametrize("table", sorted(pg._SCHEMA_18_TABLES))
    def test_a_foreign_table_with_matching_columns_is_refused(self, store, table):
        schema = f"migration_18_{table}_" + uuid.uuid4().hex
        columns, _ = pg._SCHEMA_18_TABLES[table]
        ddl = ", ".join(f"{n} {t} {'NOT NULL' if nn else ''}" for n, t, nn in columns)
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
            cur.execute(f"CREATE TABLE {table} ({ddl})")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match=table):
                pg_migrations.migrate_schema(pg.PgStore(conn, f"migration-18-{table}"))
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_version")
                assert cur.fetchone()["version"] == 2
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()

    def test_storage_refuses_a_second_active_lease_for_one_item(self, store):
        """The partial unique index backs the item-row lock."""
        (item,) = _items(store)
        run = _run(store, A)
        _claim(store, A, item, run, "index-backstop-1")
        with pytest.raises(psycopg.errors.UniqueViolation):
            with store.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO work_lease(repo_id, lease_id, work_item_id, run_id, principal_id, "
                    "workspace_id, claim_key, state, ttl_seconds, acquired_at, heartbeat_at, "
                    "verification_profile, required_checks) VALUES (%s, %s, %s, %s, 'p', 'ws-1', "
                    "'index-backstop-2', 'active', 300, now(), now(), 'checked', '[]'::jsonb)",
                    (store.repo_id, "lease_" + "1" * 26, item, run),
                )
        store.conn.rollback()


class TestReviewFindings:
    """Regression tests for the independent review of sprintctl#98."""

    @staticmethod
    def _freeze(store, item, contract, session):
        reservation = pg.reserve(
            store, item, actor="operator", session_id=session, role="execution",
            acceptance_contract=contract,
        )
        pg.release_reservation(store, reservation["id"])
        return reservation

    def test_a_later_default_reservation_cannot_lower_the_bar(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {
            "verification_profile": "human-authorized", "evidence_obligations": ["security-review"],
        }, "strict-session")
        run = _run(store, A)
        lease = _claim(store, A, item, run, "bar-lower-a")["lease"]
        assert lease["verification"] == {"profile": "human-authorized", "required_checks": ["security-review"]}
        # Anyone with work:write freezes a default contract mid-lease.
        self._freeze(store, item, None, "downgrade-session")
        assert _read(store, item)["verification"]["profile"] == "human-authorized"
        result = _complete(store, A, lease["lease_id"], run, "bar-lower-c",
                           checks=[{"name": "security-review", "status": "passed"}])
        assert result["report"]["disposition"] == "awaiting-verification"
        assert _status(store, item) == "active"

    def test_the_bar_pinned_at_claim_holds_even_if_releases_change_later(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {"evidence_obligations": ["tests", "review"]}, "pin-session")
        run = _run(store, A)
        lease = _claim(store, A, item, run, "bar-pinned-a")["lease"]
        # A revise decision leaves the item with no current release, so its
        # present bar is the default; the lease keeps the one it was given.
        pg.record_decision(store, item, "revise", actor="operator")
        assert _read(store, item)["verification"] == {"profile": "checked", "required_checks": []}
        refused = _refused(lambda: _complete(store, A, lease["lease_id"], run, "bar-pinned-c"))
        assert refused.code == "verification-unsatisfied" and "review" in refused.message

    def test_an_unknown_profile_is_refused_when_the_contract_is_written(self, store):
        (item,) = _items(store)
        with pytest.raises(ValueError, match="verification_profile"):
            pg.reserve(store, item, actor="operator", session_id="bad-profile", role="execution",
                       acceptance_contract={"verification_profile": "Checked"})

    def test_a_malformed_stored_profile_fails_closed(self):
        """Rows written before validation existed never raise."""
        config = pg._contract_verification({"verification_profile": ["checked"], "evidence_obligations": "x"})
        assert config == {"profile": "human-authorized", "required_checks": []}
        assert pg._contract_verification('{"verification_profile": "Checked"}')["profile"] == "human-authorized"
        assert pg._contract_verification("not json")["profile"] == "checked"

    def test_a_late_report_is_kept_whatever_the_stored_profile(self, store):
        (item,) = _items(store)
        self._freeze(store, item, None, "malformed-session")
        run = _run(store, A)
        lease = _claim(store, A, item, run, "malformed-a")["lease"]
        with store.conn.cursor() as cur:
            cur.execute("ALTER TABLE work_release DISABLE TRIGGER USER")
            cur.execute(
                "UPDATE work_release SET acceptance_contract = '{\"verification_profile\": [1]}'::jsonb "
                "WHERE repo_id = %s AND work_item_id = %s", (store.repo_id, item),
            )
            cur.execute("ALTER TABLE work_release ENABLE TRIGGER USER")
        store.conn.commit()
        _backdate(store, lease["lease_id"], 301)
        _claim(store, B, item, _run(store, B), "malformed-b")
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "malformed-c")).code == "lease-superseded"
        assert _reports(store, item) == 1

    def test_a_decision_made_elsewhere_ends_the_lease(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "elsewhere-a")["lease"]
        pg.record_decision(store, item, "withdraw", actor="operator")
        state = _read(store, item)
        assert state["current_lease"] is None
        (ended,) = state["leases"]
        assert (ended["state"], ended["end_reason"]) == ("settled", "item-withdrawn")
        assert _refused(lambda: _heartbeat(store, A, lease["lease_id"], run)).code == "lease-ended"

    def test_a_verifier_decision_on_an_awaiting_report_ends_the_lease(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {"verification_profile": "human-authorized"}, "verifier-session")
        run = _run(store, A)
        lease = _claim(store, A, item, run, "verifier-a")["lease"]
        _complete(store, A, lease["lease_id"], run, "verifier-c")
        pg.record_decision(store, item, "accept", actor="human-verifier")
        assert _read(store, item)["current_lease"] is None
        assert _status(store, item) == "done"

    def test_a_holder_cannot_heartbeat_an_item_moved_off_active(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "moved-hb-a")["lease"]
        pg.set_work_item_status(store, item, "blocked")
        assert _refused(lambda: _heartbeat(store, A, lease["lease_id"], run)).code == "work-not-active"
        assert _refused(lambda: _claim(store, A, item, run, "moved-hb-a")).code == "work-not-active"

    def test_a_blocker_added_after_the_claim_stops_settlement_and_keeps_the_report(self, store):
        item, blocker = _items(store, 2)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "late-blocker-a")["lease"]
        pg.add_dep(store, blocker, item)
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "late-blocker-c")).code == "work-blocked"
        assert _read(store, item)["outcome_reports"][0]["reason_code"] == "work-blocked"
        assert _status(store, item) == "active"

    def test_a_holder_retaking_its_own_stale_lease_with_a_new_key_is_not_a_takeover(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "self-retake-1")["lease"]
        _backdate(store, first["lease_id"], 301)
        again = _claim(store, A, item, run, "self-retake-2")
        assert again["took_over"] is None
        old = next(l for l in _read(store, item)["leases"] if l["lease_id"] == first["lease_id"])
        assert old["end_reason"] == "reclaimed-by-holder"

    @pytest.mark.parametrize("payload,ok", [
        ({"text": "literal \\u0000 text"}, True),
        ({"n": float("nan")}, False),
        ({"n": float("inf")}, False),
        ({"s": "\ud800"}, False),
        ({"s": "nul\x00"}, False),
        ({"nul\x00key": 1}, False),
    ])
    def test_payload_edge_cases(self, store, payload, ok):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"payload-edge-{uuid.uuid4().hex[:8]}")["lease"]
        call = lambda: _complete(store, A, lease["lease_id"], run, "payload-edge-c", payload=payload)
        if ok:
            assert call()["report"]["payload"] == payload
        else:
            assert _refused(call).code == "invalid-arguments"
            assert _reports(store, item) == 0

    def test_a_plain_index_under_the_exclusive_index_name_is_foreign(self, store):
        schema = "migration_18_index_" + uuid.uuid4().hex
        with store.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(pg.PG_DDL)
            cur.execute("UPDATE schema_version SET version = 2")
        store.conn.commit()
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        try:
            assert_disposable_connection(conn)
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            pg_migrations.migrate_schema(pg.PgStore(conn, "migration-18-index"))
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                cur.execute("DROP INDEX uq_work_lease_active_item")
                cur.execute("CREATE INDEX uq_work_lease_active_item ON work_lease(repo_id, work_item_id)")
                with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="uq_work_lease_active_item"):
                    pg._apply_schema_version_18(cur)
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()
