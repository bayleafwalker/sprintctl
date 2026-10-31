"""Exact basis settlement uses real PostgreSQL and public owner contracts."""
from __future__ import annotations

import threading
import uuid

import jsonschema
import pytest

from sprintctl import continuation_basis, pg
from sprintctl.application import ApplicationRejection
from tests.pg._shared import PG_MARKS, _PG_URL, assert_disposable_connection, dict_row, psycopg
from tests.pg.test_work_lease import A, B, _claim, _items, _invoke, _read, _run, _CONTRACTS

pytestmark = PG_MARKS
OP = "work.lease.report-outcome-v2"


@pytest.fixture(scope='module')
def store(pg_test_scope):
    def connect():
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        conn.rollback()  # snapshot factory must return an idle connection
        return conn
    conn = connect()
    owner = pg.PgStore(conn=conn, repo_id=pg_test_scope('outcome-basis'),
                       authority_repo_uuid=str(uuid.uuid4()), connection_factory=connect)
    pg.init_db(owner)
    try:
        yield owner
    finally:
        conn.close()


def setup(store):
    item = _items(store)[0]
    reservation = pg.reserve(store, item, actor="fixture", session_id=uuid.uuid4().hex,
                             role="execution", acceptance_contract={"evidence_obligations": ["tests"]})
    pg.release_reservation(store, reservation["id"])
    run = _run(store, A)
    lease = _claim(store, A, item, run, uuid.uuid4().hex)["lease"]
    checkpoint = _invoke(store, "work.run.continuation-delta-v1", {"run_id": run, "item_id": item}, A)["delta"]["current"]
    body = {"lease_id": lease["lease_id"], "run_id": run, "outcome": "succeeded",
            "summary": "exact outcome", "payload": {"candidate": "fixture"},
            "checks": [{"name": "tests", "status": "passed"}],
            "idempotency_key": uuid.uuid4().hex, "checkpoint": checkpoint}
    return item, body


def invoke(store, body, context=A):
    jsonschema.validate(body, _CONTRACTS[OP].input_schema)
    return _invoke(store, OP, body, context)


def test_exact_replay_after_own_decision_and_current_principal(store):
    item, body = setup(store)
    answer = invoke(store, body)  # discard this response to model lost acknowledgement
    assert answer["settled"] is True
    current = _invoke(store, "work.run.continuation-delta-v1", {"run_id": body["run_id"], "item_id": item}, A)["delta"]["current"]
    assert current != body["checkpoint"]
    assert invoke(store, body) == answer
    observed = _read(store, item)
    assert len(observed["outcome_reports"]) == 1
    assert len(pg.list_decisions(store, item)) == 1
    report = answer["report"]
    assert all(report[k] == body[k] for k in ("outcome", "summary", "payload", "checks"))
    assert pg.list_decisions(store, item)[0]["evidence_digests"] == [report["payload_digest"]]
    with pytest.raises(ApplicationRejection):
        invoke(store, body, B)
    changed = {**body, "checkpoint": current}
    with pytest.raises(ApplicationRejection) as error:
        invoke(store, changed)
    assert error.value.code == "idempotency-conflict"


@pytest.mark.parametrize("mutation", ["insert", "delete", "update", "decision", "goal", "release"])
def test_changed_basis_never_settles_and_keeps_report(store, mutation):
    item, body = setup(store)
    if mutation in ("delete", "update"):
        ref = pg.add_ref(store, item, "doc", "https://example.invalid/retained", "before")
        body["checkpoint"] = _invoke(store, "work.run.continuation-delta-v1", {"run_id": body["run_id"], "item_id": item}, A)["delta"]["current"]
    if mutation == "insert":
        pg.add_ref(store, item, "doc", "https://example.invalid/new", "after")
    elif mutation == "delete":
        pg.remove_ref(store, ref, item)
    elif mutation == "update":
        with store.conn.cursor() as cur:
            cur.execute("UPDATE ref SET label='after' WHERE repo_id=%s AND id=%s", (store.repo_id, ref))
        store.conn.commit()
    elif mutation == "decision":
        pg.record_decision(store, item, "revise", actor="fixture", rationale="changed basis")
    elif mutation == "goal":
        pg.update_work_item_description(store, item, "changed exact goal", actor="fixture")
    else:
        pg.reserve(store, item, actor="fixture", session_id=uuid.uuid4().hex,
                   role="execution", acceptance_contract={"evidence_obligations": ["tests", "review"]})
    with pytest.raises(ApplicationRejection) as error:
        invoke(store, body)
    assert error.value.code in {"work-basis-stale", "lease-basis-stale"}
    reports = _read(store, item)["outcome_reports"]
    assert len(reports) == 1 and reports[0]["disposition"] == "rejected"
    assert reports[0]["decision_id"] is None
    assert pg.get_work_item(store, item)["status"] != "done"


