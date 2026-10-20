"""Item-scoped parking facts publish only the cited denial's bounded fields."""
import hashlib
import json
import uuid

import pytest

from sprintctl import pg
from sprintctl.application import ApplicationRejection, WorkApplication
from tests.pg._shared import PG_MARKS
from tests.pg.test_evidence_entry import snapshot
from tests.pg.test_run_evidence import _append_args, _new_run
from tests.pg.test_work_lease import A, B, _claim, _invoke, _items

pytestmark = PG_MARKS
OP = "work.lease.parking-context-v1"


def observation():
    return {"schema": "vuoro-rate-limit-observation/v1", "observed_at": "2026-10-20T00:00:00Z",
            "status": "rejected", "limit_scope": "family", "model_id": "claude-sonnet-5",
            "model_family": "claude-sonnet", "resets_at": "2026-10-20T01:00:00Z",
            "reset_source": "harness-structured", "http_status": 429, "overage": None}


def park(store, *, obs=None, kind="rate_limit_event", ref_run=None, digest=None):
    app = WorkApplication.postgres(store)
    tag = uuid.uuid4().hex
    run = _new_run(app, "context-owner-" + tag, context=A)
    item = _items(store)[0]
    lease = _claim(store, A, item, run, "context-claim-" + tag)["lease"]["lease_id"]
    obs = observation() if obs is None else obs
    args = _append_args(run, "context-evidence-" + tag, item_id="context-entry-" + tag)
    args.update(kind=kind, provenance={"observation": obs, "private": "PRIVATE-CANARY"},
                ref="transcript:PRIVATE-CANARY", digest=digest or "sha256:" + hashlib.sha256(
                    json.dumps(obs, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest())
    _invoke(store, "work.evidence.append-v1", args, A)
    result = _invoke(store, "work.lease.report-outcome-v1", {
        "lease_id": lease, "run_id": run, "outcome": "failed", "disposition": "parked",
        "reason_ref": f"vuoro-evidence:{ref_run or run}/seq/0", "idempotency_key": "context-park-" + tag,
    }, A)
    return item, run, result["report"]["parked"]["event_id"]


def test_other_work_reader_observes_bounded_park_and_reclaim_without_private_chain_access(store):
    item, old_run, event = park(store)
    before = snapshot(store)
    current = _invoke(store, OP, {"item_id": item}, B)
    assert current["current"]["event_id"] == event
    assert current["current"]["rate_limit"]["http_status"] == 429
    assert current["previous"] is None
    assert "PRIVATE-CANARY" not in json.dumps(current)
    with pytest.raises(ApplicationRejection, match="belongs to the caller"):
        _invoke(store, "work.evidence.entry-v1", {"run_id": old_run, "chain_seq": 0}, B)
    assert snapshot(store) == before
    pg.set_work_item_status(store, item, "pending", "coordinator", reason="partial")
    new_run = _new_run(WorkApplication.postgres(store), "context-successor", context=B)
    lease = _claim(store, B, item, new_run, "context-reclaim")["lease"]["lease_id"]
    before = snapshot(store)
    result = _invoke(store, OP, {"item_id": item, "lease_id": lease}, B)
    assert result["current"] is None
    assert result["previous"]["event_id"] == event
    assert result["previous"]["run_id"] == old_run
    assert result["lease_run"] == {"run_id": new_run, "model_id": "claude-sonnet-5"}
    assert result["previous"]["release_event_id"] > event
    assert "PRIVATE-CANARY" not in json.dumps(result)
    assert snapshot(store) == before
    # Later parking cannot rewrite the prior park/release pair for this lease.
    _invoke(store, "work.lease.report-outcome-v1", {
        "lease_id": lease, "run_id": new_run, "outcome": "failed", "disposition": "parked",
        "reason_ref": "other-reason", "idempotency_key": "context-park-again",
    }, B)
    after = _invoke(store, OP, {"item_id": item, "lease_id": lease}, B)
    assert after["previous"] == result["previous"]
    assert after["current"]["reason_ref"] == "other-reason"
    assert after["current"]["rate_limit"] is None


@pytest.mark.parametrize("bad", [
    {"kind": "other"}, {"ref_run": "run_" + "0" * 26}, {"digest": "sha256:" + "0" * 64},
    {"obs": {**observation(), "status": "allowed_warning"}},
    {"obs": {**observation(), "model_id": "another-model"}},
    {"obs": {**observation(), "resets_at": "tomorrow"}},
    {"obs": {**observation(), "extra": "PRIVATE-CANARY"}},
    {"obs": {**observation(), "http_status": True}},
])
def test_unverified_or_non_denial_evidence_is_not_published_as_rate_limit(store, bad):
    item, _, _ = park(store, **bad)
    before = snapshot(store)
    result = _invoke(store, OP, {"item_id": item}, B)
    assert result["current"]["rate_limit"] is None
    assert "PRIVATE-CANARY" not in json.dumps(result)
    assert snapshot(store) == before


def test_ordinary_claim_has_no_previous_park_and_cannot_read_another_items_lease(store):
    first, second = _items(store, 2)
    run = _new_run(WorkApplication.postgres(store), "context-ordinary", context=B)
    lease = _claim(store, B, first, run, "context-ordinary")["lease"]["lease_id"]
    assert _invoke(store, OP, {"item_id": first, "lease_id": lease}, B)["previous"] is None
    with pytest.raises(ApplicationRejection) as caught:
        _invoke(store, OP, {"item_id": second, "lease_id": lease}, B)
    assert caught.value.code == "lease-not-found"


def test_empty_authority_and_unknown_inputs_are_refused(store):
    from tests.pg.test_work_lease import _context
    context = _context()
    context.identity.authorities = frozenset()
    item = _items(store)[0]
    before = snapshot(store)
    with pytest.raises(ApplicationRejection) as caught:
        _invoke(store, OP, {"item_id": item}, context)
    assert caught.value.code == "authority-required"
    for extra in ({"run_id": "private"}, {"lease_id": None}):
        with pytest.raises(ApplicationRejection) as caught:
            _invoke(store, OP, {"item_id": item, **extra}, B)
        assert caught.value.code == "invalid-arguments"
    assert snapshot(store) == before


def test_later_release_from_older_transaction_does_not_rewrite_claim_history(store):
    """PostgreSQL event time is transaction-start time, not commit order."""
    from tests.pg._shared import _PG_URL, assert_disposable_connection, dict_row, psycopg
    item, _, event = park(store)
    pg.set_work_item_status(store, item, "pending", "coordinator", reason="partial")
    run = _new_run(WorkApplication.postgres(store), "context-clock-successor", context=B)
    connection = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    peer = pg.PgStore(connection, store.repo_id)
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT now() AS started")
            started = cur.fetchone()["started"]
        lease = _claim(store, B, item, run, "context-clock-claim")["lease"]["lease_id"]
        # This later release gets the earlier transaction's timestamp.
        pg.set_work_item_status(peer, item, "pending", "coordinator", reason="partial")
        with store.conn.cursor() as cur:
            cur.execute("SELECT id, created_at FROM event WHERE repo_id=%s AND work_item_id=%s "
                        "AND event_type='item-released' ORDER BY id DESC LIMIT 1", (store.repo_id, item))
            later = cur.fetchone()
        store.conn.rollback()
        assert later["created_at"] == started
        result = _invoke(store, OP, {"item_id": item, "lease_id": lease}, B)
        assert result["previous"]["event_id"] == event
        assert result["previous"]["release_event_id"] < later["id"]
    finally:
        connection.close()


@pytest.mark.parametrize("operation", ["work.evidence.entry-v1", OP])
def test_new_reads_preserve_an_existing_callers_transaction(store, operation):
    tag = uuid.uuid4().hex
    item = _items(store)[0]
    run = _new_run(WorkApplication.postgres(store), "context-read-transaction-" + tag, context=B)
    arguments = {"run_id": run, "chain_seq": 0} if operation == "work.evidence.entry-v1" else {"item_id": item}
    try:
        with store.conn.cursor() as cur:
            cur.execute("UPDATE work_item SET title=%s WHERE repo_id=%s AND id=%s", ("caller-pending-" + tag, store.repo_id, item))
        _invoke(store, operation, arguments, B)
        with store.conn.cursor() as cur:
            cur.execute("SELECT title FROM work_item WHERE repo_id=%s AND id=%s", (store.repo_id, item))
            assert cur.fetchone()["title"] == "caller-pending-" + tag
    finally:
        store.conn.rollback()
