"""PostgreSQL integration tests: the exclusive durable work lease
(agentops#2520, E2b: ``work.lease.acquire-v1`` / ``heartbeat-v1`` /
``complete-v1`` / ``read-v1``, schema 18).

Like tests/pg/test_run_evidence.py these go through ``WorkApplication.invoke``
so identity binding, the write-tool ledger and the rejection mapping are
covered, and they race real connections for the exclusivity claims.  Time is
moved by backdating ``heartbeat_at`` in the database, never by sleeping.
"""
from __future__ import annotations

import json
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
    _append_authority_command,
    authority,
    outbox,
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


def _legacy_bar(store, item_id: int, contract: dict, lease_id: str | None = None) -> None:
    """Store an acceptance contract the way a pre-agentops#2539 authority
    could (write-time validation now refuses it), and optionally pin its
    profile on a lease as 0.9.0 would have: the bar a live tenant may hold."""
    reservation = pg.reserve(
        store, item_id, actor="operator", session_id=f"legacy-{uuid.uuid4().hex[:8]}",
        role="execution", acceptance_contract=None,
    )
    pg.release_reservation(store, reservation["id"])
    with store.conn.cursor() as cur:
        cur.execute("ALTER TABLE work_release DISABLE TRIGGER USER")
        cur.execute(
            "UPDATE work_release SET acceptance_contract = %s::jsonb "
            "WHERE repo_id = %s AND work_item_id = %s",
            (json.dumps(contract), store.repo_id, item_id),
        )
        cur.execute("ALTER TABLE work_release ENABLE TRIGGER USER")
        if lease_id is not None:
            cur.execute(
                "UPDATE work_lease SET verification_profile = %s, required_checks = %s::jsonb "
                "WHERE repo_id = %s AND lease_id = %s",
                (
                    contract.get("verification_profile", "checked"),
                    json.dumps(sorted(contract.get("evidence_obligations", []))),
                    store.repo_id, lease_id,
                ),
            )
    store.conn.commit()


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
        assert (lease["ttl_seconds"], lease["heartbeat_interval_seconds"]) == (600, 120)
        assert lease["generation"] == 1
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
        _backdate(store, first["lease_id"], 601)
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

    @pytest.mark.parametrize("ttl", [45, 600, None])
    def test_a_claim_cannot_name_its_ttl(self, store, ttl):
        """agentops#2540: the TTL is the authority's; the v1 input no longer
        has ttl_seconds, and the published schema refuses it too."""
        (item,) = _items(store)
        refused = _refused(lambda: _claim(store, A, item, _run(store, A), f"claim-ttl-own-{ttl}", ttl_seconds=ttl))
        assert (refused.code, refused.http_status) == ("invalid-arguments", 422)
        assert _lease_rows(store, item) == 0
        schema = _CONTRACTS["work.lease.acquire-v1"].input_schema
        assert "ttl_seconds" not in schema["properties"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"item_id": item, "run_id": "r", "idempotency_key": "k" * 8, "ttl_seconds": 45}, schema)

    @pytest.mark.parametrize("raw,ttl,interval", [(None, 600, 120), ("120", 120, 24), ("5", 30, 6), ("x", 600, 120)])
    def test_the_ttl_is_authority_configuration(self, store, monkeypatch, raw, ttl, interval):
        (item,) = _items(store)
        if raw is None:
            monkeypatch.delenv("SPRINTCTL_LEASE_TTL_SECONDS", raising=False)
        else:
            monkeypatch.setenv("SPRINTCTL_LEASE_TTL_SECONDS", raw)
        lease = _claim(store, A, item, _run(store, A), f"claim-ttl-env-{uuid.uuid4().hex[:8]}")["lease"]
        assert (lease["ttl_seconds"], lease["heartbeat_interval_seconds"]) == (ttl, interval)

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
        _backdate(store, stale["lease_id"], 700)
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
        _backdate(store, first["lease_id"], 700)
        calls = [(lambda s: _claim(s, A, item, run, "race-resume-key"))] * N_RACERS
        ok, refused = _split(_stampede(store, calls))
        assert refused == []
        assert len({o["lease"]["lease_id"] for o in ok}) == 1
        assert all(o["resumed"] for o in ok)
        # Case B: the same lease, reactivated in place, same generation.
        assert ok[0]["lease"]["lease_id"] == first["lease_id"]
        assert ok[0]["lease"]["generation"] == 1 and ok[0]["lease"]["takeover_of"] is None
        assert _lease_rows(store, item) == 1 and _lease_rows(store, item, "active") == 1
        assert len(_events(store, item, "lease.reactivated")) == 1

    def test_a_heartbeat_racing_a_takeover_of_a_stale_lease_never_revives_it(self, store):
        (item,) = _items(store)
        run_a = _run(store, A)
        stale = _claim(store, A, item, run_a, "race-hb-a")["lease"]
        _backdate(store, stale["lease_id"], 700)
        run_b = _run(store, B)
        outcomes = _stampede(store, [
            lambda s: _heartbeat(s, A, stale["lease_id"], run_a),
            lambda s: _claim(s, B, item, run_b, "race-hb-b"),
        ])
        assert isinstance(outcomes[0], ApplicationRejection)
        assert outcomes[0].code in ("lease-expired", "claim-superseded")
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
        _backdate(store, lease["lease_id"], 601)
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
        _backdate(store, lease_a["lease_id"], 601)
        run_b = _run(store, B)
        taken = _claim(store, B, item_x, run_b, "scenario-claim-b")
        lease_b = taken["lease"]
        assert taken["took_over"] == lease_a["lease_id"]
        assert (lease_a["generation"], lease_b["generation"]) == (1, 2)
        (takeover,) = _events(store, item_x, "work.claim.taken-over")
        assert takeover == {
            "previous_claim_id": lease_a["lease_id"],
            "previous_principal": "github:100:0",
            "previous_generation": 1,
            "previous_last_heartbeat": takeover["previous_last_heartbeat"],
            "new_claim_id": lease_b["lease_id"],
            "new_principal": "github:200:0",
            "reason": "stale-lease",
        }
        assert takeover["previous_last_heartbeat"] is not None

        # A comes back and reports success: rejected, retained, nothing settled.
        late_payload = {"diff": "a's work", "commit": "a" * 40}
        refused = _refused(lambda: _complete(
            store, A, lease_a["lease_id"], run_a, "scenario-complete-a", payload=late_payload,
        ))
        assert (refused.code, refused.http_status) == ("claim-superseded", 409)
        assert refused.details == {
            "claim_id": lease_a["lease_id"], "current_generation": 2, "reported_generation": 1,
        }
        assert "retained as evidence" in refused.message
        assert _status(store, item_x) == "active"
        state = _read(store, item_x)
        (report_a,) = state["outcome_reports"]
        assert report_a["disposition"] == "rejected"
        assert report_a["reason_code"] == "claim-superseded"
        assert report_a["payload"] == late_payload and report_a["principal_id"] == "github:100:0"
        assert report_a["decision_id"] is None
        assert {l["lease_id"]: l["state"] for l in state["leases"]} == {
            lease_a["lease_id"]: "superseded", lease_b["lease_id"]: "active",
        }
        assert item_y not in {i["id"] for i in pg.get_ready_items(store, sprint_id)}

        # B's result passes the configured profile (checked) and settles X.
        settled = _complete(store, B, lease_b["lease_id"], run_b, "scenario-complete-b")
        assert settled["settled"] is True and settled["settlement_effect"] == "settled"
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
        _backdate(store, lease_a["lease_id"], 601)
        _claim(store, B, item, _run(store, B), "replay-late-b")
        for _ in range(2):
            refused = _refused(lambda: _complete(store, A, lease_a["lease_id"], run_a, "replay-late-c"))
            assert refused.code == "claim-superseded"
            assert refused.details["reported_generation"] == 1
        assert _reports(store, item) == 1
        refused = _refused(lambda: _heartbeat(store, A, lease_a["lease_id"], run_a))
        assert (refused.code, refused.details["current_generation"]) == ("claim-superseded", 2)

    def test_a_report_on_a_stale_lease_is_retained_and_reactivation_restores_authority(self, store):
        """INV-L1: a result submitted under a stale lease is kept as evidence
        and cannot settle.  Expiry alone is not fatal to the claim
        (agentops#253 decision 3): nobody took it over, so the holder
        re-presents its claim, the same claim is reactivated (decision 5,
        Case B), and its next report settles."""
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "expired-complete-a")["lease"]
        _backdate(store, lease["lease_id"], 601)
        refused = _refused(lambda: _complete(store, A, lease["lease_id"], run, "expired-complete-c"))
        assert (refused.code, refused.http_status) == ("lease-expired", 409)
        (kept,) = _read(store, item)["outcome_reports"]
        assert (kept["disposition"], kept["reason_code"], kept["decision_id"]) == ("rejected", "lease-expired", None)
        assert _status(store, item) == "active" and pg.list_decisions(store, item) == []
        resumed = _claim(store, A, item, run, "expired-complete-a")["lease"]
        assert (resumed["lease_id"], resumed["generation"]) == (lease["lease_id"], 1)
        result = _complete(store, A, lease["lease_id"], run, "expired-complete-c2")
        assert (result["settled"], result["settlement_effect"]) == (True, "settled")
        assert _status(store, item) == "done" and _reports(store, item) == 2

    def test_after_reactivation_the_refused_reports_key_still_replays_its_refusal(self, store):
        """The refused report kept its key: retrying it replays
        lease-expired, even on the reactivated lease, and the message says
        to report under a new key."""
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "expired-replay-a")["lease"]
        _backdate(store, lease["lease_id"], 601)
        first = _refused(lambda: _complete(store, A, lease["lease_id"], run, "expired-replay-c"))
        assert first.code == "lease-expired" and "new idempotency key" in first.message
        _claim(store, A, item, run, "expired-replay-a")
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "expired-replay-c")).code == (
            "lease-expired"
        )
        assert _reports(store, item) == 1 and _status(store, item) == "active"
        assert _complete(store, A, lease["lease_id"], run, "expired-replay-c2")["settled"] is True

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

    def test_a_restarted_holder_reactivates_its_own_stale_lease_in_place(self, store):
        """Case B (agentops#253 decision 5): nothing contested ownership, so
        the same claim is reactivated -- same id, same generation -- and
        nothing is superseded or taken over."""
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "resume-expired-1")["lease"]
        _backdate(store, first["lease_id"], 601)
        resumed = _claim(store, A, item, run, "resume-expired-1")
        assert resumed["resumed"] is True and resumed["took_over"] is None
        again = resumed["lease"]
        assert (again["lease_id"], again["generation"], again["state"]) == (first["lease_id"], 1, "active")
        assert again["takeover_of"] is None and again["acquired_at"] == first["acquired_at"]
        assert again["heartbeat_at"] > first["heartbeat_at"]
        state = _read(store, item)
        assert len(state["leases"]) == 1 and state["current_lease"]["stale"] is False
        assert _events(store, item, "work.claim.taken-over") == []
        (event,) = _events(store, item, "lease.reactivated")
        assert (event["lease_id"], event["generation"]) == (first["lease_id"], 1)
        # A heartbeat on the reactivated lease works again.
        assert _heartbeat(store, A, first["lease_id"], run)["lease"]["generation"] == 1
        assert _complete(store, A, first["lease_id"], run, "resume-expired-c")["settled"] is True

    def test_reactivation_is_refused_as_a_fresh_claim_would_be(self, store):
        item, blocker = _items(store, 2)
        run = _run(store, A)
        first = _claim(store, A, item, run, "resume-blocked-1")["lease"]
        _backdate(store, first["lease_id"], 601)
        pg.add_dep(store, blocker, item)
        assert _refused(lambda: _claim(store, A, item, run, "resume-blocked-1")).code == "work-blocked"
        assert _read(store, item)["current_lease"]["stale"] is True

    def test_a_restarted_holder_whose_lease_was_taken_over_is_told_so(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "resume-lost-1")["lease"]
        _backdate(store, first["lease_id"], 601)
        _claim(store, B, item, _run(store, B), "resume-lost-b")
        refused = _refused(lambda: _claim(store, A, item, run, "resume-lost-1"))
        assert (refused.code, refused.http_status) == ("claim-superseded", 409)
        assert refused.details == {
            "claim_id": first["lease_id"], "current_generation": 2, "reported_generation": 1,
        }

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
            "profile": "checked", "requirements": ["checks"], "required_checks": ["review", "tests"],
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
        """Only a lease pinned before agentops#2539 (or a stored contract)
        can carry such a profile now; its report waits and never settles."""
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"verify-{profile}-a")["lease"]["lease_id"]
        _legacy_bar(store, item, {"verification_profile": profile}, lease)
        result = _complete(store, A, lease, run, f"verify-{profile}-c")
        assert result["settled"] is False
        assert result["report"]["disposition"] == "awaiting-verification"
        assert result["report"]["verification_profile"] == profile
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
        _backdate(scoped, lease["lease_id"], 601)
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

    def test_a_later_weaker_reservation_cannot_lower_the_bar(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {"evidence_obligations": ["security-review"]}, "strict-session")
        run = _run(store, A)
        lease = _claim(store, A, item, run, "bar-lower-a")["lease"]
        assert lease["verification"] == {
            "profile": "checked", "requirements": ["checks"], "required_checks": ["security-review"],
        }
        # Anyone with work:write freezes a weaker contract mid-lease; the
        # bars combine by union, so it adds nothing and removes nothing.
        self._freeze(store, item, {"verification_profile": "self-reported"}, "downgrade-session")
        assert _read(store, item)["verification"] == lease["verification"]
        refused = _refused(lambda: _complete(store, A, lease["lease_id"], run, "bar-lower-c", checks=[]))
        assert refused.code == "verification-unsatisfied" and "security-review" in refused.message
        assert _status(store, item) == "active"

    def test_the_bar_pinned_at_claim_holds_even_if_releases_change_later(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {"evidence_obligations": ["tests", "review"]}, "pin-session")
        run = _run(store, A)
        lease = _claim(store, A, item, run, "bar-pinned-a")["lease"]
        # A revise decision leaves the item with no current release, so its
        # present bar is the default; the lease keeps the one it was given.
        pg.record_decision(store, item, "revise", actor="operator")
        assert _read(store, item)["verification"] == {
            "profile": "checked", "requirements": ["checks"], "required_checks": [],
        }
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
        assert config == {
            "profile": "human-authorized", "requirements": ["human-authorization"], "required_checks": [],
        }
        assert pg._contract_verification('{"verification_profile": "Checked"}')["profile"] == "human-authorized"
        assert pg._contract_verification("not json")["profile"] == "checked"

    def test_a_late_report_is_kept_whatever_the_stored_profile(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "malformed-a")["lease"]
        _backdate(store, lease["lease_id"], 601)
        _claim(store, B, item, _run(store, B), "malformed-b")
        # The item's stored contract goes bad after the takeover.
        _legacy_bar(store, item, {"verification_profile": [1]})
        assert _refused(lambda: _complete(store, A, lease["lease_id"], run, "malformed-c")).code == "claim-superseded"
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
        run = _run(store, A)
        lease = _claim(store, A, item, run, "verifier-a")["lease"]
        _legacy_bar(store, item, {"verification_profile": "human-authorized"}, lease["lease_id"])
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
        """A new claim key is a new claim: the old one is released, not
        superseded (nobody displaced it), and no takeover is recorded."""
        (item,) = _items(store)
        run = _run(store, A)
        first = _claim(store, A, item, run, "self-retake-1")["lease"]
        _backdate(store, first["lease_id"], 601)
        again = _claim(store, A, item, run, "self-retake-2")
        assert again["took_over"] is None and again["lease"]["takeover_of"] is None
        assert again["lease"]["generation"] == 2
        old = next(l for l in _read(store, item)["leases"] if l["lease_id"] == first["lease_id"])
        assert (old["state"], old["end_reason"], old["superseded_by"]) == ("released", "replaced-by-holder", None)
        assert _events(store, item, "work.claim.taken-over") == []
        (acquired,) = [e for e in _events(store, item, "lease.acquired") if e["lease_id"] == again["lease"]["lease_id"]]
        assert acquired["replaces"] == first["lease_id"] and acquired["takeover_of"] is None
        # The released claim's late report is kept and cannot settle.
        refused = _refused(lambda: _complete(store, A, first["lease_id"], run, "self-retake-c"))
        assert refused.code == "lease-ended" and _reports(store, item) == 1

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