@pytest.mark.parametrize("unknown", ["checkpoint", "population"])
def test_unknown_population_is_not_exact_equality(store, unknown):
    item, body = setup(store)
    if unknown == "checkpoint":
        body["checkpoint"]["current_reference_set_digest"] = None
    else:
        for i in range(65):
            pg.add_ref(store, item, "other", f"https://example.invalid/{i}")
        body["checkpoint"] = _invoke(store, "work.run.continuation-delta-v1", {"run_id": body["run_id"], "item_id": item}, A)["delta"]["current"]
        assert not continuation_basis.complete(body["checkpoint"])
    with pytest.raises(ApplicationRejection) as error:
        invoke(store, body)
    assert error.value.code == "work-basis-unavailable"
    assert _read(store, item)["outcome_reports"][0]["decision_id"] is None


@pytest.mark.parametrize("mutation", ["insert", "delete", "update", "goal", "decision", "release", "import-decision", "drop-fk", "disable-trigger"])
def test_basis_writers_cannot_cross_locked_compare(store, monkeypatch, mutation):
    item, body = setup(store)
    ref = pg.add_ref(store, item, "doc", "https://example.invalid/current", "before")
    body["checkpoint"] = _invoke(store, "work.run.continuation-delta-v1", {"run_id": body["run_id"], "item_id": item}, A)["delta"]["current"]
    store.conn.commit()
    original = pg._outcome_current_basis_reason
    trace = []
    def guarded(cur, owner, current_item, checkpoint):
        value = original(cur, owner, current_item, checkpoint)
        assert value is None
        def writer():
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            try:
                assert_disposable_connection(conn)
                other = pg.PgStore(conn=conn, repo_id=store.repo_id, authority_repo_uuid=store.authority_repo_uuid)
                with conn.cursor() as cursor:
                    cursor.execute("SET lock_timeout='200ms'")
                if mutation == "insert":
                    pg.add_ref(other, item, "doc", "https://example.invalid/racing")
                elif mutation == "delete":
                    pg.remove_ref(other, ref, item)
                elif mutation == "update":
                    with conn.cursor() as cursor:
                        cursor.execute("UPDATE ref SET label='racing' WHERE repo_id=%s AND id=%s", (store.repo_id, ref))
                    conn.commit()
                elif mutation == "goal":
                    pg.update_work_item_description(other, item, "racing goal", actor="racer")
                elif mutation == "decision":
                    pg.record_decision(other, item, "revise", actor="racer", rationale="race")
                elif mutation == "release":
                    pg.reserve(other, item, actor="racer", session_id=uuid.uuid4().hex, role="execution")
                elif mutation == "import-decision":
                    pg.import_ndjson(other, [{"table": "work_decision", "data": {
                        "work_item_id": item, "kind": "revise", "actor": "racer",
                        "rationale": "imported revision", "evidence_digests": [],
                        "release_digest": body["checkpoint"]["current_release_digest"],
                    }}])
                else:
                    with conn.cursor() as cursor:
                        if mutation == "drop-fk":
                            cursor.execute("SELECT conname FROM pg_constraint WHERE conrelid='ref'::regclass AND contype='f'")
                            name = cursor.fetchone()["conname"]
                            cursor.execute(psycopg.sql.SQL("ALTER TABLE ref DROP CONSTRAINT {}").format(psycopg.sql.Identifier(name)))
                        else:
                            cursor.execute("ALTER TABLE ref DISABLE TRIGGER USER")
                    conn.commit()
                trace.append("committed")
            except psycopg.errors.LockNotAvailable:
                trace.append("blocked")
            finally:
                conn.rollback(); conn.close()
        thread = threading.Thread(target=writer)
        thread.start(); thread.join(timeout=5)
        assert not thread.is_alive()
        assert trace == ["blocked"], trace
        return value
    monkeypatch.setattr(pg, "_outcome_current_basis_reason", guarded)
    assert invoke(store, body)["settled"] is True


