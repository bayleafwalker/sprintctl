"""PostgreSQL integration tests: NdjsonRoundTrip.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    contracts,
    pg,
    _uid,
    PG_MARKS,
    _PG_URL,
    io,
    json,
    threading,
    uuid,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


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

    def test_sqlite_to_pg_full_round_trip(self, store, pg_test_scope, tmp_path):
        """Export from a fresh sqlite db, import into pg, verify data survives."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rt.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        rt_repo_id = pg_test_scope("round-trip")
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
            rt_store.conn.close()

    def test_import_with_replace_clears_existing_data(self, store, pg_test_scope, tmp_path):
        """import_ndjson with replace=True should delete existing rows first."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rep.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        repl_repo_id = pg_test_scope("replace")
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
            repl_store.conn.close()

    def test_concurrent_imports_serialize_identity_sequence_advance(
        self, store, pg_test_scope, monkeypatch
    ):
        """Regression test for sprintctl#1250.

        _advance_identity_sequences reads MAX(id) then setval()s the shared,
        cross-repo identity sequence to it. Without IDENTITY_SEQUENCE_LOCK_KEYS,
        two concurrent import_ndjson calls can each read a stale MAX(id) under
        READ COMMITTED (neither sees the other's still-uncommitted rows), and
        because sequence state is non-transactional, whichever call's setval()
        physically executes last wins -- even if it reflects fewer rows. That
        can regress the sequence below rows the *other* caller already
        committed, so a naive test asserting only "my own rows are covered"
        would not catch it; this asserts the sequence covers the max id across
        *both* callers.

        Proves two things: (1) the second call's advisory-lock acquisition
        genuinely blocks until the first call's transaction commits (not just
        that results happen to come out right), and (2) the resulting sequence
        value is >= the true combined max of both callers' rows.
        """
        repo_a = pg_test_scope("seq-lock-a")
        repo_b = pg_test_scope("seq-lock-b")
        base = 2_000_000_000 + int(uuid.uuid4().hex[:6], 16)
        ids_a = [base, base + 1, base + 2]
        ids_b = [base + 1000, base + 1001, base + 1002]

        def sprint_records(repo_id, ids):
            return [
                {
                    "table": "sprint",
                    "repo_id": repo_id,
                    "data": {"id": i, "name": f"SeqLock-{i}", "goal": "G", "status": "active"},
                }
                for i in ids
            ]

        first_paused = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        second_completed = threading.Event()
        original_advance = pg._advance_identity_sequences

        def instrumented_advance(cur, tables):
            name = threading.current_thread().name
            if name == "import-worker-b":
                second_attempted.set()
            original_advance(cur, tables)
            if name == "import-worker-a":
                first_paused.set()
                assert release_first.wait(timeout=5)
            else:
                second_completed.set()

        monkeypatch.setattr(pg, "_advance_identity_sequences", instrumented_advance)
        failures = []

        def worker(repo_id, ids):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=repo_id)
            try:
                pg.import_ndjson(independent_store, sprint_records(repo_id, ids))
            except BaseException as exc:  # Preserve a worker failure for assertion.
                failures.append(exc)
            finally:
                conn.close()

        first = threading.Thread(
            target=worker, args=(repo_a, ids_a), name="import-worker-a"
        )
        second = threading.Thread(
            target=worker, args=(repo_b, ids_b), name="import-worker-b"
        )
        try:
            first.start()
            assert first_paused.wait(timeout=5), "first import never reached the critical section"
            second.start()
            assert not second_attempted.wait(timeout=0.2), (
                "second import bypassed the identity-sequence advisory lock while "
                "the first import's transaction was still open"
            )
            release_first.set()
            first.join(timeout=15)
            second.join(timeout=15)
            assert not first.is_alive() and not second.is_alive()
            assert second_completed.wait(timeout=5)
        finally:
            release_first.set()  # Never leave the first worker parked on failure.

        assert failures == []

        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_get_serial_sequence('sprint', 'id') AS seq")
            seq_name = cur.fetchone()["seq"]
            cur.execute(f"SELECT last_value FROM {seq_name}")  # noqa: S608 - identifier from pg catalog, not user input
            last_value = cur.fetchone()["last_value"]

        true_max = max(ids_a + ids_b)
        assert last_value >= true_max, (
            f"sequence last_value={last_value} regressed below the combined "
            f"max id={true_max} across both concurrent importers"
        )

        # Defense in depth: the next ordinary (non-explicit-id) insert must not
        # collide with either caller's explicitly-imported rows.
        next_id = pg.create_sprint(
            pg.PgStore(conn=store.conn, repo_id=repo_a), f"After-{uuid.uuid4().hex[:6]}", "G",
            status="active",
        )
        assert next_id > true_max
