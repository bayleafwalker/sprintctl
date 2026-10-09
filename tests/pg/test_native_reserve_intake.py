"""Real native owner histories through the reserve-only offline carrier."""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
import json
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest

from sprintctl import outbox, pg, reserve_intake as intake, served
from sprintctl.application import ApplicationRejection, WorkApplication
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg
from tests.pg.test_run_evidence import _context, _new_run
from tests.pg.test_work_lease import _items
from tests.test_native_reserve_intake import capture, payload

pytestmark = PG_MARKS


def setup(store, tmp_path, pg_test_scope):
    store = replace(store, repo_id=pg_test_scope("reserve-carrier"))
    app = WorkApplication.postgres(store)
    ctx = _context(actor="actor")
    ctx.identity.authorities = {"work:read", "work:write", "work:evidence"}
    run = _new_run(app, "reserve-carrier-" + str(uuid.uuid4()), context=ctx)
    binding = app.invoke(intake.RESOLVE, {"run_id": run}, ctx)
    (item,) = _items(store)
    request, _ = payload(store.repo_id)
    request["arguments"].update(item_id=item, expected_revision=pg.item_release_revision(store, item))
    return store, app, ctx, binding, tmp_path / "producer.db", request


def invoke(app, ctx):
    def call(operation, args, key):
        actual = SimpleNamespace(**vars(ctx)); actual.idempotency_key = key
        return app.invoke(operation, args, actual)
    return call


def sync(path, binding, call):
    return intake.synchronize(path, repo_id=binding["repo_id"], invoke=call, rejection_type=ApplicationRejection)


def effects(store):
    with store.conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM reservation WHERE repo_id=%s) AS rows, "
                    "(SELECT count(*) FROM event WHERE repo_id=%s AND event_type='reservation.reserved') AS events, "
                    "(SELECT count(*) FROM work_idempotency_ledger WHERE repo_id=%s AND tool='reservation.reserve-v1') AS keys",
                    (store.repo_id,) * 3)
        return dict(cur.fetchone())


@pytest.mark.parametrize("protected", [False, True])
def test_reserve_carrier_offline_lost_reply_and_exact_recovery(store, tmp_path, pg_test_scope, protected):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    if protected: request["arguments"]["acceptance_contract"] = {"review_required": True, "effect_verification_required": True}
    captured = capture(path, request, binding)
    assert effects(store) == {"rows": 0, "events": 0, "keys": 0}
    assert not pg.list_releases(store, request["arguments"]["item_id"])
    call = invoke(app, ctx)
    def lost(op, args, key):
        result = call(op, args, key)
        if op == intake.OPERATION:
            raise OSError("lost after real owner commit")
        return result
    assert sync(path, binding, lost)["reserve_attempts"][0]["phase"] == "unknown"
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}
    result = sync(path, binding, call)
    assert result["confirmed_reserve_request_ids"] == [captured["request_id"]]
    assert not result["pending_reserve_request_ids"]
    assert sync(path, binding, lambda *a: pytest.fail("confirmed request invoked again"))["reserve_attempts"] == []
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}
    conn = outbox.open_outbox(path)
    record = conn.execute("SELECT result_json,result_sha256 FROM native_reserve_attempt WHERE phase='confirmed'").fetchone()
    assert intake._digest(record[0].encode()) == record[1]
    receipt = json.loads(record[0])
    assert receipt["reservation_response"]["reservation"]["replayed"]
    assert receipt["release_response"]["release"]["item_revision"] == request["arguments"]["expected_revision"]
    conn.close()


def test_reserve_carrier_stale_basis_and_head_of_line_refusal(store, tmp_path, pg_test_scope):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    first = capture(path, request, binding)
    later = copy.deepcopy(request); later["idempotency_key"] = "later-reserve"
    second = capture(path, later, binding)
    item = request["arguments"]["item_id"]
    _, edit = pg.get_work_item_with_edit_revision(store, item)
    pg.update_work_item_description(store, item, "changed while offline", expected_revision=edit, actor="editor")
    result = sync(path, binding, invoke(app, ctx))
    assert result["reserve_attempts"] == [{"request_id": first["request_id"], "phase": "rejected", "operation": intake.OPERATION, "code": "stale-basis"}]
    assert result["pending_reserve_request_ids"] == [first["request_id"], second["request_id"]]
    assert effects(store) == {"rows": 0, "events": 0, "keys": 0}
    request["arguments"]["expected_revision"] = pg.item_release_revision(store, item)
    with pytest.raises(ValueError, match="already captured"):
        capture(path, request, binding)


