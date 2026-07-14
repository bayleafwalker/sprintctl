"""
PostgreSQL integration tests for sprintctl.pg.

Requires a real PostgreSQL instance. Set SPRINTCTL_TEST_PG_URL to run:

    SPRINTCTL_TEST_PG_URL=postgresql://localhost/testdb pytest tests/test_pg_integration.py -v

All tests are automatically skipped when the variable is unset or psycopg is unavailable.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
import threading
import uuid

import pytest

_PG_URL: str | None = os.environ.get("SPRINTCTL_TEST_PG_URL")

try:
    import psycopg
    from psycopg.rows import dict_row
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False

_SKIP = not _PG_URL or not _PSYCOPG_AVAILABLE
_SKIP_REASON = (
    "SPRINTCTL_TEST_PG_URL not set"
    if not _PG_URL
    else "psycopg not installed — run: pip install 'sprintctl[remote]'"
)

pytestmark = pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)

# Safe unconditional imports: pg.py handles missing psycopg gracefully.
from sprintctl import contracts, pg
from sprintctl import outbox
from sprintctl.db import ClaimConflict, InvalidTransition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def store():
    """Create a PgStore with a unique repo_id; clean up all rows at module teardown."""
    if _SKIP:
        pytest.skip(_SKIP_REASON)
    repo_id = f"itest-{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    s = pg.PgStore(conn=conn, repo_id=repo_id)
    pg.init_db(s)
    yield s
    with conn.cursor() as cur:
        for table in ("ingest_record", "ingest_stream", "dep", "ref", "claim", "event", "work_item", "track", "sprint"):
            cur.execute(f"DELETE FROM {table} WHERE repo_id = %s", (repo_id,))
    conn.commit()
    conn.close()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _receipt_bytes(store, sprint_id, boundary_event_id, **overrides):
    receipt_id = f"{store.repo_id}.2026-07-13.boundary"
    receipt = {
        "schema_version": "capability-receipt/v1",
        "id": receipt_id,
        "project": store.repo_id,
        "status": "draft",
        "publication": "private",
        "boundary": {
            "kind": "sprint-close",
            "ref": {
                "kind": "sprint-event",
                "source": f"sprintctl:{store.repo_id}:sprint:{sprint_id}",
                "revision": f"event:{boundary_event_id}",
            },
        },
    }
    receipt.update(overrides)
    return json.dumps(receipt, sort_keys=True).encode()


def _receipt_payload(store, receipt_bytes):
    receipt_id = f"{store.repo_id}.2026-07-13.boundary"
    return {
        "project": store.repo_id,
        "receipt_id": receipt_id,
        "receipt_path": (
            f"/projects/dev/_artifacts/{store.repo_id}/capability/receipts/"
            f"{receipt_id}.json"
        ),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }


@pytest.fixture
def sprint_id(store):
    return pg.create_sprint(store, f"S-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")


@pytest.fixture
def track_id(store, sprint_id):
    return pg.get_or_create_track(store, sprint_id, "eng")


@pytest.fixture
def work_item_id(store, sprint_id, track_id):
    return pg.create_work_item(store, sprint_id, track_id, f"Item-{_uid()}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_idempotent(self, store):
        pg.init_db(store)
        pg.init_db(store)
        sprint_id = pg.create_sprint(store, f"Init-{_uid()}", "G", status="active")
        assert pg.get_sprint(store, sprint_id) is not None


# ---------------------------------------------------------------------------
# Producer outbox ingestion
# ---------------------------------------------------------------------------

class TestProducerOutboxIngestion:
    def _records(self, tmp_path, count=2):
        conn = outbox.open_outbox(tmp_path / "producer-outbox.db")
        try:
            return [
                outbox.append_observation(
                    conn,
                    event_type="work.completed",
                    actor="producer-a",
                    payload={"index": index},
                    event_id=f"ingest-{uuid.uuid4().hex}-{index}",
                    occurred_at="2026-07-14T12:00:00Z",
                )
                for index in range(count)
            ]
        finally:
            conn.close()

    def test_retry_returns_original_offset_and_cursor_orders_new_records(self, store, tmp_path):
        first, second = self._records(tmp_path)

        admitted = pg.ingest_records(store, [first, second])
        retried = pg.ingest_records(store, [first, second])
        ordered = pg.list_ingested_records(store)

        assert [result.duplicate for result in admitted] == [False, False]
        assert [result.duplicate for result in retried] == [True, True]
        assert [result.ingest_offset for result in retried] == [result.ingest_offset for result in admitted]
        assert [result.ingest_offset for result in ordered] == [result.ingest_offset for result in admitted]
        assert [result.record.created_at for result in ordered] == [first.created_at, second.created_at]
        assert ordered[1].ingest_offset > ordered[0].ingest_offset

    def test_changed_producer_timestamp_conflicts_with_existing_origin_tuple(self, store, tmp_path):
        first = self._records(tmp_path, count=1)[0]
        pg.ingest_records(store, [first])

        with pytest.raises(pg.IngestConflictError, match="different record"):
            pg.ingest_records(
                store,
                [replace(first, created_at="2026-07-14T12:00:01Z")],
            )

    def test_gap_rejects_whole_batch_without_advancing_stream(self, store, tmp_path):
        first, second = self._records(tmp_path)
        before_offset = max(
            (result.ingest_offset for result in pg.list_ingested_records(store)), default=0
        )

        with pytest.raises(pg.IngestGapError, match="expected sequence 1, received 2"):
            pg.ingest_records(store, [second])
        assert pg.list_ingested_records(store, after_offset=before_offset) == []

        admitted = pg.ingest_records(store, [first, second])
        assert [result.record.origin_seq for result in admitted] == [1, 2]

    def test_concurrent_same_stream_retry_admits_once(self, store, tmp_path):
        record = self._records(tmp_path, count=1)[0]
        before_offset = max(
            (result.ingest_offset for result in pg.list_ingested_records(store)), default=0
        )
        barrier = threading.Barrier(3)
        outcomes = []
        failures = []

        def worker():
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                barrier.wait()
                outcomes.append(pg.ingest_records(independent_store, [record])[0])
            except Exception as exc:  # pragma: no cover - asserted through failures
                failures.append(exc)
            finally:
                conn.close()

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        barrier.wait()
        for worker_thread in workers:
            worker_thread.join()

        assert failures == []
        assert len(outcomes) == 2
        assert {outcome.ingest_offset for outcome in outcomes} == {outcomes[0].ingest_offset}
        assert sorted(outcome.duplicate for outcome in outcomes) == [False, True]
        assert len(pg.list_ingested_records(store, after_offset=before_offset)) == 1


# ---------------------------------------------------------------------------
# Sprint
# ---------------------------------------------------------------------------

class TestSprint:
    def test_create_and_get(self, store):
        sid = pg.create_sprint(store, "SprintA", "G", "2026-01-01", "2026-12-31", "active")
        row = pg.get_sprint(store, sid)
        assert row is not None
        assert row["name"] == "SprintA"
        assert row["repo_id"] == store.repo_id

    def test_get_missing_returns_none(self, store):
        assert pg.get_sprint(store, 9_999_999) is None

    def test_get_active_sprint(self, store):
        pg.create_sprint(store, f"Active-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        result = pg.get_active_sprint(store)
        assert result is not None

    def test_list_active_sprints(self, store):
        pg.create_sprint(store, f"La-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        rows = pg.list_active_sprints(store)
        assert isinstance(rows, list)
        assert all(r["repo_id"] == store.repo_id for r in rows)

    def test_list_sprints(self, store):
        rows = pg.list_sprints(store)
        assert isinstance(rows, list)

    def test_set_sprint_status_active_to_closed(self, store):
        sid = pg.create_sprint(store, f"St-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        pg.set_sprint_status(store, sid, "closed")
        assert pg.get_sprint(store, sid)["status"] == "closed"

    def test_explicit_close_appends_boundary_event_atomically(self, store):
        sid = pg.create_sprint(
            store,
            f"Cb-{_uid()}",
            "G",
            "2026-01-01",
            "2026-12-31",
            "active",
        )

        event_id = pg.close_sprint_with_boundary_event(store, sid, "operator")

        assert pg.get_sprint(store, sid)["status"] == "closed"
        event = next(
            event for event in pg.list_events(store, sid) if event["id"] == event_id
        )
        assert event["event_type"] == "sprint-close-boundary"
        assert event["actor"] == "operator"
        assert json.loads(event["payload"]) == {
            "previous_status": "active",
            "status": "closed",
        }

    def test_explicit_close_rolls_back_when_boundary_insert_fails(self, store, monkeypatch):
        sid = pg.create_sprint(
            store,
            f"Cr-{_uid()}",
            "G",
            "2026-01-01",
            "2026-12-31",
            "active",
        )

        def fail_insert(*args, **kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(pg, "_insert_event", fail_insert)
        with pytest.raises(RuntimeError, match="injected event failure"):
            pg.close_sprint_with_boundary_event(store, sid, "operator")

        assert pg.get_sprint(store, sid)["status"] == "active"
        assert pg.list_events(store, sid) == []

    def test_set_sprint_kind(self, store):
        sid = pg.create_sprint(store, f"Sk-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        pg.set_sprint_kind(store, sid, "backlog")
        assert pg.get_sprint(store, sid)["kind"] == "backlog"

    def test_timestamps_are_iso_strings(self, store):
        sid = pg.create_sprint(store, f"Ts-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        row = pg.get_sprint(store, sid)
        assert isinstance(row["created_at"], str)
        assert "T" in row["created_at"]


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------

class TestTrack:
    def test_get_or_create_idempotent(self, store, sprint_id):
        tid = pg.get_or_create_track(store, sprint_id, "eng")
        assert pg.get_or_create_track(store, sprint_id, "eng") == tid

    def test_list_tracks(self, store, sprint_id):
        pg.get_or_create_track(store, sprint_id, f"trk-{_uid()}")
        tracks = pg.list_tracks(store, sprint_id)
        assert len(tracks) >= 1

    def test_get_track(self, store, sprint_id):
        tid = pg.get_or_create_track(store, sprint_id, f"gt-{_uid()}")
        track = pg.get_track(store, tid)
        assert track is not None
        assert track["sprint_id"] == sprint_id


# ---------------------------------------------------------------------------
# WorkItem
# ---------------------------------------------------------------------------

class TestWorkItem:
    def test_create_and_get(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "WI title", "desc")
        item = pg.get_work_item(store, iid)
        assert item is not None
        assert item["title"] == "WI title"
        assert item["description"] == "desc"
        assert item["status"] == "pending"

    def test_update_description(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "Editable", "Old scope")

        pg.update_work_item_description(store, iid, "New shaped scope")

        assert pg.get_work_item(store, iid)["description"] == "New shaped scope"

    def test_update_description_validation_and_missing_item(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, "Validated", "Keep scope")

        with pytest.raises(ValueError, match="non-whitespace"):
            pg.update_work_item_description(store, iid, "  \n  ")
        with pytest.raises(ValueError, match="NUL"):
            pg.update_work_item_description(store, iid, "invalid\x00scope")
        with pytest.raises(ValueError, match="Item #999999999 not found"):
            pg.update_work_item_description(store, 999_999_999, "Valid scope")
        assert pg.get_work_item(store, iid)["description"] == "Keep scope"

    def test_get_missing_returns_none(self, store):
        assert pg.get_work_item(store, 9_999_999) is None

    def test_list_by_sprint(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Li-{_uid()}")
        items = pg.list_work_items(store, sprint_id=sprint_id)
        assert any(i["id"] == iid for i in items)

    def test_list_by_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ls-{_uid()}")
        pending = pg.list_work_items(store, sprint_id=sprint_id, status="pending")
        assert any(i["id"] == iid for i in pending)

    def test_set_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ss-{_uid()}")
        pg.set_work_item_status(store, iid, "active")
        assert pg.get_work_item(store, iid)["status"] == "active"

    def test_set_status_invalid_transition_raises(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Inv-{_uid()}")
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, iid, "done")  # pending → done not allowed

    def test_claimed_item_requires_proof_for_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cp-{_uid()}")
        claim_id = pg.create_claim(store, iid, "ag", ttl_seconds=300)
        claim = pg.get_claim(store, claim_id, include_secret=True)
        token = claim["claim_token"]
        pg.set_work_item_status(store, iid, "active", claim_id=claim_id, claim_token=token)
        with pytest.raises(ClaimConflict):
            pg.set_work_item_status(store, iid, "done")


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class TestEvent:
    def test_generic_api_cannot_forge_close_boundary(self, store, sprint_id):
        with pytest.raises(ValueError, match="reserved; use the atomic sprint close"):
            pg.create_event(
                store,
                sprint_id,
                "forger",
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                payload={"previous_status": "active", "status": "closed"},
            )

    def test_capability_pointer_requires_closed_sprint_and_local_boundary(
        self,
        store,
        sprint_id,
    ):
        receipt_bytes = _receipt_bytes(store, sprint_id, 1)
        with pytest.raises(ValueError, match="requires a closed sprint"):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=_receipt_payload(store, receipt_bytes),
            )

    def test_create_and_list(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "hi"})
        events = pg.list_events(store, sprint_id)
        assert any(e["event_type"] == "note" for e in events)

    def test_payload_is_string(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "str-check"})
        events = pg.list_events(store, sprint_id)
        note = next(e for e in reversed(events) if e["event_type"] == "note")
        assert isinstance(note["payload"], str)
        assert json.loads(note["payload"])["summary"] is not None

    def test_capability_receipt_pointer_matches_sqlite_contract(
        self,
        store,
        sprint_id,
        monkeypatch,
    ):
        boundary_event_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_event_id)
        payload = _receipt_payload(store, receipt_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: receipt_bytes,
        )

        event_id = pg.create_event(
            store,
            sprint_id,
            "drafting-agent",
            "capability-receipt-drafted",
            payload=payload,
        )

        event = next(
            event
            for event in pg.list_events(store, sprint_id)
            if event["id"] == event_id
        )
        assert json.loads(event["payload"]) == payload

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda payload: payload.update(receipt_id="other.receipt"), "must start"),
            (lambda payload: payload.update(receipt_path="/tmp/receipt.json"), "must be exactly"),
            (lambda payload: payload.update(receipt_sha256="A" * 64), "lowercase hexadecimal"),
            (lambda payload: payload.update(receipt_body={"private": True}), "unknown fields"),
            (
                lambda payload: payload.update(
                    project="other",
                    receipt_id="other.receipt",
                    receipt_path=(
                        "/projects/dev/_artifacts/other/capability/receipts/"
                        "other.receipt.json"
                    ),
                ),
                "owning repository",
            ),
        ],
    )
    def test_malformed_capability_pointer_matches_sqlite_rejection(
        self,
        store,
        sprint_id,
        mutate,
        message,
    ):
        receipt_bytes = _receipt_bytes(store, sprint_id, 1)
        payload = _receipt_payload(store, receipt_bytes)
        mutate(payload)

        with pytest.raises(ValueError, match=message):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )

    def test_capability_pointer_file_and_boundary_are_verified_before_insert(
        self,
        store,
        sprint_id,
        monkeypatch,
    ):
        boundary_event_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        wrong_revision_bytes = _receipt_bytes(
            store,
            sprint_id,
            boundary_event_id,
            boundary={
                "kind": "sprint-close",
                "ref": {
                    "kind": "sprint-event",
                    "source": f"sprintctl:{store.repo_id}:sprint:{sprint_id}",
                    "revision": f"event:{boundary_event_id + 1}",
                },
            },
        )
        payload = _receipt_payload(store, wrong_revision_bytes)
        monkeypatch.setattr(
            contracts,
            "_read_capability_receipt_bytes",
            lambda receipt_path: wrong_revision_bytes,
        )

        with pytest.raises(ValueError, match="boundary.ref.revision"):
            pg.create_event(
                store,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=payload,
            )
        assert not any(
            event["event_type"] == contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
            for event in pg.list_events(store, sprint_id)
        )

    def test_list_events_limited(self, store, sprint_id):
        for _ in range(4):
            pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                            payload={"summary": "x"})
        limited = pg.list_events_limited(store, sprint_id, limit=2)
        assert len(limited) == 2

    def test_list_knowledge_candidates(self, store, sprint_id, work_item_id):
        pg.create_event(store, sprint_id, "ag", "decision", source_type="actor",
                        work_item_id=work_item_id, payload={"summary": "chose X"})
        candidates = pg.list_knowledge_candidates(store, sprint_id)
        assert any(c["event_type"] == "decision" for c in candidates)
        assert all(isinstance(c["payload"], dict) for c in candidates)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

class TestClaim:
    def test_create_and_get(self, store, work_item_id):
        cid = pg.create_claim(store, work_item_id, "ag-A", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        assert claim is not None
        assert claim["agent"] == "ag-A"
        assert claim["claim_token"] is not None

    def test_get_missing_returns_none(self, store):
        assert pg.get_claim(store, 9_999_999) is None

    def test_token_not_exposed_by_default(self, store, work_item_id):
        cid = pg.create_claim(store, work_item_id, "ag-hidden", ttl_seconds=300)
        claim = pg.get_claim(store, cid)
        assert "claim_token" not in claim or claim.get("claim_token_redacted") is True

    def test_heartbeat_extends_ttl(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Hb-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-hb", ttl_seconds=60)
        claim = pg.get_claim(store, cid, include_secret=True)
        pg.heartbeat_claim(store, cid, claim["claim_token"], ttl_seconds=600)
        updated = pg.get_claim(store, cid)
        assert updated is not None

    def test_release_deletes_claim(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Rel-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-rel", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        pg.release_claim(store, cid, claim["claim_token"])
        assert pg.get_claim(store, cid) is None

    def test_handoff_rotates_token_and_changes_agent(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ho-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-from", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        old_token = claim["claim_token"]
        new_claim = pg.handoff_claim(store, cid, old_token, actor="ag-to")
        assert new_claim["agent"] == "ag-to"
        assert new_claim["claim_token"] != old_token

    def test_explicit_lost_proof_adoption_rotates_remote_claim_token(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Adopt-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-from", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)

        with pytest.raises(ValueError, match="Invalid claim_token"):
            pg.handoff_claim(
                store, cid, "not-the-token", actor="ag-to", allow_legacy_adopt=True
            )

        adopted = pg.handoff_claim(
            store, cid, None, actor="ag-to", allow_legacy_adopt=True, mode="rotate"
        )
        assert adopted["claim_token"] != claim["claim_token"]

        events = pg.list_events(store, sprint_id)
        handoff = [event for event in events if event["event_type"] == "claim-handoff"][-1]
        payload = json.loads(handoff["payload"])
        assert payload["lost_proof_adopted"] is True

    def test_find_claim_by_instance_id(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Fi-{_uid()}")
        inst = f"inst-{_uid()}"
        pg.create_claim(store, iid, "ag-fi", ttl_seconds=300, instance_id=inst)
        found = pg.find_claim_by_identity(store, instance_id=inst)
        assert len(found) == 1
        assert found[0]["instance_id"] == inst

    def test_list_claims_by_sprint(self, store, sprint_id, work_item_id):
        pg.create_claim(store, work_item_id, "ag-ls", ttl_seconds=300)
        claims = pg.list_claims_by_sprint(store, sprint_id)
        assert any(c["work_item_id"] == work_item_id for c in claims)

    def test_list_claims(self, store, work_item_id):
        pg.create_claim(store, work_item_id, "ag-lc", ttl_seconds=300)
        claims = pg.list_claims(store, work_item_id)
        assert isinstance(claims, list)

    def test_conflict_on_double_exclusive_claim(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cc-{_uid()}")
        pg.create_claim(store, iid, "ag-1", ttl_seconds=300)
        with pytest.raises(ClaimConflict):
            pg.create_claim(store, iid, "ag-2", ttl_seconds=300)

    def test_concurrent_exclusive_claims_serialize_on_work_item_row(
        self, store, sprint_id, track_id, monkeypatch
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cr-{_uid()}")
        first_locked = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        second_locked = threading.Event()
        original_lock = pg._lock_claim_arbitration_row

        def instrumented_lock(cur, independent_store, work_item_id):
            if threading.current_thread().name == "claim-worker-b":
                second_attempted.set()
            original_lock(cur, independent_store, work_item_id)
            if threading.current_thread().name == "claim-worker-a":
                first_locked.set()
                assert release_first.wait(timeout=5)
            else:
                second_locked.set()

        monkeypatch.setattr(pg, "_lock_claim_arbitration_row", instrumented_lock)
        outcomes = []
        outcomes_lock = threading.Lock()

        def worker(actor):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                claim_id = pg.create_claim(independent_store, iid, actor, ttl_seconds=300)
                outcome = {"actor": actor, "result": "accepted", "claim_id": claim_id}
            except ClaimConflict as exc:
                outcome = {"actor": actor, "result": "rejected", "error": str(exc)}
            finally:
                conn.close()
            with outcomes_lock:
                outcomes.append(outcome)

        first = threading.Thread(target=worker, args=("ag-a",), name="claim-worker-a")
        second = threading.Thread(target=worker, args=("ag-b",), name="claim-worker-b")
        first.start()
        assert first_locked.wait(timeout=5)
        second.start()
        assert second_attempted.wait(timeout=5)
        assert not second_locked.wait(timeout=0.1), "second claim bypassed the arbitration lock"
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()

        assert sorted(outcome["result"] for outcome in outcomes) == ["accepted", "rejected"]
        with store.conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS count FROM claim
                WHERE repo_id = %s AND work_item_id = %s AND exclusive = true
                  AND expires_at > now()
                """,
                (store.repo_id, iid),
            )
            assert cur.fetchone()["count"] == 1