def test_v1_preserves_reference_change_behavior(store):
    item, body = setup(store)
    pg.add_ref(store, item, "doc", "https://example.invalid/new")
    del body["checkpoint"]
    assert _invoke(store, "work.lease.report-outcome-v1", body, A)["settled"] is True


@pytest.mark.parametrize("fault", ["missing", "deferred", "not-validated"])
def test_actual_foreign_key_protection_is_required(store, fault):
    item, body = setup(store)
    store.conn.commit()
    # Schema fault exists only in a rollback-only transaction in the guarded
    # disposable database. The original validated owner FK is always restored.
    with store.conn.transaction(force_rollback=True):
        with store.conn.cursor() as cur:
            cur.execute("SELECT conname FROM pg_constraint WHERE conrelid='ref'::regclass AND contype='f'")
            name = cur.fetchone()["conname"]
            cur.execute(psycopg.sql.SQL("ALTER TABLE ref DROP CONSTRAINT {}").format(psycopg.sql.Identifier(name)))
            if fault != "missing":
                suffix = " DEFERRABLE INITIALLY DEFERRED" if fault == "deferred" else " NOT VALID"
                cur.execute("ALTER TABLE ref ADD CONSTRAINT fixture_reference_fk FOREIGN KEY(repo_id,work_item_id) REFERENCES work_item(repo_id,id)" + suffix)
            result = pg.complete_lease(
                store, lease_id=body["lease_id"], run_id=body["run_id"],
                principal_id=A.identity.principal_id, workspace_id=A.identity.workspace_id,
                client_id=None, grant_id=None, idempotency_key=body["idempotency_key"],
                request_digest="a" * 64, outcome=body["outcome"],
                summary=body["summary"], payload=body["payload"], checks=body["checks"],
                actor=A.identity.actor, checkpoint=body["checkpoint"], cur=cur,
            )
            assert result["disposition"] == "rejected"
            assert result["reason_code"] == "work-basis-unavailable"
            assert result["decision_id"] is None
    assert not pg.list_decisions(store, item)


def test_blocked_table_admission_times_out_without_report_or_decision(store):
    item, body = setup(store)
    store.conn.commit()
    blocker = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(blocker)
    try:
        with blocker.cursor() as cur:
            cur.execute("LOCK TABLE ref IN SHARE UPDATE EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            invoke(store, body)
        store.conn.rollback()
        assert _read(store, item)["outcome_reports"] == []
        assert pg.list_decisions(store, item) == []
    finally:
        blocker.rollback(); blocker.close()
    assert invoke(store, body)["settled"] is True


@pytest.mark.parametrize('function', ['RI_FKey_check_ins', 'RI_FKey_check_upd'])
def test_incomplete_internal_trigger_metadata_is_not_effective_fk(store, monkeypatch, function):
    item, body = setup(store)
    with store.conn.cursor() as cur:
        cur.execute("SELECT t.oid FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
                    "WHERE t.tgrelid='ref'::regclass AND t.tgisinternal AND p.proname=%s", (function,))
        removed = cur.fetchone()['oid']
    original = pg._lock_outcome_references
    class MetadataFault:
        def __init__(self, cursor): self.cursor = cursor
        def execute(self, query, params=None):
            if 'SELECT c.oid FROM pg_constraint c' in query and params[0] == 'ref':
                # Read-only metadata counterexample: hide exactly one actual
                # internal source-check row. No catalog mutation or fake FK.
                query = 'WITH pg_trigger AS (SELECT * FROM pg_catalog.pg_trigger WHERE oid <> %s) ' + query
                params = (removed, *params)
            return self.cursor.execute(query, params)
        def __getattr__(self, name): return getattr(self.cursor, name)
    monkeypatch.setattr(pg, '_lock_outcome_references',
                        lambda cur, owner, ident: original(MetadataFault(cur), owner, ident))
    with pytest.raises(ApplicationRejection) as error:
        invoke(store, body)
    assert error.value.code == 'work-basis-unavailable'
    assert not pg.list_decisions(store, item)