@pytest.mark.parametrize("failure", ["row", "snapshot", "overlap", "release", "digest", "release-reply-loss"])
def test_reserve_carrier_unrelated_or_missing_receipts_remain_unconfirmed(store, tmp_path, pg_test_scope, failure):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    captured = capture(path, request, binding)
    call = invoke(app, ctx)
    def broken(op, args, key):
        result = copy.deepcopy(call(op, args, key))
        if op == intake.OPERATION:
            if failure == "row": result["reservation"]["id"] += 1
            if failure == "snapshot": result["reservation"]["admission_snapshot"]["session_id"] = "unrelated"
            if failure == "overlap": result["reservation"]["conflict_severity"] = "warning"
        if op == intake.RELEASE:
            if failure == "release": result["release"]["item_revision"] += "changed"
            if failure == "digest": result["release"]["context_refs"].append({"ref_type": "doc", "url": "changed", "label": "changed"})
            if failure == "release-reply-loss": raise OSError("lost after reserve and Release read")
        return result
    result = sync(path, binding, broken)
    assert result["pending_reserve_request_ids"] == [captured["request_id"]]
    assert result["reserve_attempts"][0]["phase"] == "unknown"
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}
    assert sync(path, binding, call)["confirmed_reserve_request_ids"] == [captured["request_id"]]


@pytest.mark.parametrize("role", ["observation", "verification"])
def test_reserve_carrier_nonexecution_never_claims_a_release(store, tmp_path, pg_test_scope, role):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    request["arguments"]["role"] = role
    captured = capture(path, request, binding)
    call = invoke(app, ctx); operations = []
    def observed(op, args, key):
        operations.append(op); return call(op, args, key)
    assert sync(path, binding, observed)["confirmed_reserve_request_ids"] == [captured["request_id"]]
    assert operations == [intake.RESOLVE, intake.OPERATION]
    assert not pg.list_releases(store, request["arguments"]["item_id"])


def test_reserve_carrier_damaged_confirmed_receipt_cannot_report_empty_pending(store, tmp_path, pg_test_scope):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    capture(path, request, binding)
    assert not sync(path, binding, invoke(app, ctx))["pending_reserve_request_ids"]
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER native_reserve_attempt_update")
    conn.execute("UPDATE native_reserve_attempt SET result_json='{}' WHERE phase='confirmed'")
    conn.commit(); conn.close()
    with pytest.raises(ValueError, match="integrity"):
        intake.status(path)
    with pytest.raises(ValueError, match="integrity"):
        sync(path, binding, lambda *a: pytest.fail("corrupt confirmation must refuse before invoking"))


def test_reserve_carrier_replay_after_reassignment_and_release(store, tmp_path, pg_test_scope):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    captured = capture(path, request, binding)
    call = invoke(app, ctx)
    def lost(op, args, key):
        result = call(op, args, key)
        if op == intake.OPERATION: raise OSError("lost after commit")
        return result
    sync(path, binding, lost)
    row = pg.list_reservations(store, request["arguments"]["item_id"])[0]
    pg.reassign_reservation(store, row["id"], actor="successor", session_id="successor", correlation_ref="later-ref")
    pg.release_reservation(store, row["id"], actor="successor")
    before = pg.get_reservation(store, row["id"])
    assert sync(path, binding, call)["confirmed_reserve_request_ids"] == [captured["request_id"]]
    after = pg.get_reservation(store, row["id"])
    assert after["last_activity_at"] == before["last_activity_at"] and after["state"] == "released"
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}