class TestVerificationProfilesAsRequirementSets:
    """agentops#2539 (with #2528 and #2529 n2): profiles are sets of
    requirements combined by union; the ones nothing enforces yet are
    refused when a contract is written and fail closed when stored."""

    @staticmethod
    def _freeze(store, item, contract):
        reservation = pg.reserve(
            store, item, actor="operator", session_id=f"s-{uuid.uuid4().hex[:8]}",
            role="execution", acceptance_contract=contract,
        )
        pg.release_reservation(store, reservation["id"])
        return reservation

    @pytest.mark.parametrize("profile", ["role-separated", "identity-separated", "human-authorized"])
    def test_an_unenforced_profile_is_refused_when_the_contract_is_written(self, store, profile):
        (item,) = _items(store)
        with pytest.raises(ValueError, match="not enforced yet"):
            self._freeze(store, item, {"verification_profile": profile})
        assert pg.current_release(store, item) is None

    def test_self_reported_cannot_carry_evidence_obligations(self, store):
        (item,) = _items(store)
        with pytest.raises(ValueError, match="self-reported"):
            self._freeze(store, item, {"verification_profile": "self-reported", "evidence_obligations": ["tests"]})

    def test_self_reported_does_not_settle_with_a_failed_check(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {"verification_profile": "self-reported"})
        run = _run(store, A)
        lease = _claim(store, A, item, run, "self-failed-a")["lease"]["lease_id"]
        checks = [{"name": "tests", "status": "failed"}]
        refused = _refused(lambda: _complete(store, A, lease, run, "self-failed-c", checks=checks))
        assert (refused.code, refused.http_status) == ("verification-unsatisfied", 422)
        assert _status(store, item) == "active" and pg.list_decisions(store, item) == []
        assert _complete(store, A, lease, run, "self-failed-c2", checks=[])["settled"] is True

    def test_a_stored_self_reported_contract_with_obligations_still_needs_them(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "self-oblig-a")["lease"]["lease_id"]
        _legacy_bar(store, item, {"verification_profile": "self-reported", "evidence_obligations": ["tests"]}, lease)
        refused = _refused(lambda: _complete(store, A, lease, run, "self-oblig-c", checks=[]))
        assert refused.code == "verification-unsatisfied" and "tests" in refused.message

    def test_profiles_combine_by_union_not_by_rank(self):
        def bar(*profiles):
            return pg._combined(*(pg._verification(p, []) for p in profiles))

        assert bar("self-reported", "checked") == {
            "profile": "checked", "requirements": ["checks"], "required_checks": [],
        }
        # No ladder: a human's authorization does not imply checks, nor a
        # verifier role a separate identity.
        assert bar("checked", "human-authorized") == {
            "profile": "checked+human-authorized",
            "requirements": ["checks", "human-authorization"], "required_checks": [],
        }
        assert bar("role-separated", "identity-separated")["requirements"] == [
            "checks", "verifier-identity", "verifier-role",
        ]
        assert bar("role-separated", "checked")["profile"] == "role-separated"
        combined = bar("role-separated", "human-authorized")
        assert combined["profile"] == "role-separated+human-authorized"
        # A combined name read back from a lease row means the same bar.
        assert pg._verification(combined["profile"], [])["requirements"] == combined["requirements"]
        assert pg._verification("checked+bogus", [])["profile"] == "human-authorized"

    def test_union_over_releases_keeps_every_required_check(self, store):
        (item,) = _items(store)
        self._freeze(store, item, {"verification_profile": "self-reported"})
        self._freeze(store, item, {"evidence_obligations": ["lint"]})
        self._freeze(store, item, {"evidence_obligations": ["tests"]})
        assert _read(store, item)["verification"] == {
            "profile": "checked", "requirements": ["checks"], "required_checks": ["lint", "tests"],
        }

    @pytest.mark.parametrize("profile", ["role-separated", "identity-separated", "human-authorized", "Checked", 7])
    def test_a_stored_unenforced_or_malformed_profile_refuses_claims(self, store, profile):
        """Fail closed: a contract stored before the refusal existed cannot
        be leased, rather than being leased under a bar nothing checks."""
        (item,) = _items(store)
        _legacy_bar(store, item, {"verification_profile": profile})
        refused = _refused(lambda: _claim(store, A, item, _run(store, A), "stored-unenforced-a"))
        assert (refused.code, refused.http_status) == ("verification-unsupported", 409)
        assert _lease_rows(store, item) == 0 and _status(store, item) == "pending"

    def _awaiting(self, store, key: str):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"{key}-a")["lease"]
        _legacy_bar(store, item, {"verification_profile": "human-authorized"}, lease["lease_id"])
        report = _complete(store, A, lease["lease_id"], run, f"{key}-c")["report"]
        assert report["disposition"] == "awaiting-verification"
        return item, run, lease, report

    def test_an_awaiting_report_protects_the_item_from_takeover(self, store):
        item, _run_id, lease, report = self._awaiting(store, "await-protect")
        _backdate(store, lease["lease_id"], 3601)
        refused = _refused(lambda: _claim(store, B, item, _run(store, B), "await-protect-b"))
        assert refused.code == "work-awaiting-verification" and report["report_id"] in refused.message
        state = _read(store, item)
        assert state["current_lease"]["lease_id"] == lease["lease_id"]
        assert [r["disposition"] for r in state["outcome_reports"]] == ["awaiting-verification"]

    def test_an_accept_never_attests_a_verifier_nothing_checked(self, store):
        """No decision path checks a verifier role, identity or human yet,
        so an ``accept`` ends the wait without the report claiming it was
        verified (review finding 2 on sprintctl#99)."""
        item, _run_id, _lease, report = self._awaiting(store, "await-accept")
        pg.record_decision(store, item, "accept", actor="human-verifier")
        (stamped,) = _read(store, item)["outcome_reports"]
        assert (stamped["report_id"], stamped["disposition"], stamped["reason_code"]) == (
            report["report_id"], "rejected", "decided-accept-unverified",
        )
        assert stamped["decision_id"] is None and stamped["payload"] == {"result": "ok"}
        assert _status(store, item) == "done"

    @pytest.mark.parametrize("kind", ["reject", "withdraw", "revise", "supersede"])
    def test_any_other_decision_ends_the_wait_and_keeps_the_report(self, store, kind):
        item, _run_id, lease, report = self._awaiting(store, f"await-{kind}")
        extra = {}
        if kind == "supersede":
            (replacement,) = _items(store)
            extra = {"superseded_by_item_id": replacement}
        pg.record_decision(store, item, kind, actor="human-verifier", **extra)
        (stamped,) = _read(store, item)["outcome_reports"]
        assert (stamped["report_id"], stamped["disposition"], stamped["reason_code"]) == (
            report["report_id"], "rejected", f"decided-{kind}",
        )
        assert stamped["decision_id"] is None and stamped["payload"] == {"result": "ok"}
        if kind == "revise":
            # Sent back, not closed: the wait is over, so the item can be
            # claimed again once the holder's lease goes stale.
            _backdate(store, lease["lease_id"], 3601)
            assert _claim(store, B, item, _run(store, B), "await-revise-b")["took_over"] == lease["lease_id"]


