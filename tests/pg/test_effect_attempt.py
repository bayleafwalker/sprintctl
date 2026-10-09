"""Actual owner transactions on isolated disposable PostgreSQL schemas."""
from __future__ import annotations

import copy
import threading
import time
import uuid

import pytest
import jsonschema

from sprintctl import effect_attempt as contract, effect_attempt_pg, effect_attempt_schema, pg, pg_migrations
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg
from tests.pg.test_effect_intent import _accept, _app as _owner_application, _context, _get
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.pg.test_protected_artifact_acceptance import setup, receipt, accept
from tests.pg.test_releases import _edit

pytestmark = PG_MARKS
APPLIER = _context("fixture-pure-applier", {"work.effect.mark-applied"})
CONTRACTS = {c.name: c for c in WORK_OPERATION_CONTRACTS}


class ContractApplication:
    def __init__(self, store):
        self.application = _owner_application(store)

    def invoke(self, operation, arguments, context):
        jsonschema.validate(arguments, CONTRACTS[operation].input_schema)
        result = self.application.invoke(operation, arguments, context)
        jsonschema.validate(result, CONTRACTS[operation].result_schema)
        return result


def _app(store):
    return ContractApplication(store)


@pytest.fixture(scope="module")
def attempts_store(pg_test_scope, store):
    schema = "attempt_oracle_" + uuid.uuid4().hex
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
    conn.commit()

    def factory():
        sibling = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(sibling)
        with sibling.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
        sibling.commit()
        return sibling

    isolated = pg.PgStore(conn, pg_test_scope("attempt-oracle"), connection_factory=factory)
    try:
        pg_migrations.migrate_schema(isolated)
        yield isolated
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


def prepared(store, *, pr=False):
    item, reservation, intent = setup(store, required=False)
    _accept(store, intent)
    release = pg.get_release(store, reservation["release_digest"])
    target = {"operation": "open_pull_request" if pr else "push_branch",
              "branch": "custom/prefix/" + intent["intent_id"], "commit_sha": "b" * 40}
    if pr:
        target["base_branch"] = "main"
    arguments = {k: intent[k] for k in ("intent_id", "revision", "canonical_intent_digest")}
    arguments.update(expected_revision=release["item_revision"], release_digest=release["release_digest"],
                     target=target, idempotency_key="open-" + uuid.uuid4().hex)
    return item, intent, arguments


def opened(store, *, pr=False):
    item, intent, arguments = prepared(store, pr=pr)
    result = _app(store).invoke(contract.OPERATION_OPEN, arguments, APPLIER)
    consume = {"attempt_id": result["authorization"]["attempt_id"],
               "authorization_digest": result["authorization_digest"],
               "idempotency_key": "consume-" + uuid.uuid4().hex}
    return item, intent, arguments, result, consume


def current(store, consume, context=APPLIER):
    return _app(store).invoke(contract.OPERATION_GET, {"attempt_id": consume["attempt_id"]}, context)


def test_pure_applier_is_bound_without_proposer_run_and_targets_have_separate_grants(attempts_store):
    store = attempts_store
    _, intent, args, result, consume = opened(store)
    assert result["authorization"]["principal_id"] == APPLIER.identity.principal_id
    assert result["authorization"]["acceptance"] == _get(store, intent["intent_id"])["acceptance"]
    assert current(store, consume)["state"] == "accepted"
    with store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM run WHERE repo_id=%s AND principal_id=%s",
                    (store.repo_id, APPLIER.identity.principal_id))
        assert cur.fetchone()["n"] == 0
    store.conn.rollback()
    pr_args = {**args, "target": {**args["target"], "operation": "open_pull_request", "base_branch": "main"},
               "idempotency_key": "pr-" + uuid.uuid4().hex}
    pr = _app(store).invoke(contract.OPERATION_OPEN, pr_args, APPLIER)
    assert pr["authorization_digest"] != result["authorization_digest"]


