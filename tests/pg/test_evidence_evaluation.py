"""Served evaluation through the owner; only disposable PG fixtures."""

from copy import deepcopy
from dataclasses import replace
import uuid
from types import SimpleNamespace
import pytest
import jsonschema
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

from sprintctl import pg, evidence_evaluation as e
from sprintctl.application import ApplicationRejection, WorkApplication
from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    psycopg,
    dict_row,
    assert_disposable_connection,
)
from tests.pg.test_run_evidence import (
    _context as _evidence_context,
    _new_run,
    _validity,
)

pytestmark = PG_MARKS
OP = "work.evidence.evaluate-v1"
AT = "2026-10-09T12:00:00Z"


def _context(**kwargs):
    ctx = _evidence_context(**kwargs)
    ctx.identity.authorities = frozenset({"work:read", "work:evidence"})
    return ctx


@pytest.fixture
def prepared(store, work_item_id):
    store = replace(pg.get_connection(_PG_URL), repo_id=store.repo_id)
    assert_disposable_connection(store.conn)
    app = WorkApplication.postgres(store)
    run = _new_run(app, str(uuid.uuid4()))
    reservation = pg.reserve(
        store, work_item_id, actor="eval-fixture", session_id="eval-fixture"
    )
    release = pg.get_release(store, reservation["release_digest"])
    basis = {
        "item_id": work_item_id,
        "expected_revision": release["item_revision"],
        "release_digest": release["release_digest"],
    }
    args = {
        "run_id": run,
        "subject": "effect",
        "basis": basis,
        "as_of": AT,
        "current_input_digests": {},
        "expected_tail": None,
    }
    try:
        yield app, store, args
    finally:
        store.conn.close()


def append(prepared, **changes):
    app, store, args = prepared
    payload = {
        "run_id": args["run_id"],
        "item_id": "e-" + str(uuid.uuid4()),
        "kind": "test",
        "ref": "local:fixture",
        "digest": "sha256:" + "d" * 64,
        "collector": "fixture",
        "validity": _validity(),
        "claims": [],
        "provenance": {},
        "chain_seq": 0,
        "chain_prev_digest": None,
        "idempotency_key": "eval-" + str(uuid.uuid4()),
    }
    payload.update(changes)
    result = app.invoke("work.evidence.append-v1", payload, _context())["item"]
    args["expected_tail"] = e.chain_tail([result])
    return result


def test_empty_current_basis_is_readonly_and_execution_is_unsupported(prepared):
    app, store, args = prepared
    before = store.conn.execute(
        "SELECT pg_current_xact_id_if_assigned() AS id"
    ).fetchone()["id"]
    first = app.invoke(OP, args, _context())
    assert (
        first["basis_status"] == "current"
        and first["source_watermark"]["evidence_chain"]["item_count"] == 0
    )
    assert (
        first["authority_coverage"] == "unsupported"
        and first["authenticated_execution_facts"] == []
    )
    assert first["recommendation"] == "reconcile" and not first["authorizes_execution"]
    assert (
        store.conn.execute("SELECT pg_current_xact_id_if_assigned() AS id").fetchone()[
            "id"
        ]
        == before
    )
    assert (
        store.conn.execute(
            "SELECT count(*) AS n FROM work_decision WHERE repo_id=%s", (store.repo_id,)
        ).fetchone()["n"]
        == 0
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_id", "github:2:0"),
        ("workspace_id", "other"),
        ("client_id", "other"),
        ("grant_id", "other"),
    ],
)
def test_full_binding_refusal(prepared, field, value):
    app, store, args = prepared
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context(**{field: value}))
    assert err.value.code == "run-not-found" and err.value.http_status == 404


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(trusted=True),
        lambda x: x["basis"].update(extra=True),
        lambda x: x.update(as_of="2026-10-09T12:00:00"),
        lambda x: x.update(current_input_digests={"tree": None}),
    ],
)
def test_closed_request_and_clock_refusals(prepared, mutation):
    app, store, args = prepared
    mutation(args)
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "invalid-arguments"


@pytest.mark.parametrize("authorities", [{"work:evidence"}, {"work:read"}, set()])
def test_missing_authority_and_idempotency_are_refused(prepared, authorities):
    app, store, args = prepared
    ctx = _context()
    ctx.identity.authorities = frozenset(authorities)
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, ctx)
    assert err.value.code == "authority-required"
    ctx = _context()
    ctx.idempotency_key = "read-key"
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, ctx)
    assert err.value.code == "invalid-arguments"


def test_authored_trust_remains_assertion_and_repeat_snapshot_is_exact(prepared):
    app, store, args = prepared
    append(
        prepared,
        claims=[
            {
                "claim_type": "effect_completed",
                "subject": "effect",
                "detail": {"trusted": True},
            }
        ],
        provenance={"authority": "owner"},
    )
    a = app.invoke(OP, args, _context())
    b = app.invoke(OP, args, _context())
    a.pop("observed_at")
    b.pop("observed_at")
    assert (
        a == b
        and a["effect_state"] == "unknown"
        and a["authored_assertions"][0]["authority"] == "authored-assertion"
    )


def test_changed_revision_and_stale_recorded_release_are_reported(prepared):
    app, store, args = prepared
    app.invoke(
        "work.item.edit",
        {
            "item_id": args["basis"]["item_id"],
            "description": "Changed independently",
            "expected_revision": args["basis"]["expected_revision"].rsplit(
                "@revise:", 1
            )[0],
        },
        _context(),
    )
    result = app.invoke(OP, args, _context())
    assert result["basis_status"] == "stale"
    # Even a caller changing its expected revision cannot make an old frozen Release current.
    args["basis"]["expected_revision"] = result["current_basis"]["expected_revision"]
    assert app.invoke(OP, args, _context())["basis_status"] == "stale"