def _served_context(actor: str, key: str):
    identity = SimpleNamespace(
        actor=actor, environment="vuoro-dev", authorities=frozenset(),
        authorizes_repo=lambda repo_id: True,
    )
    return SimpleNamespace(
        identity=identity, request_id="request-1", basis_revision=None,
        catalog_revision="catalog-1", idempotency_requirement="required",
        idempotency_key=key,
    )


def _outbox_decision(store, tmp_path, item_id: int, route: str, actor: str) -> None:
    """Accept through an authority outbox command: ``item.done`` (the old
    client's alias) or ``decision.record`` (authority.py's two paths)."""
    from sprintctl import authority, outbox
    from tests.pg._shared import _append_authority_command

    item = pg.get_work_item(store, item_id)
    producer = outbox.open_outbox(tmp_path / f"{route}-{uuid.uuid4().hex[:8]}.db")
    try:
        if route == "outbox-item-done":
            command = _append_authority_command(
                producer, store, record_type="item.done", aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "done"}, actor=actor,
            )
        else:
            command = _append_authority_command(
                producer, store, record_type="decision.record", aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"kind": "accept", "rationale": "verified", "evidence_digests": ["a" * 64]},
                actor=actor,
            )
        assert authority.arbitrate_command(store, command).accepted is True
    finally:
        producer.close()