def test_required_protected_verification_is_frozen_for_pure_applier(attempts_store):
    store = attempts_store
    item, reservation, intent = setup(store, required=True)
    release = pg.get_release(store, reservation["release_digest"])
    ref, _, _ = receipt(store, intent, release)
    accepted = accept(_owner_application(store), intent, ref)
    with store.conn.cursor() as cur:
        cur.execute("SELECT row_to_json(e) AS row FROM evidence_item e WHERE repo_id=%s ORDER BY item_id", (store.repo_id,))
        before_evidence = cur.fetchall()
    store.conn.rollback()
    arguments = {k: intent[k] for k in ("intent_id", "revision", "canonical_intent_digest")}
    arguments.update(expected_revision=release["item_revision"], release_digest=release["release_digest"],
        target={"operation": "push_branch", "branch": "verified/" + intent["intent_id"], "commit_sha": "b" * 40},
        idempotency_key="verified-open-" + uuid.uuid4().hex)
    result = _app(store).invoke(contract.OPERATION_OPEN, arguments, APPLIER)
    frozen = accepted["acceptance"]["verification"]
    assert frozen is not None and result["authorization"]["verification_binding"] == frozen
    consume = {"attempt_id": result["authorization"]["attempt_id"],
               "authorization_digest": result["authorization_digest"], "idempotency_key": "verified-redeem-" + uuid.uuid4().hex}
    assert _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)["dispatch_permitted"] is True
    assert current(store, consume)["authorization"]["verification_binding"] == frozen
    assert APPLIER.identity.authorities == {"work.effect.mark-applied"}
    assert _get(store, intent["intent_id"]) == accepted
    with store.conn.cursor() as cur:
        cur.execute("SELECT row_to_json(e) AS row FROM evidence_item e WHERE repo_id=%s ORDER BY item_id", (store.repo_id,))
        assert cur.fetchall() == before_evidence
    store.conn.rollback()


def test_lost_redemption_reply_replay_never_regrants_dispatch(attempts_store):
    store = attempts_store
    _, _, _, _, args = opened(store)
    fresh = _app(store).invoke(contract.OPERATION_REDEEM, args, APPLIER)
    assert fresh["dispatch_permitted"] is True and fresh["delivery"] == "fresh"
    replay = _app(store).invoke(contract.OPERATION_REDEEM, args, APPLIER)
    assert replay["dispatch_permitted"] is False and replay["delivery"] == "replay"
    assert replay["receipt"] == fresh["receipt"]
    with store.conn.cursor() as cur:
        cur.execute("SELECT result FROM work_idempotency_ledger WHERE repo_id=%s AND tool=%s AND idempotency_key=%s",
                    (store.repo_id, contract.OPERATION_REDEEM, args["idempotency_key"]))
        saved = cur.fetchone()["result"]
        assert "dispatch_permitted" not in saved and "delivery" not in saved
    store.conn.rollback()
    with pytest.raises(ApplicationRejection, match="already redeemed"):
        _app(store).invoke(contract.OPERATION_REDEEM, {**args, "idempotency_key": "different-" + uuid.uuid4().hex}, APPLIER)
    assert len(current(store, args)["events"]) == 2


@pytest.mark.parametrize("field,value", [("principal_id", "foreign-applier"), ("workspace_id", "foreign-workspace"),
    ("client_id", "foreign-client"), ("grant_id", "foreign-grant")])
def test_foreign_authenticated_binding_cannot_read_or_consume(attempts_store, field, value):
    store = attempts_store
    _, _, _, _, args = opened(store)
    context = copy.deepcopy(APPLIER)
    setattr(context.identity, field, value)
    for operation, arguments in ((contract.OPERATION_GET, {"attempt_id": args["attempt_id"]}),
                                 (contract.OPERATION_REDEEM, args), (contract.OPERATION_SEAL_UNUSED, args)):
        with pytest.raises(ApplicationRejection) as exc:
            _app(store).invoke(operation, arguments, context)
        assert exc.value.code == "effect-attempt-not-found" and exc.value.http_status == 404
    assert current(store, args)["state"] == "accepted"