def test_mismatched_tail_and_corrupt_middle_refuse(prepared):
    app, store, args = prepared
    append(prepared)
    args["expected_tail"] = None
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "evidence-tail-mismatch"
    store.conn.execute(
        "UPDATE evidence_item SET chain_prev_digest=%s WHERE repo_id=%s AND run_id=%s",
        ("corrupt", store.repo_id, args["run_id"]),
    )
    store.conn.commit()
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "evidence-chain-invalid"


def test_concurrent_append_and_edit_are_one_repeatable_snapshot(prepared, monkeypatch):
    import threading

    app, store, args = prepared
    original = append(prepared)
    entered, proceed = threading.Event(), threading.Event()
    resolve = pg.resolve_run
    snapshots = []

    def paused(snapshot, *pos, **kw):
        result = resolve(snapshot, *pos, **kw)
        if (
            snapshot is not store
            and snapshot.connection_factory is store.connection_factory
        ):
            snapshots.append(snapshot.conn)
            entered.set()
            assert proceed.wait(10)
        return result

    monkeypatch.setattr(pg, "resolve_run", paused)
    results, errors = [], []

    def read():
        try:
            results.append(app.invoke(OP, deepcopy(args), _context()))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=read)
    thread.start()
    assert entered.wait(5)
    other = replace(pg.get_connection(_PG_URL), repo_id=store.repo_id)
    assert_disposable_connection(other.conn)
    try:
        mutation_app = WorkApplication.postgres(other)
        payload = {
            "run_id": args["run_id"],
            "item_id": "second-" + str(uuid.uuid4()),
            "kind": "test",
            "ref": "local:second",
            "digest": "sha256:" + "e" * 64,
            "collector": "fixture",
            "validity": _validity(),
            "claims": [],
            "provenance": {},
            "chain_seq": 1,
            "chain_prev_digest": pg.evidence_entry_digest(original),
            "idempotency_key": "second-" + str(uuid.uuid4()),
        }
        appended = mutation_app.invoke("work.evidence.append-v1", payload, _context())[
            "item"
        ]
        mutation_app.invoke(
            "work.item.edit",
            {
                "item_id": args["basis"]["item_id"],
                "description": "Concurrent change",
                "expected_revision": args["basis"]["expected_revision"].rsplit(
                    "@revise:", 1
                )[0],
            },
            _context(),
        )
    finally:
        other.conn.close()
        proceed.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert (
        results[0]["basis_status"] == "current"
        and results[0]["source_watermark"]["evidence_chain"]["item_count"] == 1
    )
    assert (
        results[0]["source_watermark"]["evidence_chain"]["tail"]
        == args["expected_tail"]
    )
    assert snapshots[0].closed
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "evidence-tail-mismatch" and snapshots[-1].closed
    args["expected_tail"] = {
        "item_id": appended["item_id"],
        "chain_seq": 1,
        "entry_digest": pg.evidence_entry_digest(appended),
    }
    fresh = app.invoke(OP, args, _context())
    assert (
        fresh["basis_status"] == "stale"
        and fresh["source_watermark"]["evidence_chain"]["item_count"] == 2
    )
    assert snapshots[-1].closed


def test_source_bounds_refuse_before_fetch_and_close_snapshot(prepared, monkeypatch):
    app, store, args = prepared
    append(prepared, provenance={"payload": "x" * 100})
    factory = store.connection_factory
    connections = []

    def tracked():
        conn = factory()
        connections.append(conn)
        return conn

    store.connection_factory = tracked
    monkeypatch.setattr(e, "MAX_SNAPSHOT_BYTES", 1)
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "evidence-snapshot-too-large" and connections[-1].closed


@pytest.mark.parametrize(
    "bad",
    [
        {"subject": "x" * 513},
        {"current_input_digests": {"x" * 257: "a"}},
        {"current_input_digests": {str(i): "a" for i in range(257)}},
    ],
)
def test_bounded_request(prepared, bad):
    app, store, args = prepared
    args.update(bad)
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "invalid-arguments"


def test_plain_edit_revision_is_not_a_full_evaluation_basis(prepared):
    app, store, args = prepared
    args["basis"]["expected_revision"] = args["basis"]["expected_revision"].rsplit(
        "@revise:", 1
    )[0]
    with pytest.raises(ApplicationRejection) as err:
        app.invoke(OP, args, _context())
    assert err.value.code == "invalid-arguments"


@pytest.mark.parametrize("source", ["empty", "current", "stale", "invalid-validity"])
def test_actual_served_result_matches_published_schema(prepared, source):
    app, store, args = prepared
    if source != "empty":
        append(prepared)
    if source == "stale":
        app.invoke(
            "work.item.edit",
            {
                "item_id": args["basis"]["item_id"],
                "description": "Independent schema-fixture revision",
                "expected_revision": args["basis"]["expected_revision"].rsplit(
                    "@revise:", 1
                )[0],
            },
            _context(),
        )
    elif source == "invalid-validity":
        # Legacy malformed source remains reportable, without blessing its validity.
        store.conn.execute(
            "UPDATE evidence_item SET validity=%s::jsonb WHERE repo_id=%s AND run_id=%s",
            ('{"basis":"unsupported-legacy"}', store.repo_id, args["run_id"]),
        )
        store.conn.commit()
    result = app.invoke(OP, args, _context())
    contract = next(c for c in WORK_OPERATION_CONTRACTS if c.name == OP)
    jsonschema.validate(result, contract.result_schema)
    assert result["basis_status"] == ("stale" if source == "stale" else "current")
    if source == "invalid-validity":
        assert result["evidence_validity"][0]["status"] == "invalid"
    elif source == "current":
        assert result["evidence_validity"][0]["status"] == "valid"