def _accept(store, tmp_path, item_id: int, route: str, actor: str) -> None:
    if route == "record-decision":
        pg.record_decision(store, item_id, "accept", actor=actor)
    elif route == "set-status-done":
        pg.set_work_item_status(store, item_id, "done", actor=actor)
    elif route == "served-accept":
        _app(store).invoke("work.decision.record", {
            "item_id": item_id, "kind": "accept", "rationale": "verified",
            "evidence_digests": ["ab" * 32],
        }, _served_context(actor, uuid.uuid4().hex))
    else:
        _outbox_decision(store, tmp_path, item_id, route, actor)


_ACCEPT_ROUTES = ["record-decision", "set-status-done", "served-accept", "outbox-item-done", "outbox-decision-record"]


class TestAwaitingReportStamping:
    """Review findings 1-4 on sprintctl#99: every decision path stamps an
    awaiting report, and none of them lets a report settle work it had no
    authority over (INV-L1) or attest a verification nobody performed."""

    def _awaiting(self, store, key: str, profile: str = "human-authorized"):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"{key}-a")["lease"]
        _legacy_bar(store, item, {"verification_profile": profile}, lease["lease_id"])
        report = _complete(store, A, lease["lease_id"], run, f"{key}-c")["report"]
        assert report["disposition"] == "awaiting-verification"
        return item, run, lease, report

    def _taken_over_under_090(self, store, monkeypatch, key: str):
        """The legacy state 0.9.0 could leave: A's report awaits a verifier,
        A's lease went stale and B took the item over anyway (0.9.0 had no
        awaiting protection), with the item's contract back at ``checked``."""
        item, _run_id, lease_a, report_a = self._awaiting(store, key)
        _legacy_bar(store, item, {"verification_profile": "checked"})
        _backdate(store, lease_a["lease_id"], 3601)
        with monkeypatch.context() as patch:
            patch.setattr(pg, "_refuse_awaiting_verification", lambda *args, **kwargs: None)
            run_b = _run(store, B)
            claimed = _claim(store, B, item, run_b, f"{key}-b")
        assert claimed["took_over"] == lease_a["lease_id"]
        return item, report_a, run_b, claimed["lease"]

    def _report(self, store, item, report_id):
        return next(r for r in _read(store, item)["outcome_reports"] if r["report_id"] == report_id)

    def test_a_lease_settlement_never_settles_a_superseded_awaiting_report(self, store, monkeypatch):
        item, report_a, run_b, lease_b = self._taken_over_under_090(store, monkeypatch, "sup-settle")
        settled = _complete(store, B, lease_b["lease_id"], run_b, "sup-settle-bc")
        assert settled["settled"] is True and settled["report"]["disposition"] == "settled"
        stale = self._report(store, item, report_a["report_id"])
        assert (stale["disposition"], stale["reason_code"], stale["decision_id"]) == (
            "rejected", "claim-superseded", None,
        )
        assert stale["payload"] == {"result": "ok"}
        (decision,) = pg.list_decisions(store, item)
        assert settled["report"]["decision_id"] == decision["id"]

    @pytest.mark.parametrize("route", _ACCEPT_ROUTES)
    def test_an_accept_never_settles_a_superseded_awaiting_report(self, store, monkeypatch, tmp_path, route):
        item, report_a, _run_b, _lease_b = self._taken_over_under_090(store, monkeypatch, f"sup-{route}")
        _accept(store, tmp_path, item, route, "human-verifier")
        assert _status(store, item) == "done"
        stale = self._report(store, item, report_a["report_id"])
        assert (stale["disposition"], stale["reason_code"], stale["decision_id"]) == (
            "rejected", "claim-superseded", None,
        )

    @pytest.mark.parametrize("route", _ACCEPT_ROUTES)
    @pytest.mark.parametrize("profile", ["role-separated", "identity-separated", "human-authorized"])
    def test_every_accept_path_stamps_an_unverified_pin_without_attesting_it(
        self, store, tmp_path, route, profile,
    ):
        """Including the worker's own ``done``: the report is kept as
        evidence but never reads as a verification that passed."""
        item, _run_id, _lease, report = self._awaiting(store, f"pin-{route}-{profile}", profile)
        _accept(store, tmp_path, item, route, A.identity.actor)
        assert _status(store, item) == "done"
        stamped = self._report(store, item, report["report_id"])
        assert (stamped["disposition"], stamped["reason_code"], stamped["decision_id"]) == (
            "rejected", "decided-accept-unverified", None,
        )
        assert stamped["verification_profile"] == profile
        assert _read(store, item)["current_lease"] is None

    def test_the_holders_own_stale_resume_is_refused_while_its_report_waits(self, store):
        item, run, lease, report = self._awaiting(store, "own-resume")
        _backdate(store, lease["lease_id"], 3601)
        refused = _refused(lambda: _claim(store, A, item, run, "own-resume-a"))
        assert (refused.code, refused.http_status) == ("work-awaiting-verification", 409)
        assert report["report_id"] in refused.message
        refused_new_key = _refused(lambda: _claim(store, A, item, run, "own-resume-a2"))
        assert refused_new_key.code == "work-awaiting-verification"
        assert _lease_rows(store, item) == 1 and _lease_rows(store, item, "active") == 1

    def test_a_fresh_lease_with_a_waiting_report_answers_awaiting_before_held(self, store):
        item, _run_id, _lease, _report = self._awaiting(store, "check-order")
        assert _refused(lambda: _claim(store, B, item, _run(store, B), "check-order-b")).code == (
            "work-awaiting-verification"
        )

    @pytest.mark.parametrize("profile", ["role-separated", "identity-separated"])
    def test_zero_checks_under_a_checks_bearing_pin_is_rejected_not_deferred(self, store, profile):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"zero-{profile}-a")["lease"]["lease_id"]
        _legacy_bar(store, item, {"verification_profile": profile}, lease)
        refused = _refused(lambda: _complete(store, A, lease, run, f"zero-{profile}-c", checks=[]))
        assert (refused.code, refused.http_status) == ("verification-unsatisfied", 422)
        (report,) = _read(store, item)["outcome_reports"]
        assert (report["disposition"], report["reason_code"]) == ("rejected", "verification-unsatisfied")
        # Nothing waits: a later claim meets the stored bar's own refusal,
        # not work-awaiting-verification.
        _backdate(store, lease, 3601)
        assert _refused(lambda: _claim(store, B, item, _run(store, B), f"zero-{profile}-b")).code == (
            "verification-unsupported"
        )