def test_normal_work_scope_and_outer_cache_key_cannot_authorize(attempts_store):
    store = attempts_store
    _, _, args = prepared(store)
    ordinary = _context("ordinary", {"work:read", "work:write", "work:claim"})
    with pytest.raises(ApplicationRejection) as exc:
        _app(store).invoke(contract.OPERATION_OPEN, args, ordinary)
    assert exc.value.code == "authority-required"
    cached = copy.deepcopy(APPLIER)
    cached.idempotency_key = "outer-cache-key"
    with pytest.raises(ApplicationRejection, match="outer envelope"):
        _app(store).invoke(contract.OPERATION_OPEN, args, cached)


def test_current_basis_change_refuses_new_permission_but_allows_sealing_and_replay(attempts_store):
    store = attempts_store
    item, _, args, authorization, consume = opened(store)
    _edit(pg, store, item, "changed after authorization")
    with pytest.raises(ApplicationRejection) as exc:
        _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
    assert exc.value.code == "effect-release-mismatch"
    sealed = _app(store).invoke(contract.OPERATION_SEAL_UNUSED, consume, APPLIER)
    assert sealed["receipt"]["event_kind"] == "attempt_closed_without_redemption"
    replay = _app(store).invoke(contract.OPERATION_OPEN, args, APPLIER)
    assert replay["authorization_digest"] == authorization["authorization_digest"] and replay["delivery"] == "replay"
    assert current(store, consume)["state"] == "sealed_unused"


def test_authenticated_report_is_immutable_and_does_not_mark_legacy_intent_applied(attempts_store):
    store = attempts_store
    item, intent, _, _, consume = opened(store, pr=True)
    _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
    _edit(pg, store, item, "basis changed after possible provider invocation")
    report = {**consume, "idempotency_key": "report-" + uuid.uuid4().hex,
              "commit_sha": "b" * 40, "pr_url": "https://forge.example/fixture/repository/pulls/1"}
    result = _app(store).invoke(contract.OPERATION_REPORT, report, APPLIER)
    assert result["receipt"]["event_kind"] == "application_report_received"
    assert _get(store, intent["intent_id"])["state"] == "accepted"
    assert _app(store).invoke(contract.OPERATION_REPORT, report, APPLIER)["delivery"] == "replay"
    with pytest.raises(ApplicationRejection):
        _app(store).invoke(contract.OPERATION_REPORT, {**report, "pr_url": report["pr_url"] + "2"}, APPLIER)
    assert len(current(store, consume)["events"]) == 3


def test_redeem_seal_race_has_one_committed_consumer(attempts_store):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def call(operation):
        conn = store.connection_factory()
        sibling = pg.PgStore(conn, store.repo_id, connection_factory=store.connection_factory)
        try:
            app = _app(sibling)
            barrier.wait(timeout=10)
            results.append(app.invoke(operation, {**consume, "idempotency_key": "race-" + uuid.uuid4().hex}, APPLIER))
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=call, args=(op,)) for op in (contract.OPERATION_REDEEM, contract.OPERATION_SEAL_UNUSED)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()
    assert len(results) == 1 and len(errors) == 1
    assert isinstance(errors[0], ApplicationRejection) and errors[0].code == "effect-attempt-already-consumed"
    assert len(current(store, consume)["events"]) == 2


def test_redemption_rollback_removes_state_fact_and_cached_result(attempts_store, monkeypatch):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    event = effect_attempt_pg._event

    def fail_after_transition(*args, **kwargs):
        raise RuntimeError("injected before fact append")

    monkeypatch.setattr(effect_attempt_pg, "_event", fail_after_transition)
    with pytest.raises(RuntimeError, match="before fact append"):
        _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
    assert current(store, consume)["state"] == "accepted"
    with store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM work_idempotency_ledger WHERE repo_id=%s AND tool=%s "
                    "AND idempotency_key=%s", (store.repo_id, contract.OPERATION_REDEEM, consume["idempotency_key"]))
        assert cur.fetchone()["n"] == 0
    store.conn.rollback()
    monkeypatch.setattr(effect_attempt_pg, "_event", event)
    assert _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)["dispatch_permitted"] is True
    assert len(current(store, consume)["events"]) == 2