# ---------------------------------------------------------------------------
# Ref
# ---------------------------------------------------------------------------

class TestRef:
    def test_add_list_remove(self, store, work_item_id):
        rid = pg.add_ref(store, work_item_id, "pr",
                         "https://github.com/org/repo/pull/1")
        refs = pg.list_refs(store, work_item_id)
        assert any(r["id"] == rid for r in refs)
        pg.remove_ref(store, rid, work_item_id)
        assert not any(r["id"] == rid for r in pg.list_refs(store, work_item_id))

    def test_invalid_ref_type_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_ref(store, work_item_id, "tweet", "https://example.com")


# ---------------------------------------------------------------------------
# Dep
# ---------------------------------------------------------------------------

class TestDep:
    def test_add_and_list(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Da-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Db-{_uid()}")
        pg.add_dep(store, a, b)
        assert any(d["item_id"] == a for d in pg.list_deps_blocking(store, b))
        assert any(d["blocked_item_id"] == b for d in pg.list_deps_blocked_by(store, a))

    def test_remove_dep(self, store, sprint_id, track_id):
        x = pg.create_work_item(store, sprint_id, track_id, f"Dx-{_uid()}")
        y = pg.create_work_item(store, sprint_id, track_id, f"Dy-{_uid()}")
        dep_id = pg.add_dep(store, x, y)
        pg.remove_dep(store, dep_id, x)
        assert pg.list_deps_blocking(store, y) == []

    def test_get_ready_items(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Ra-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Rb-{_uid()}")
        pg.add_dep(store, a, b)
        ready_ids = {i["id"] for i in pg.get_ready_items(store, sprint_id)}
        assert a in ready_ids
        assert b not in ready_ids

    def test_self_dep_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_dep(store, work_item_id, work_item_id)


# ---------------------------------------------------------------------------
# NDJSON round-trip (export sqlite → import pg)
# ---------------------------------------------------------------------------

class TestNdjsonRoundTrip:
    def test_trusted_state_transfer_preserves_typed_event_ids_exactly(self, store):
        sprint_id = 2_000_000_000 + int(uuid.uuid4().hex[:6], 16)
        boundary_event_id = sprint_id + 1
        receipt_event_id = sprint_id + 2
        receipt_id = f"{store.repo_id}.2026-07-13.migration"
        receipt_payload = {
            "project": store.repo_id,
            "receipt_id": receipt_id,
            "receipt_path": (
                f"/projects/dev/_artifacts/{store.repo_id}/capability/receipts/"
                f"{receipt_id}.json"
            ),
            "receipt_sha256": "c" * 64,
        }
        records = [
            {
                "table": "sprint",
                "repo_id": store.repo_id,
                "data": {
                    "id": sprint_id,
                    "name": f"Trusted-{_uid()}",
                    "goal": "G",
                    "status": "closed",
                },
            },
            {
                "table": "event",
                "repo_id": store.repo_id,
                "data": {
                    "id": boundary_event_id,
                    "sprint_id": sprint_id,
                    "work_item_id": None,
                    "source_type": "actor",
                    "actor": "operator",
                    "event_type": contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                    "payload": {"previous_status": "active", "status": "closed"},
                },
            },
            {
                "table": "event",
                "repo_id": store.repo_id,
                "data": {
                    "id": receipt_event_id,
                    "sprint_id": sprint_id,
                    "work_item_id": None,
                    "source_type": "actor",
                    "actor": "drafting-agent",
                    "event_type": contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                    "payload": receipt_payload,
                },
            },
        ]

        pg.import_ndjson(store, records, trusted_state_transfer=True)

        events = {event["id"]: event for event in pg.list_events(store, sprint_id)}
        assert events[boundary_event_id]["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
        assert events[receipt_event_id]["event_type"] == contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE
        assert json.loads(events[receipt_event_id]["payload"]) == receipt_payload
        with pytest.raises(ValueError, match="cannot remap IDs"):
            pg.import_ndjson(
                store,
                records,
                remap_ids=True,
                trusted_state_transfer=True,
            )

    def test_sqlite_to_pg_full_round_trip(self, store, tmp_path):
        """Export from a fresh sqlite db, import into pg, verify data survives."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rt.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        rt_repo_id = f"rt-{_uid()}"
        sid = _db.create_sprint(conn, "RT Sprint", "G", "2026-01-01", "2026-12-31", "active")
        tid = _db.get_or_create_track(conn, sid, "eng")
        iid = _db.create_work_item(conn, sid, tid, "RT Item")
        _db.create_event(conn, sid, actor="a", event_type="note", source_type="actor",
                         work_item_id=iid, payload={"summary": "round-trip"})
        _db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/99")
        conn.commit()

        buf = io.StringIO()
        export_counts = pg.export_ndjson(conn, rt_repo_id, buf)
        conn.close()

        rt_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row),
                              repo_id=rt_repo_id)
        pg.init_db(rt_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            import_counts = pg.import_ndjson(rt_store, records, remap_ids=True)

            assert import_counts["sprint"] == export_counts["sprint"] == 1
            assert import_counts["track"] == export_counts["track"] == 1
            assert import_counts["work_item"] == export_counts["work_item"] == 1
            assert import_counts["event"] == export_counts["event"] == 1
            assert import_counts["ref"] == export_counts["ref"] == 1

            sprints = pg.list_sprints(rt_store)
            assert any(s["name"] == "RT Sprint" for s in sprints)
            items = pg.list_work_items(rt_store)
            assert any(i["title"] == "RT Item" for i in items)
        finally:
            with rt_store.conn.cursor() as cur:
                for table in ("dep", "ref", "claim", "event", "work_item", "track", "sprint"):
                    cur.execute(f"DELETE FROM {table} WHERE repo_id = %s", (rt_repo_id,))
            rt_store.conn.commit()
            rt_store.conn.close()

    def test_import_with_replace_clears_existing_data(self, store, tmp_path):
        """import_ndjson with replace=True should delete existing rows first."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rep.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        repl_repo_id = f"repl-{_uid()}"
        sid = _db.create_sprint(conn, "Repl Sprint", "G", "2026-01-01", "2026-12-31", "active")
        conn.commit()

        buf = io.StringIO()
        pg.export_ndjson(conn, repl_repo_id, buf)
        conn.close()

        repl_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row),
                                repo_id=repl_repo_id)
        pg.init_db(repl_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            pg.import_ndjson(repl_store, records, remap_ids=True)
            # Re-importing with replace should succeed without unique-violation errors
            pg.import_ndjson(repl_store, records, replace=True, remap_ids=True)
            assert len(pg.list_sprints(repl_store)) == 1
        finally:
            with repl_store.conn.cursor() as cur:
                for table in ("dep", "ref", "claim", "event", "work_item", "track", "sprint"):
                    cur.execute(f"DELETE FROM {table} WHERE repo_id = %s", (repl_repo_id,))
            repl_store.conn.commit()
            repl_store.conn.close()


# ---------------------------------------------------------------------------
# Maintain
# ---------------------------------------------------------------------------

class TestMaintain:
    def test_purge_expired_claims(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Pu-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-pu", ttl_seconds=300)
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE claim SET expires_at = now() - interval '1 second'"
                " WHERE repo_id = %s AND id = %s",
                (store.repo_id, cid),
            )
        store.conn.commit()
        deleted = pg.purge_expired_claims(store, sprint_id)
        assert deleted >= 1


# ---------------------------------------------------------------------------
# Takeup
# ---------------------------------------------------------------------------

class TestTakeup:
    def test_list_takeup_history_structure(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "sprint-taken-up", source_type="actor",
                        payload={"summary": "up", "detail": None, "actor_kind": "agent",
                                 "hostname": "h", "pid": 1, "instance_id": "i1",
                                 "runtime_session_id": None})
        history = pg.list_takeup_history(store, sprint_id)
        assert "active_takeups" in history
        assert isinstance(history["active_takeups"], list)

    def test_list_active_takeups(self, store, sprint_id):
        result = pg.list_active_takeups(store, sprint_id)
        assert isinstance(result, list)