def _report(store, context, lease_id: str, run_id: str, key: str, *, outcome="succeeded",
            checks=None, payload=None, operation="work.lease.report-outcome-v1") -> dict:
    return _invoke(store, operation, {
        "lease_id": lease_id, "run_id": run_id, "outcome": outcome, "summary": "done",
        "payload": payload if payload is not None else {"result": "ok"},
        "checks": checks if checks is not None else [{"name": "tests", "status": "passed"}],
        "idempotency_key": key,
    }, context)


class TestOperatorLeaseContract:
    """agentops#2540: the shipped lease aligned with the operator's
    normative contract on agentops#253, and its two invariants."""

    def _taken_over(self, store, key: str):
        (item,) = _items(store)
        run_a = _run(store, A)
        lease_a = _claim(store, A, item, run_a, f"{key}-a")["lease"]
        _backdate(store, lease_a["lease_id"], 601)
        run_b = _run(store, B)
        lease_b = _claim(store, B, item, run_b, f"{key}-b")["lease"]
        return item, run_a, lease_a, run_b, lease_b

    @pytest.mark.parametrize("outcome", ["succeeded", "failed"])
    def test_inv_l1_a_superseded_lease_keeps_its_result_but_not_its_authority(self, store, outcome):
        """INV-L1: a lease grants authority, not ownership of the result."""
        item, run_a, lease_a, _run_b, lease_b = self._taken_over(store, f"inv-l1-{outcome}")
        late = {"diff": "a's work"}
        refused = _refused(lambda: _report(store, A, lease_a["lease_id"], run_a, f"inv-l1-{outcome}-c",
                                           outcome=outcome, payload=late))
        assert refused.code == "claim-superseded"
        (kept,) = _read(store, item)["outcome_reports"]
        assert (kept["disposition"], kept["payload"], kept["decision_id"]) == ("rejected", late, None)
        # Nothing settled or released: the item, its decisions and B's lease
        # are exactly as they were.
        assert _status(store, item) == "active" and pg.list_decisions(store, item) == []
        current = _read(store, item)["current_lease"]
        assert (current["lease_id"], current["state"]) == (lease_b["lease_id"], "active")

    @pytest.mark.parametrize("outcome", ["succeeded", "failed"])
    def test_inv_l1_a_stale_lease_nobody_took_keeps_its_result_but_cannot_settle(self, store, outcome):
        """INV-L1 names stale leases too: a report under one is retained and
        changes nothing, even though nobody took the claim over."""
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"inv-l1-stale-{outcome}-a")["lease"]
        _backdate(store, lease["lease_id"], 3600)
        refused = _refused(lambda: _report(store, A, lease["lease_id"], run, f"inv-l1-stale-{outcome}-c",
                                           outcome=outcome))
        assert refused.code == "lease-expired" and refused.details is None
        (kept,) = _read(store, item)["outcome_reports"]
        assert (kept["disposition"], kept["reason_code"]) == ("rejected", "lease-expired")
        assert _status(store, item) == "active" and pg.list_decisions(store, item) == []
        current = _read(store, item)["current_lease"]
        assert (current["lease_id"], current["state"], current["stale"]) == (lease["lease_id"], "active", True)

    def test_inv_l2_the_same_key_restores_identity_never_superseded_authority(self, store):
        """INV-L2: after a takeover, A's re-presented claim -- same key, same
        arguments -- is refused, and stays refused even once B's lease is
        stale in turn; it never revives A's lease."""
        item, run_a, lease_a, _run_b, lease_b = self._taken_over(store, "inv-l2")
        for _ in range(2):
            refused = _refused(lambda: _claim(store, A, item, run_a, "inv-l2-a"))
            assert refused.code == "claim-superseded" and refused.details["claim_id"] == lease_a["lease_id"]
        _backdate(store, lease_b["lease_id"], 601)
        assert _refused(lambda: _claim(store, A, item, run_a, "inv-l2-a")).code == "claim-superseded"
        assert _refused(lambda: _heartbeat(store, A, lease_a["lease_id"], run_a)).code == "claim-superseded"
        state = _read(store, item)
        assert {l["lease_id"]: l["state"] for l in state["leases"]} == {
            lease_a["lease_id"]: "superseded", lease_b["lease_id"]: "active",
        }
        # New authority comes only from asking again: a new claim, which is
        # a takeover of B's stale lease in its own right (generation 3).
        again = _claim(store, A, item, run_a, "inv-l2-a-new")
        assert again["took_over"] == lease_b["lease_id"] and again["lease"]["generation"] == 3
        assert len(_events(store, item, "work.claim.taken-over")) == 2

    def test_report_outcome_and_its_deprecated_alias_are_one_operation(self, store):
        """agentops#253 decision 4: the operation is report_outcome; the
        shipped complete-v1 is a deprecated alias with the same input,
        result and ledger, so one key is one report under either name."""
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "alias-claim-a")["lease"]["lease_id"]
        first = _report(store, A, lease, run, "alias-report-c", outcome="failed")
        assert (first["settled"], first["settlement_effect"]) == (False, "lease-released")
        replayed = _report(store, A, lease, run, "alias-report-c", outcome="failed",
                           operation="work.lease.complete-v1")
        assert replayed["report"]["report_id"] == first["report"]["report_id"]
        conflict = _refused(lambda: _report(store, A, lease, run, "alias-report-c", outcome="succeeded",
                                            operation="work.lease.complete-v1"))
        assert conflict.code == "idempotency-conflict"
        assert _reports(store, item) == 1
        new = _CONTRACTS["work.lease.report-outcome-v1"]
        old = _CONTRACTS["work.lease.complete-v1"]
        assert (old.input_schema, old.result_schema) == (new.input_schema, new.result_schema)
        assert new.deprecation is None
        assert old.deprecation == {
            "deprecated": True, "replacement": "work.lease.report-outcome-v1", "sunset_at": None,
        }

    def test_the_catalog_publishes_the_alias_as_deprecated(self):
        from sprintctl.vuoro_adapter import catalog_operation_specs

        specs = {spec["name"]: spec for spec in catalog_operation_specs(resource_schema_available=True)}
        assert specs["work.lease.complete-v1"]["deprecation"]["replacement"] == "work.lease.report-outcome-v1"
        assert specs["work.lease.report-outcome-v1"]["deprecation"]["deprecated"] is False

    @pytest.mark.parametrize("disposition,effect", [
        ("settled", "settled"), ("recorded", "lease-released"), ("rejected", "none"),
    ])
    def test_every_report_says_what_it_did_to_the_work(self, store, disposition, effect):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, f"effect-{disposition}-a")["lease"]["lease_id"]
        key = f"effect-{disposition}-c"
        if disposition == "rejected":
            refused = _refused(lambda: _report(store, A, lease, run, key, checks=[]))
            assert refused.code == "verification-unsatisfied"
            return
        result = _report(store, A, lease, run, key, outcome="succeeded" if disposition == "settled" else "failed")
        assert (result["report"]["disposition"], result["settlement_effect"]) == (disposition, effect)

    def test_a_reactivation_is_refused_while_the_holders_report_waits(self, store):
        """Case B does not bypass the awaiting protection (agentops#2528)."""
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "react-await-a")["lease"]
        _legacy_bar(store, item, {"verification_profile": "human-authorized"}, lease["lease_id"])
        assert _report(store, A, lease["lease_id"], run, "react-await-c")["report"]["disposition"] == (
            "awaiting-verification"
        )
        # Case A (fresh) still just refreshes the holder's own heartbeat.
        assert _claim(store, A, item, run, "react-await-a")["lease"]["lease_id"] == lease["lease_id"]
        _backdate(store, lease["lease_id"], 601)
        assert _refused(lambda: _claim(store, A, item, run, "react-await-a")).code == "work-awaiting-verification"
        assert _events(store, item, "lease.reactivated") == []

    def test_generations_count_claims_on_the_item(self, store):
        """Generation 1, a takeover makes 2, the holder's own reactivation
        keeps it, and ``work.lease.read-v1`` reports the same numbers."""
        item, run_a, lease_a, run_b, lease_b = self._taken_over(store, "gen-count")
        assert (lease_a["generation"], lease_b["generation"]) == (1, 2)
        _backdate(store, lease_b["lease_id"], 601)
        again = _claim(store, B, item, run_b, "gen-count-b")["lease"]
        assert (again["lease_id"], again["generation"]) == (lease_b["lease_id"], 2)
        state = _read(store, item)
        assert {l["lease_id"]: l["generation"] for l in state["leases"]} == {
            lease_a["lease_id"]: 1, lease_b["lease_id"]: 2,
        }
        assert state["current_lease"]["generation"] == 2