def test_same_key_concurrent_redemption_has_one_fresh_delivery(attempts_store):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def call():
        conn = store.connection_factory()
        sibling = pg.PgStore(conn, store.repo_id, connection_factory=store.connection_factory)
        try:
            app = _app(sibling)
            barrier.wait(timeout=10)
            results.append(app.invoke(contract.OPERATION_REDEEM, consume, APPLIER))
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()
    assert errors == [] and len(results) == 2
    assert sorted(r["dispatch_permitted"] for r in results) == [False, True]
    assert results[0]["receipt"] == results[1]["receipt"]
    assert len(current(store, consume)["events"]) == 2


def test_replay_after_basis_change_does_not_wait_for_item_or_attempt_locks(attempts_store):
    store = attempts_store
    item, _, _, _, consume = opened(store)
    _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
    _edit(pg, store, item, "changed after committed redemption")
    blocker = store.connection_factory()
    worker = store.connection_factory()
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT id FROM work_item WHERE repo_id=%s AND id=%s FOR UPDATE", (store.repo_id, item))
            cur.execute("SELECT attempt_id FROM work_effect_attempt WHERE repo_id=%s AND attempt_id=%s FOR UPDATE",
                        (store.repo_id, consume["attempt_id"]))
        with worker.cursor() as cur:
            cur.execute("SET statement_timeout='1s'")
        worker.commit()
        sibling = pg.PgStore(worker, store.repo_id, connection_factory=store.connection_factory)
        replay = _app(sibling).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
        assert replay["delivery"] == "replay" and replay["dispatch_permitted"] is False
    finally:
        blocker.rollback()
        blocker.close()
        worker.close()


@pytest.mark.parametrize("field", ["client_id", "grant_id"])
def test_changed_grant_cannot_adopt_committed_replay_key(attempts_store, field):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
    foreign = copy.deepcopy(APPLIER)
    setattr(foreign.identity, field, "changed-binding")
    with pytest.raises(ApplicationRejection) as exc:
        _app(store).invoke(contract.OPERATION_REDEEM, consume, foreign)
    assert exc.value.code == "idempotency-conflict"
    assert len(current(store, consume)["events"]) == 2


@pytest.mark.parametrize("assignment", ["target='{}'::jsonb", "principal_id='foreign'",
    "authorization_digest='" + "0" * 64 + "'", "expected_revision=expected_revision||'0'",
    "state='redeemed',redeemed_at=clock_timestamp()"])
def test_direct_sql_immutability_and_missing_fact_commit_refusals(attempts_store, assignment):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    with pytest.raises(psycopg.errors.CheckViolation):
        with store.conn.cursor() as cur:
            cur.execute(f"UPDATE work_effect_attempt SET {assignment} WHERE repo_id=%s AND attempt_id=%s",
                        (store.repo_id, consume["attempt_id"]))
        store.conn.commit()
    store.conn.rollback()
    assert current(store, consume)["state"] == "accepted" and len(current(store, consume)["events"]) == 1


@pytest.mark.parametrize("table,operation", [("work_effect_attempt", "DELETE FROM"),
    ("work_effect_attempt_event", "DELETE FROM"), ("work_effect_attempt", "TRUNCATE"),
    ("work_effect_attempt_event", "TRUNCATE")])
def test_attempt_and_events_cannot_be_removed(attempts_store, table, operation):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.FeatureNotSupported)):
        with store.conn.cursor() as cur:
            cur.execute(operation + " " + table + (" CASCADE" if operation == "TRUNCATE" else ""))
        store.conn.commit()
    store.conn.rollback()
    assert current(store, consume)["state"] == "accepted"


