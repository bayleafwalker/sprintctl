"""PostgreSQL integration tests: ProducerOutboxIngestion.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    authority,
    observations,
    pg,
    projection,
    sync,
    outbox,
    _uid,
    PG_MARKS,
    _PG_URL,
    replace,
    threading,
    uuid,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


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

    def test_interleaved_repositories_each_publish_contiguous_offsets(
        self, pg_test_scope, store, tmp_path
    ):
        repo_a = pg.PgStore(store.conn, pg_test_scope("cursor-interleaved-a"))
        repo_b = pg.PgStore(store.conn, pg_test_scope("cursor-interleaved-b"))
        first_a, second_a = self._records(tmp_path / "repo-a")
        first_b, second_b = self._records(tmp_path / "repo-b")

        outcomes = [
            pg.ingest_records(repo_a, [first_a])[0],
            pg.ingest_records(repo_b, [first_b])[0],
            pg.ingest_records(repo_a, [second_a])[0],
            pg.ingest_records(repo_b, [second_b])[0],
        ]

        assert [outcomes[index].ingest_offset for index in (0, 2)] == [1, 2]
        assert [outcomes[index].ingest_offset for index in (1, 3)] == [1, 2]
        assert pg.get_ingest_high_water(repo_a) == 2
        assert pg.get_ingest_high_water(repo_b) == 2

    def test_concurrent_streams_in_one_repository_allocate_offsets_one_and_two(
        self, pg_test_scope, store, tmp_path
    ):
        repo_id = pg_test_scope("cursor-concurrent-same-repo")
        records = [
            self._records(tmp_path / f"same-repo-{index}", count=1)[0]
            for index in range(2)
        ]
        barrier = threading.Barrier(3)
        outcomes = []
        failures = []

        def worker(record):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent = pg.PgStore(conn, repo_id)
            try:
                barrier.wait(timeout=15)
                outcomes.append(pg.ingest_records(independent, [record])[0])
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(record,)) for record in records]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=15)
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        assert {outcome.ingest_offset for outcome in outcomes} == {1, 2}

    def test_concurrent_first_appends_use_public_one_and_distinct_internal_ids(
        self, pg_test_scope, store, tmp_path
    ):
        repo_ids = [
            pg_test_scope("cursor-concurrent-repo-a"),
            pg_test_scope("cursor-concurrent-repo-b"),
        ]
        records = [
            self._records(tmp_path / f"first-repo-{index}", count=1)[0]
            for index in range(2)
        ]
        barrier = threading.Barrier(3)
        outcomes = []
        failures = []

        def worker(repo_id, record):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent = pg.PgStore(conn, repo_id)
            try:
                barrier.wait(timeout=15)
                outcome = pg.ingest_records(independent, [record])[0]
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT ingest_id FROM ingest_record "
                        "WHERE repo_id = %s AND event_id = %s",
                        (repo_id, record.event_id),
                    )
                    ingest_id = int(cur.fetchone()["ingest_id"])
                outcomes.append((repo_id, outcome.ingest_offset, ingest_id))
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=pair)
            for pair in zip(repo_ids, records, strict=True)
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=15)
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        assert [offset for _repo, offset, _ingest_id in outcomes] == [1, 1]
        assert len({ingest_id for _repo, _offset, ingest_id in outcomes}) == 2

    def test_lost_response_retry_does_not_advance_repository_cursor(
        self, pg_test_scope, store, tmp_path
    ):
        isolated = pg.PgStore(store.conn, pg_test_scope("cursor-lost-response"))
        first, second = self._records(tmp_path / "lost-response")

        admitted = pg.ingest_records(isolated, [first])[0]
        retried = pg.ingest_records(isolated, [first])[0]
        following = pg.ingest_records(isolated, [second])[0]

        assert (admitted.ingest_offset, retried.ingest_offset) == (1, 1)
        assert retried.duplicate is True
        assert following.ingest_offset == 2
        assert pg.get_ingest_high_water(isolated) == 2

    def test_failure_after_offset_allocation_rolls_back_cursor_and_record(
        self, pg_test_scope, store, tmp_path
    ):
        repo_id = pg_test_scope("cursor-rollback")
        isolated = pg.PgStore(store.conn, repo_id)
        first = self._records(tmp_path / "cursor-rollback", count=1)[0]
        suffix = uuid.uuid4().hex
        function_name = f"reject_cursor_record_{suffix}"
        trigger_name = f"reject_cursor_record_{suffix}"
        with store.conn.cursor() as cur:
            cur.execute(
                f"CREATE FUNCTION {function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
                "'injected post-allocation failure'; END; $$"
            )
            cur.execute(
                f"CREATE TRIGGER {trigger_name} AFTER INSERT ON ingest_record "
                f"FOR EACH ROW WHEN (NEW.repo_id = '{repo_id}') "
                f"EXECUTE FUNCTION {function_name}()"
            )
        store.conn.commit()
        try:
            with pytest.raises(Exception, match="injected post-allocation failure"):
                pg.ingest_records(isolated, [first])
            assert pg.get_ingest_high_water(isolated) == 0
            assert pg.list_ingested_records(isolated) == []
        finally:
            with store.conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON ingest_record")
                cur.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
            store.conn.commit()

        admitted = pg.ingest_records(isolated, [first])[0]
        assert admitted.ingest_offset == 1

    def test_item_evidence_ingest_deduplicates_stale_basis_without_status_mutation(
        self, store, tmp_path
    ):
        sprint_id = pg.create_sprint(store, f"Evidence-ingest-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Evidence-item-{_uid()}")
        pg.set_work_item_status(store, item_id, "active")
        item_before = pg.get_work_item(store, item_id)

        producer = outbox.open_outbox(tmp_path / "item-evidence.db")
        try:
            record = observations.append_item_evidence_observation(
                producer,
                event_type=observations.WORK_COMPLETED,
                actor="session-wrapper",
                repo_id=store.repo_id,
                sprint_id=sprint_id,
                work_item_id=item_id,
                runtime_session_id="session-pg-evidence",
                summary="Work completed while offline",
                evidence_refs=[
                    {
                        "kind": "git-commit",
                        "source": f"repo:{store.repo_id}",
                        "revision": "a" * 40,
                    }
                ],
                basis_revision=authority.item_revision(item_before),
                authored_at="2026-07-19T12:00:00Z",
            )
        finally:
            producer.close()

        pg.set_work_item_status(store, item_id, "done")
        current = pg.get_work_item(store, item_id)
        admitted = pg.ingest_records(store, [record])
        retried = pg.ingest_records(store, [record])
        projected = observations.project_item_evidence(
            retried[0].record,
            current_revision=authority.item_revision(current),
        )

        assert admitted[0].duplicate is False
        assert retried[0].duplicate is True
        assert retried[0].ingest_offset == admitted[0].ingest_offset
        assert projected.basis.classification is observations.BasisClassification.ANACHRONISTIC
        assert projected.runtime_session_id == "session-pg-evidence"
        assert pg.get_work_item(store, item_id)["status"] == "done"

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

    def test_seed_ingest_stream_lets_a_stranded_producer_resume(self, store, tmp_path):
        """A producer whose remote ledger history was lost (e.g. by a
        ledger-introducing/resetting schema migration -- see sprintctl-work
        schema v4's repository_ingest_cursor rollout, 2026-07-24) can never
        admit another record: the remote permanently expects sequence 1
        while the producer permanently sends whatever it already has queued
        locally. ``seed_ingest_stream`` is the explicit, operator-invoked
        recovery for exactly that, and does not weaken ordinary gap
        detection for anyone else.
        """
        stranded_stream_id = str(uuid.uuid4())
        first, second = self._records(tmp_path)
        stranded_second = replace(second, origin_stream_id=stranded_stream_id, origin_seq=7)

        with pytest.raises(pg.IngestGapError, match="expected sequence 1, received 7"):
            pg.ingest_records(store, [stranded_second])

        pg.seed_ingest_stream(store, stranded_stream_id, 6)

        admitted = pg.ingest_records(store, [stranded_second])
        assert admitted[0].record.origin_seq == 7
        assert admitted[0].duplicate is False

        with pytest.raises(pg.IngestConflictError, match="already has ingest history"):
            pg.seed_ingest_stream(store, stranded_stream_id, 100)

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

    def test_disposable_remote_history_rebuilds_projection(
        self, pg_test_scope, tmp_path
    ):
        repo_id = pg_test_scope("projection-rebuild")
        rebuild_store = pg.PgStore(
            conn=psycopg.connect(_PG_URL, row_factory=dict_row),
            repo_id=repo_id,
        )
        pg.init_db(rebuild_store)
        try:
            records = self._records(tmp_path, count=4)
            admitted = pg.ingest_records(rebuild_store, records)
            remote_history = pg.list_ingested_records(rebuild_store)
            cache = projection.open_cached_projection(tmp_path / "remote-rebuild.db")
            try:
                projection.apply_ingested_records(
                    cache,
                    [sync._cached_record(record) for record in remote_history],
                )

                assert projection.get_watermark(cache).ingest_offset == admitted[-1].ingest_offset
                assert [
                    record.record["event_id"]
                    for record in projection.list_cached_records(cache)
                ] == [record.event_id for record in records]
            finally:
                cache.close()
        finally:
            rebuild_store.conn.close()


# ---------------------------------------------------------------------------
# Sprint
# ---------------------------------------------------------------------------
