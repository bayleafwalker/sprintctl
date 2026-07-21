from datetime import datetime, timezone
import sqlite3

import pytest

from sprintctl import projection


def _record(offset: int, value: str = "received") -> projection.CachedIngestRecord:
    return projection.CachedIngestRecord(
        ingest_offset=offset,
        record={"event_id": f"event-{offset}", "kind": "remote.observation", "value": value},
    )


def test_apply_persists_remote_records_and_visible_watermark_atomically(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")

    watermark = projection.apply_ingested_records(
        conn,
        [_record(1), _record(2)],
        advanced_at="2026-07-14T12:00:00Z",
    )

    assert watermark == projection.ProjectionWatermark(2, "2026-07-14T12:00:00Z")
    assert projection.list_cached_records(conn) == [_record(1), _record(2)]
    assert watermark.age_seconds(datetime(2026, 7, 14, 12, 0, 9, tzinfo=timezone.utc)) == 9
    conn.close()


def test_failed_record_application_rolls_back_records_and_watermark(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    conn.execute(
        """
        CREATE TRIGGER reject_second_cached_record
        BEFORE INSERT ON cached_ingest_record WHEN NEW.ingest_offset = 2
        BEGIN
            SELECT RAISE(ABORT, 'injected projection application failure');
        END;
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected projection application failure"):
        projection.apply_ingested_records(conn, [_record(1), _record(2)])

    assert projection.list_cached_records(conn) == []
    assert projection.get_watermark(conn) == projection.ProjectionWatermark(0, None)
    conn.close()


def test_existing_offsets_replay_idempotently_and_conflicting_replay_rejects(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    projection.apply_ingested_records(conn, [_record(1)], advanced_at="2026-07-14T12:00:00Z")

    replayed = projection.apply_ingested_records(conn, [_record(1)], advanced_at="2026-07-14T12:01:00Z")
    assert replayed == projection.ProjectionWatermark(1, "2026-07-14T12:00:00Z")

    with pytest.raises(projection.ProjectionConflictError, match="different data"):
        projection.apply_ingested_records(conn, [_record(1, "changed")])
    assert projection.list_cached_records(conn) == [_record(1)]
    conn.close()


def test_gaps_and_out_of_order_inputs_are_rejected_without_advancing_watermark(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")

    with pytest.raises(projection.ProjectionGapError, match="expected 1, received 2"):
        projection.apply_ingested_records(conn, [_record(2)])
    with pytest.raises(ValueError, match="strictly increasing"):
        projection.apply_ingested_records(conn, [_record(2), _record(1)])

    assert projection.get_watermark(conn) == projection.ProjectionWatermark(0, None)
    assert projection.list_cached_records(conn) == []
    conn.close()


def test_serializable_input_and_unapplied_watermark_age(tmp_path):
    record = _record(1)
    assert projection.CachedIngestRecord.from_dict(record.to_dict()) == record
    with pytest.raises(ValueError, match="exactly ingest_offset and record"):
        projection.CachedIngestRecord.from_dict({"ingest_offset": 1, "record": {}, "extra": True})

    conn = projection.open_cached_projection(tmp_path / "projection.db")
    assert projection.get_watermark(conn).age_seconds() is None


def test_authority_decisions_advance_separate_sparse_watermark_atomically(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    decisions = [
        projection.CachedAuthorityDecision(
            decision_ingest_offset=3,
            decision={"request_event_id": "request-1", "outcome": "accepted"},
        ),
        projection.CachedAuthorityDecision(
            decision_ingest_offset=8,
            decision={"request_event_id": "request-2", "outcome": "rejected"},
        ),
    ]

    watermark = projection.apply_authority_decisions(
        conn,
        decisions,
        advanced_at="2026-07-14T18:00:00Z",
    )

    assert watermark == projection.ProjectionWatermark(8, "2026-07-14T18:00:00Z")
    assert projection.get_watermark(conn).ingest_offset == 0
    assert projection.list_cached_authority_decisions(conn) == decisions


def test_authority_decision_failure_rolls_back_rows_and_watermark(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "projection.db")
    conn.executescript(
        """
        CREATE TRIGGER reject_second_authority_decision
        BEFORE INSERT ON cached_authority_decision
        WHEN NEW.decision_ingest_offset = 4
        BEGIN
            SELECT RAISE(ABORT, 'injected authority cache failure');
        END;
        """
    )
    decisions = [
        projection.CachedAuthorityDecision(2, {"outcome": "accepted"}),
        projection.CachedAuthorityDecision(4, {"outcome": "rejected"}),
    ]

    with pytest.raises(Exception, match="injected authority cache failure"):
        projection.apply_authority_decisions(conn, decisions)

    assert projection.list_cached_authority_decisions(conn) == []
    assert projection.get_authority_watermark(conn).ingest_offset == 0
    conn.close()


def test_offline_catch_up_and_full_log_rebuild_converge(tmp_path):
    remote_history = [_record(1), _record(2), _record(3), _record(4)]
    offline_cache = projection.open_cached_projection(tmp_path / "offline.db")
    projection.apply_ingested_records(
        offline_cache,
        remote_history[:1],
        advanced_at="2026-07-14T12:00:00Z",
    )

    # The remote commits later offsets while this reader remains offline.
    assert projection.get_watermark(offline_cache).ingest_offset == 1
    assert projection.list_cached_records(offline_cache) == remote_history[:1]

    projection.apply_ingested_records(
        offline_cache,
        remote_history[1:],
        advanced_at="2026-07-14T12:05:00Z",
    )
    rebuilt_cache = projection.open_cached_projection(tmp_path / "rebuilt.db")
    projection.apply_ingested_records(
        rebuilt_cache,
        remote_history,
        advanced_at="2026-07-14T12:05:00Z",
    )

    assert projection.list_cached_records(offline_cache) == remote_history
    assert projection.list_cached_records(rebuilt_cache) == remote_history
    assert projection.get_watermark(offline_cache) == projection.get_watermark(rebuilt_cache)
    offline_cache.close()
    rebuilt_cache.close()


def test_retained_suffix_cannot_advance_an_empty_projection(tmp_path):
    conn = projection.open_cached_projection(tmp_path / "retained-suffix.db")

    with pytest.raises(projection.ProjectionGapError, match="expected 1, received 3"):
        projection.apply_ingested_records(conn, [_record(3), _record(4)])

    assert projection.list_cached_records(conn) == []
    assert projection.get_watermark(conn) == projection.ProjectionWatermark(0, None)
    conn.close()


def test_schema_v1_cache_fails_closed_until_atomic_rebuild(tmp_path):
    path = tmp_path / "schema-v1.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE cached_ingest_watermark (
            singleton INTEGER PRIMARY KEY,
            ingest_offset INTEGER NOT NULL,
            advanced_at TEXT
        );
        INSERT INTO cached_ingest_watermark VALUES (1, 7, '2026-07-21T12:00:00Z');
        CREATE TABLE cached_projection_meta (
            singleton INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL
        );
        INSERT INTO cached_projection_meta VALUES (1, 1);
        """
    )
    legacy.commit()
    legacy.close()

    reopened = projection.open_cached_projection(path, repo_id="repo-a")
    try:
        health = projection.assess_freshness(reopened)
        assert health.schema_version == 1
        assert health.fallback_reason == "schema-upgrade-required"
        with pytest.raises(RuntimeError, match="requires an atomic rebuild"):
            projection.apply_ingested_records(reopened, [_record(8)])
    finally:
        reopened.close()


def test_high_water_rebuild_rejects_suffix_and_preserves_live_cache(tmp_path):
    path = tmp_path / "projection.db"
    live = projection.open_cached_projection(path, repo_id="repo-a")
    projection.apply_ingested_records(live, [_record(1, "live")])
    live.close()

    with pytest.raises(projection.ProjectionGapError, match="expected 1, received 3"):
        projection.rebuild_cached_projection(
            path,
            repo_id="repo-a",
            captured_high_water=3,
            fetch_page=lambda _after, _limit: [_record(3), _record(4)],
        )

    reopened = projection.open_cached_projection(path, repo_id="repo-a")
    try:
        assert projection.get_watermark(reopened).ingest_offset == 1
        assert projection.list_cached_records(reopened) == [_record(1, "live")]
    finally:
        reopened.close()
    assert list(tmp_path.glob(".projection.db.rebuild-*")) == []


def test_high_water_rebuild_replaces_cache_only_after_contiguous_capture(tmp_path):
    path = tmp_path / "projection.db"
    old = projection.open_cached_projection(path, repo_id="repo-a")
    projection.apply_ingested_records(old, [_record(1, "old")])
    old.close()
    remote = [_record(1, "new"), _record(2, "new")]

    rebuilt = projection.rebuild_cached_projection(
        path,
        repo_id="repo-a",
        captured_high_water=2,
        fetch_page=lambda after, limit: [
            record for record in remote if record.ingest_offset > after
        ][:limit],
        batch_size=1,
    )

    assert rebuilt.ingest_offset == 2
    reopened = projection.open_cached_projection(path, repo_id="repo-a")
    try:
        assert projection.list_cached_records(reopened) == remote
    finally:
        reopened.close()