@pytest.mark.parametrize("opening", [True, False])
def test_different_key_concurrent_authorization_or_redemption_has_measured_intent_blocker(attempts_store, opening):
    store = attempts_store
    if opening:
        _, intent, args = prepared(store)
        operation = contract.OPERATION_OPEN
    else:
        _, intent, _, _, args = opened(store)
        operation = contract.OPERATION_REDEEM
    blocker = store.connection_factory()
    monitor = store.connection_factory()
    results, errors, pids = [], [], []
    barrier = threading.Barrier(3)

    def call(n):
        conn = store.connection_factory()
        sibling = pg.PgStore(conn, store.repo_id, connection_factory=store.connection_factory)
        try:
            app = _app(sibling)
            pids.append(conn.info.backend_pid)
            context = _context("other-pure-applier", {"work.effect.mark-applied"}) if opening and n else APPLIER
            barrier.wait(timeout=10)
            results.append(app.invoke(operation, {**args, "idempotency_key": "distinct-" + uuid.uuid4().hex}, context))
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=call, args=(n,)) for n in range(2)]
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT intent_id FROM work_effect_intent WHERE repo_id=%s AND intent_id=%s FOR UPDATE",
                        (store.repo_id, intent["intent_id"]))
        for t in threads:
            t.start()
        barrier.wait(timeout=10)
        observed = set()
        limit = time.monotonic() + 5
        while time.monotonic() < limit and len(observed) != 2:
            with monitor.cursor() as cur:
                cur.execute("SELECT pid,pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                            "WHERE pid=ANY(%s)", (pids,))
                graph = {r["pid"]: r["blockers"] for r in cur.fetchall()}
                # A later tuple-lock waiter can wait on the earlier waiter,
                # which itself waits on the held transaction. Measure that
                # actual blocker chain rather than assuming two direct edges.
                for pid in pids:
                    pending, seen = list(graph.get(pid, [])), set()
                    while pending:
                        blocking = pending.pop()
                        if blocking == blocker.info.backend_pid:
                            observed.add(pid)
                            break
                        if blocking not in seen:
                            seen.add(blocking)
                            pending.extend(graph.get(blocking, []))
            monitor.rollback()
            if len(observed) != 2:
                time.sleep(0.01)
        assert observed == set(pids) and len(observed) == 2
    finally:
        blocker.rollback()
        blocker.close()
        monitor.close()
        for t in threads:
            if t.ident is not None:
                t.join(timeout=15)
    assert all(not t.is_alive() for t in threads)
    assert len(results) == 1 and len(errors) == 1
    assert isinstance(errors[0], ApplicationRejection)
    assert errors[0].code == ("effect-attempt-already-authorized" if opening else "effect-attempt-already-consumed")
    if not opening:
        assert results[0]["dispatch_permitted"] is True
        assert len(current(store, args)["events"]) == 2


def test_get_snapshot_cannot_mix_old_state_with_new_history(attempts_store, monkeypatch):
    store = attempts_store
    _, _, _, _, consume = opened(store)
    entered, resume = threading.Event(), threading.Event()
    owned = effect_attempt_pg._owned
    results, errors = [], []

    def pause_after_parent_read(*args, **kwargs):
        row = owned(*args, **kwargs)
        if threading.current_thread().name == "attempt-snapshot-reader":
            entered.set()
            assert resume.wait(timeout=10)
        return row

    monkeypatch.setattr(effect_attempt_pg, "_owned", pause_after_parent_read)

    def read():
        conn = store.connection_factory()
        sibling = pg.PgStore(conn, store.repo_id, connection_factory=store.connection_factory)
        try:
            app = _app(sibling)
            with conn.cursor() as cur:
                cur.execute("SELECT 42 AS caller_transaction")
            status = conn.info.transaction_status
            results.append(app.invoke(contract.OPERATION_GET, {"attempt_id": consume["attempt_id"]}, APPLIER))
            assert conn.info.transaction_status == status
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=read, name="attempt-snapshot-reader")
    thread.start()
    try:
        assert entered.wait(timeout=10)
        _app(store).invoke(contract.OPERATION_REDEEM, consume, APPLIER)
    finally:
        resume.set()
        thread.join(timeout=15)
    assert not thread.is_alive() and errors == []
    assert results[0]["state"] == "accepted" and len(results[0]["events"]) == 1
    assert current(store, consume)["state"] == "redeemed" and len(current(store, consume)["events"]) == 2