# ---------------------------------------------------------------------------
# agentops#2529: the remaining lease findings from the review of sprintctl#98.
# m2: a release to pending ends the item's active lease in the same
#     transaction (state=released, end_reason=item-released-<reason>).
# m3: heartbeat_lease takes the repo claims lock (repo, then item, then lease).
# n1: the schema-18 foreign check compares each named index's key columns.
# ---------------------------------------------------------------------------

_RELEASE_REASONS = ("rework", "partial", "abandoned")
_RELEASE_SOURCES = ("active", "blocked")
_RELEASE_PATHS = ("pg", "authority")


def _payload(value) -> dict:
    return json.loads(value) if isinstance(value, str) else value


def _release_to_pending(store, item_id: int, reason: str, path: str, tmp_path) -> None:
    """Move an item to pending through one of the two writers: the local
    pg.set_work_item_status or the authority (outbox command) path."""
    if path == "pg":
        pg.set_work_item_status(store, item_id, "pending", reason=reason, actor="releaser")
        return
    item = pg.get_work_item(store, item_id)
    producer = outbox.open_outbox(tmp_path / f"release-{uuid.uuid4().hex[:8]}.db")
    try:
        command = _append_authority_command(
            producer,
            store,
            record_type="item.transition",
            aggregate_type="item",
            aggregate_uuid=item["aggregate_uuid"],
            basis_revision=authority.item_revision(item),
            payload={"to_status": "pending", "reason": reason},
            actor="releaser",
        )
        decision = authority.arbitrate_command(store, command)
    finally:
        producer.close()
    assert decision.accepted is True, decision
    # The authority effect/receipt shape does not change.
    assert "released_lease_ids" not in decision.effect
    assert decision.effect["status"] == "pending"


def _lease_row_of(store, lease_id: str) -> dict:
    with store.conn.cursor() as cur:
        cur.execute(
            "SELECT state, ended_at, end_reason, superseded_by FROM work_lease "
            "WHERE repo_id = %s AND lease_id = %s",
            (store.repo_id, lease_id),
        )
        row = dict(cur.fetchone())
    store.conn.rollback()
    return row


def _last_release_event(store, item_id: int) -> dict:
    events = _events(store, item_id, "item-released")
    assert events, "no item-released event was recorded"
    return _payload(events[-1])


def _claimed(store, source: str, key: str) -> tuple[int, str, dict]:
    (item,) = _items(store)
    run = _run(store, A)
    lease = _claim(store, A, item, run, key)["lease"]
    if source == "blocked":
        pg.set_work_item_status(store, item, "blocked")
    assert _status(store, item) == source
    return item, run, lease


def _authority_scoped(store, pg_test_scope, label: str) -> pg.PgStore:
    repo_id = pg_test_scope(label)
    return pg.PgStore(
        conn=store.conn,
        repo_id=repo_id,
        authority_repo_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{repo_id}")),
    )


class TestReleaseToPendingEndsTheLease:
    """m2: every transition to pending ends the item's active lease, whoever
    holds it, in the same transaction as the status update."""

    @pytest.mark.parametrize("path", _RELEASE_PATHS)
    @pytest.mark.parametrize("source", _RELEASE_SOURCES)
    @pytest.mark.parametrize("reason", _RELEASE_REASONS)
    def test_a_release_ends_the_lease_and_frees_the_item_at_once(
        self, store, tmp_path, reason, source, path
    ):
        key = f"rel-{reason}-{source}-{path}-{uuid.uuid4().hex[:6]}"
        item, run_a, lease = _claimed(store, source, key + "-a")
        _release_to_pending(store, item, reason, path, tmp_path)
        assert _status(store, item) == "pending"

        row = _lease_row_of(store, lease["lease_id"])
        assert row["state"] == "released"
        assert row["ended_at"] is not None
        assert row["end_reason"] == f"item-released-{reason}"
        assert row["superseded_by"] is None
        assert _lease_rows(store, item, "active") == 0
        read = _read(store, item)
        assert read["current_lease"] is None
        (ended,) = [l for l in read["leases"] if l["lease_id"] == lease["lease_id"]]
        assert (ended["state"], ended["end_reason"]) == ("released", f"item-released-{reason}")
        assert ended["ended_at"] is not None

        released = _last_release_event(store, item)
        assert released["reason"] == reason and released["previous_status"] == source
        assert released["released_lease_ids"] == [lease["lease_id"]]

        # The former holder's heartbeat is refused: its lease has ended.
        refused = _refused(lambda: _heartbeat(store, A, lease["lease_id"], run_a))
        assert refused.code == "lease-ended"

        # Another principal claims immediately: no lease-held, no takeover.
        taken = _claim(store, B, item, _run(store, B), key + "-b")
        assert taken["took_over"] is None
        assert taken["lease"]["takeover_of"] is None
        assert taken["lease"]["state"] == "active"
        assert _events(store, item, "work.claim.taken-over") == []
        assert _lease_rows(store, item, "active") == 1
        assert _status(store, item) == "active"

    @pytest.mark.parametrize("path", _RELEASE_PATHS)
    @pytest.mark.parametrize("source", _RELEASE_SOURCES)
    @pytest.mark.parametrize("reason", _RELEASE_REASONS)
    def test_maintenance_activates_right_after_a_release(
        self, store, pg_test_scope, tmp_path, reason, source, path
    ):
        scoped = _authority_scoped(store, pg_test_scope, "lease-release-maint")
        item, _run_a, lease = _claimed(scoped, source, f"rel-maint-{reason}-{source}-{path}")
        _release_to_pending(scoped, item, reason, path, tmp_path)
        assert _lease_row_of(scoped, lease["lease_id"])["state"] == "released"
        # A released item's lease no longer counts as live work.
        TestMaintenance._activate(*TestMaintenance._attested(scoped))

    @pytest.mark.parametrize("path", _RELEASE_PATHS)
    def test_a_failure_after_the_lease_update_rolls_everything_back(
        self, store, tmp_path, monkeypatch, path
    ):
        item, _run_a, lease = _claimed(store, "active", f"rel-atomic-{path}-{uuid.uuid4().hex[:6]}")
        original = pg._insert_event

        def failing_insert_event(*args, **kwargs):
            event_type = args[3] if len(args) > 3 else kwargs.get("event_type")
            if event_type == "item-released":
                raise RuntimeError("injected failure writing item-released")
            return original(*args, **kwargs)

        monkeypatch.setattr(pg, "_insert_event", failing_insert_event)
        with pytest.raises(RuntimeError, match="injected failure"):
            _release_to_pending(store, item, "rework", path, tmp_path)
        monkeypatch.setattr(pg, "_insert_event", original)
        store.conn.rollback()

        row = _lease_row_of(store, lease["lease_id"])
        assert (row["state"], row["ended_at"], row["end_reason"]) == ("active", None, None)
        assert _status(store, item) == "active"
        assert _events(store, item, "item-released") == []

        # The retry, with nothing failing, ends the lease.
        _release_to_pending(store, item, "rework", path, tmp_path)
        assert _lease_row_of(store, lease["lease_id"])["end_reason"] == "item-released-rework"
        assert _last_release_event(store, item)["released_lease_ids"] == [lease["lease_id"]]

    @pytest.mark.parametrize("path", _RELEASE_PATHS)
    def test_a_release_with_no_lease_records_an_empty_list(self, store, tmp_path, path):
        (item,) = _items(store)
        pg.set_work_item_status(store, item, "active")
        _release_to_pending(store, item, "partial", path, tmp_path)
        assert _status(store, item) == "pending"
        assert _lease_rows(store, item) == 0
        assert _last_release_event(store, item)["released_lease_ids"] == []

    def test_moving_to_blocked_keeps_the_lease(self, store):
        """Non-scope guard: active -> blocked is not a release."""
        item, _run_a, lease = _claimed(store, "blocked", f"rel-blocked-{uuid.uuid4().hex[:6]}")
        assert _lease_row_of(store, lease["lease_id"])["state"] == "active"