@pytest.mark.parametrize("failure", ["authority", "actor", "principal", "workspace", "grant"])
def test_reserve_carrier_changed_caller_refuses_without_effect(store, tmp_path, pg_test_scope, failure):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    capture(path, request, binding)
    changed = copy.deepcopy(ctx)
    if failure == "authority": changed.identity.authorities = {"work:evidence", "work:read"}
    if failure == "actor": changed.identity.actor = "other-actor"
    if failure == "principal": changed.identity.principal_id = "github:2:0"
    if failure == "workspace": changed.identity.workspace_id = "ws-other"
    if failure == "grant": changed.identity.grant_id = "different-grant"
    result = sync(path, binding, invoke(app, changed))
    assert result["reserve_attempts"][0]["phase"] in {"rejected", "unknown"}
    assert result["pending_reserve_request_ids"]
    assert effects(store) == {"rows": 0, "events": 0, "keys": 0}


def test_reserve_transport_credential_change_after_resolve_keeps_original_owner(store, tmp_path, pg_test_scope, monkeypatch):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    captured = capture(path, request, binding)
    other = copy.deepcopy(ctx)
    other.identity.principal_id = "github:2:0"
    other.identity.workspace_id = "ws-other"
    available = [ctx]
    resolutions, used = [], []
    def resolve(ref):
        resolutions.append(ref); return available[0]
    class Client:
        def __init__(self, resolver): self.resolver = resolver
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def invoke(self, operation, arguments, **kwargs):
            caller = self.resolver("test-reference")
            used.append(caller.identity.principal_id)
            result = invoke(app, caller)(operation, arguments, kwargs.get("idempotency_key"))
            if operation == intake.RESOLVE: available[0] = other
            return result
    monkeypatch.setattr(served, "resolve_file_credential", resolve)
    monkeypatch.setattr(served, "_client", lambda profile, *, credential_resolver: Client(credential_resolver))
    call = served.native_reserve_invoker(SimpleNamespace(credential_ref="test-reference"), repo_id=store.repo_id)
    assert sync(path, binding, call)["confirmed_reserve_request_ids"] == [captured["request_id"]]
    assert resolutions == ["test-reference"]
    assert used == [ctx.identity.principal_id] * 3
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}
    with store.conn.cursor() as cur:
        cur.execute("SELECT principal_id,workspace_id FROM work_idempotency_ledger WHERE repo_id=%s AND tool='reservation.reserve-v1'", (store.repo_id,))
        row = cur.fetchone()
        assert row == {"principal_id": ctx.identity.principal_id, "workspace_id": ctx.identity.workspace_id}


def test_reserve_carrier_interruption_before_local_confirmation_replays(store, tmp_path, pg_test_scope, monkeypatch):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    captured = capture(path, request, binding)
    original = intake._attempt
    def crash(conn, request_id, attempt, op, phase, **kwargs):
        if phase == "confirmed": raise SystemExit("process boundary before receipt persistence")
        return original(conn, request_id, attempt, op, phase, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(intake, "_attempt", crash)
        with pytest.raises(SystemExit): sync(path, binding, invoke(app, ctx))
    state = intake.status(path)["reserve_request_states"][0]
    assert state["latest_attempt"]["phase"] == "started"
    assert sync(path, binding, invoke(app, ctx))["confirmed_reserve_request_ids"] == [captured["request_id"]]
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}


def test_reserve_carrier_two_independent_producers_share_one_owner_effect(store, tmp_path, pg_test_scope):
    store, app, ctx, binding, path, request = setup(store, tmp_path, pg_test_scope)
    paths = [path, tmp_path / "second.db"]
    captures = [capture(p, request, binding) for p in paths]
    store.conn.commit()
    barrier = threading.Barrier(2)
    def run(p):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row); assert_disposable_connection(conn)
        try:
            other = WorkApplication.postgres(replace(store, conn=conn))
            call = invoke(other, ctx)
            def overlap(op, args, key):
                if op == intake.OPERATION: barrier.wait(timeout=10)
                return call(op, args, key)
            return sync(p, binding, overlap)
        finally: conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, paths))
    for result, captured in zip(results, captures, strict=True):
        assert result["confirmed_reserve_request_ids"] == [captured["request_id"]]
    assert effects(store) == {"rows": 1, "events": 1, "keys": 1}