def _heartbeat_under_held_repo_lock(store, wait: float = 0.5) -> tuple[bool, object]:
    """Hold the repo claims lock on an independent connection, heartbeat a
    fresh lease on another, and report whether the heartbeat returned while
    the lock was still held, plus its eventual outcome."""
    (item,) = _items(store)
    run = _run(store, A)
    lease = _claim(store, A, item, run, f"hb-lock-{uuid.uuid4().hex[:8]}")["lease"]
    holder = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(holder)
    worker = _sibling(store)
    outcome: dict = {}

    def beat():
        try:
            outcome["value"] = _heartbeat(worker, A, lease["lease_id"], run)
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome["value"] = exc

    thread = threading.Thread(target=beat)
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (store.repo_id,))
        thread.start()
        thread.join(timeout=wait)
        returned_while_held = not thread.is_alive()
        holder.rollback()  # ends the transaction, releasing the xact lock
        thread.join(timeout=30)
        assert not thread.is_alive(), "heartbeat never returned after the repo lock was released"
    finally:
        try:
            holder.rollback()
        finally:
            holder.close()
            if thread.is_alive():
                thread.join(timeout=30)
            worker.conn.close()
    result = outcome.get("value")
    if isinstance(result, dict):
        assert result["lease"]["lease_id"] == lease["lease_id"]
    return returned_while_held, result


class TestHeartbeatTakesTheRepoLock:
    """m3: a heartbeat serializes with claims and maintenance activation on
    the repo claims lock, so it cannot leave a live lease under an active
    maintenance capability."""

    def test_a_heartbeat_waits_for_the_repo_claims_lock(self, store):
        started = time.monotonic()
        returned_while_held, result = _heartbeat_under_held_repo_lock(store, wait=0.5)
        assert returned_while_held is False, (
            "heartbeat_lease returned while another transaction held the repo claims lock"
        )
        assert time.monotonic() - started > 0.5
        assert isinstance(result, dict), repr(result)
        assert result["lease"]["state"] == "active"

    def test_without_the_repo_lock_the_heartbeat_does_not_wait(self, store, monkeypatch):
        """Forced-failure check: with _lock_repo_for_claims removed from the
        heartbeat, the probe above sees the heartbeat return under the lock."""
        monkeypatch.setattr(pg, "_lock_repo_for_claims", lambda cur, store: None)
        returned_while_held, result = _heartbeat_under_held_repo_lock(store, wait=0.5)
        assert returned_while_held is True
        assert isinstance(result, dict), repr(result)


class TestSchema18IndexKeyColumns:
    """n1: an exclusivity index whose key columns differ from the pinned
    (repo_id, work_item_id) is foreign, even when unique and partial."""

    @pytest.mark.parametrize("columns", [
        "repo_id, work_item_id, lease_id",
        "work_item_id, repo_id",
        "repo_id, lease_id",
    ])
    def test_a_wrongly_keyed_unique_exclusivity_index_is_foreign(self, store, columns):
        schema = "migration_18_keys_" + uuid.uuid4().hex
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
            pg_migrations.migrate_schema(pg.PgStore(conn, "migration-18-keys"))
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                cur.execute("DROP INDEX uq_work_lease_active_item")
                cur.execute(
                    f"CREATE UNIQUE INDEX uq_work_lease_active_item ON work_lease({columns}) "
                    "WHERE state = 'active'"
                )
                with pytest.raises(pg_migrations.RemoteSchemaMigrationError, match="uq_work_lease_active_item"):
                    pg._apply_schema_version_18(cur)
            conn.rollback()
        finally:
            conn.close()
            with store.conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            store.conn.commit()


class TestParkedDisposition:
    """agentops#2543 (agentops#253 lease point 7): a denial is a fact about
    the work, not a lease state.  ``report-outcome-v1`` with
    ``outcome=failed, disposition=parked, reason_ref`` releases the lease and
    appends a ``work.parked`` item event that claims and readiness respect,
    until a release to pending lifts it."""

    def _park(self, store, key: str, *, context=A, reason_ref="agentops#253:denied",
              operation="work.lease.report-outcome-v1"):
        (item,) = _items(store)
        run = _run(store, context)
        lease = _claim(store, context, item, run, f"{key}-a")["lease"]["lease_id"]
        result = _invoke(store, operation, {
            "lease_id": lease, "run_id": run, "outcome": "failed", "summary": "denied",
            "disposition": "parked", "reason_ref": reason_ref, "idempotency_key": f"{key}-c",
        }, context)
        return item, run, lease, result

    def test_a_parked_failure_releases_the_lease_and_records_work_parked(self, store):
        item, _run_id, lease, result = self._park(store, "park-basic")
        assert (result["settled"], result["settlement_effect"]) == (False, "parked")
        report = result["report"]
        assert (report["outcome"], report["disposition"]) == ("failed", "recorded")
        assert report["parked"]["reason_ref"] == "agentops#253:denied"
        assert _status(store, item) == "active"
        row = _scalar(
            store, "SELECT end_reason FROM work_lease WHERE repo_id = %s AND lease_id = %s",
            (store.repo_id, lease),
        )
        assert row == "reported-parked"
        (event,) = _events(store, item, "work.parked")
        assert event["reason_ref"] == "agentops#253:denied"
        assert event["report_id"] == report["report_id"] and event["lease_id"] == lease
        state = _read(store, item)
        assert state["current_lease"] is None
        assert state["parked"]["reason_ref"] == "agentops#253:denied"
        assert state["parked"]["event_id"] == report["parked"]["event_id"]

    def test_a_parked_item_is_not_claimable(self, store):
        item, *_ = self._park(store, "park-claim")
        for who in (A, B):
            refused = _refused(lambda: _claim(store, who, item, _run(store, who), f"park-claim-{who.identity.principal_id}"))
            assert refused.code == "work-parked" and refused.http_status == 409
        assert _lease_rows(store, item, "active") == 0

    def test_a_plain_failure_is_still_claimable(self, store):
        (item,) = _items(store)
        run = _run(store, A)
        lease = _claim(store, A, item, run, "plain-fail-a")["lease"]["lease_id"]
        result = _report(store, A, lease, run, "plain-fail-c", outcome="failed")
        assert result["settlement_effect"] == "lease-released" and "parked" not in result["report"]
        assert _events(store, item, "work.parked") == []
        assert _read(store, item)["parked"] is None
        assert _claim(store, B, item, _run(store, B), "plain-fail-b")["took_over"] is None

    def test_a_parked_item_is_not_ready_even_when_left_pending(self, store):
        item, *_ = self._park(store, "park-ready")
        sprint_id = pg.get_work_item(store, item)["sprint_id"]
        # Moved off active without the release event (a direct row change):
        # the parking still holds it out of readiness.
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE work_item SET status = 'pending' WHERE repo_id = %s AND id = %s",
                (store.repo_id, item),
            )
        store.conn.commit()
        assert item not in [ready["id"] for ready in pg.get_ready_items(store, sprint_id)]

    def test_a_release_to_pending_lifts_the_parking(self, store):
        item, *_ = self._park(store, "park-lift")
        sprint_id = pg.get_work_item(store, item)["sprint_id"]
        pg.set_work_item_status(store, item, "pending", "operator", reason="partial")
        assert _read(store, item)["parked"] is None
        assert item in [ready["id"] for ready in pg.get_ready_items(store, sprint_id)]
        assert _claim(store, B, item, _run(store, B), "park-lift-b")["took_over"] is None

    def test_a_later_report_parks_the_released_work_again(self, store):
        item, *_ = self._park(store, "park-again")
        pg.set_work_item_status(store, item, "pending", "operator", reason="rework")
        run = _run(store, B)
        lease = _claim(store, B, item, run, "park-again-b")["lease"]["lease_id"]
        second = _invoke(store, "work.lease.report-outcome-v1", {
            "lease_id": lease, "run_id": run, "outcome": "failed", "disposition": "parked",
            "reason_ref": "run:2", "idempotency_key": "park-again-c2",
        }, B)
        assert second["settlement_effect"] == "parked"
        assert len(_events(store, item, "work.parked")) == 2
        assert _read(store, item)["parked"]["reason_ref"] == "run:2"
        assert _refused(lambda: _claim(store, A, item, _run(store, A), "park-again-a3")).code == "work-parked"

    def test_the_deprecated_alias_parks_too(self, store):
        item, *_ = self._park(store, "park-alias", operation="work.lease.complete-v1")
        assert _read(store, item)["parked"]["reason_ref"] == "agentops#253:denied"

    def test_a_replayed_key_answers_as_the_first_call_did(self, store):
        item, run, lease, first = self._park(store, "park-replay")
        again = _invoke(store, "work.lease.report-outcome-v1", {
            "lease_id": lease, "run_id": run, "outcome": "failed", "summary": "denied",
            "disposition": "parked", "reason_ref": "agentops#253:denied",
            "idempotency_key": "park-replay-c",
        }, A)
        assert again == first
        assert len(_events(store, item, "work.parked")) == 1 and _reports(store, item) == 1
        # Same key, different park request: a conflict, never a silent re-park.
        changed = _refused(lambda: _invoke(store, "work.lease.report-outcome-v1", {
            "lease_id": lease, "run_id": run, "outcome": "failed", "summary": "denied",
            "idempotency_key": "park-replay-c",
        }, A))
        assert changed.code == "idempotency-conflict"

    def test_the_direct_replay_reports_the_parking_too(self, store):
        item, run, lease, first = self._park(store, "park-direct")
        replayed = pg.complete_lease(
            store, lease_id=lease, run_id=run, principal_id="github:100:0", workspace_id="ws-1",
            client_id=None, grant_id=None, idempotency_key="park-direct-c",
            request_digest=_scalar(
                store, "SELECT request_digest FROM work_outcome_report WHERE repo_id = %s "
                "AND report_id = %s", (store.repo_id, first["report"]["report_id"]),
            ),
            outcome="failed", summary="denied", payload={}, checks=[], actor="x",
            outcome_disposition="parked", reason_ref="agentops#253:denied",
        )
        assert replayed["parked"]["reason_ref"] == "agentops#253:denied"

    def test_a_superseded_holder_cannot_park_the_work(self, store):
        """INV-L1: a rejected report is evidence, not an action on the work."""
        (item,) = _items(store)
        run_a = _run(store, A)
        lease_a = _claim(store, A, item, run_a, "park-sup-a")["lease"]
        _backdate(store, lease_a["lease_id"], 601)
        run_b = _run(store, B)
        lease_b = _claim(store, B, item, run_b, "park-sup-b")["lease"]
        refused = _refused(lambda: _invoke(store, "work.lease.report-outcome-v1", {
            "lease_id": lease_a["lease_id"], "run_id": run_a, "outcome": "failed",
            "disposition": "parked", "reason_ref": "stale", "idempotency_key": "park-sup-c",
        }, A))
        assert refused.code == "claim-superseded"
        assert _events(store, item, "work.parked") == []
        assert _read(store, item)["parked"] is None
        assert _read(store, item)["current_lease"]["lease_id"] == lease_b["lease_id"]

    @pytest.mark.parametrize("bad", [
        {"outcome": "succeeded", "disposition": "parked", "reason_ref": "r"},
        {"outcome": "failed", "disposition": "parked"},
        {"outcome": "failed", "disposition": "parked", "reason_ref": ""},
        {"outcome": "failed", "disposition": "parked", "reason_ref": " padded "},
        {"outcome": "failed", "disposition": "parked", "reason_ref": "x" * 513},
        {"outcome": "failed", "reason_ref": "r"},
        {"outcome": "failed", "disposition": "shelved", "reason_ref": "r"},
    ])
    def test_a_malformed_park_request_is_refused_and_changes_nothing(self, store, bad):
        (item,) = _items(store)
        run = _run(store, A)
        tag = uuid.uuid4().hex[:12]
        lease = _claim(store, A, item, run, f"park-bad-a-{tag}")["lease"]["lease_id"]
        rejected = _refused(lambda: _app(store).invoke("work.lease.report-outcome-v1", {
            "lease_id": lease, "run_id": run, "idempotency_key": f"park-bad-c-{tag}", **bad,
        }, A))
        assert rejected.code == "invalid-arguments"
        assert _reports(store, item) == 0 and _events(store, item, "work.parked") == []
        assert _lease_rows(store, item, "active") == 1

    def test_the_generic_event_writer_cannot_forge_a_parking(self, store):
        (item,) = _items(store)
        sprint_id = pg.get_work_item(store, item)["sprint_id"]
        with pytest.raises(ValueError, match="work.parked is reserved"):
            pg.create_event(store, sprint_id, "anyone", "work.parked", work_item_id=item,
                            payload={"reason_ref": "forged"})
        assert _events(store, item, "work.parked") == []
