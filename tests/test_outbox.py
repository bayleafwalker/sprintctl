import sqlite3
import threading

import pytest

from sprintctl import outbox


def _append(conn, event_id=None, **kwargs):
    return outbox.append_observation(
        conn,
        event_type="work.completed",
        actor="agent-a",
        payload={"item": "outbox-foundation"},
        event_id=event_id,
        **kwargs,
    )


def test_record_survives_restart_with_required_provenance(tmp_path):
    path = tmp_path / "producer-outbox.db"
    conn = outbox.open_outbox(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    record = _append(
        conn,
        event_id="event-1",
        runtime_session_id="session-1",
        basis_revision="item:7@rev:3",
        correlation_id="correlation-1",
        causation_id="cause-1",
        occurred_at="2026-07-14T10:00:00Z",
    )
    conn.close()

    reopened = outbox.open_outbox(path)
    records = outbox.list_records(reopened)

    assert len(records) == 1
    assert records[0] == record
    assert records[0].origin_seq == 1
    assert records[0].record_class == outbox.OBSERVATION
    assert records[0].schema_version == outbox.OUTBOX_SCHEMA_VERSION
    assert records[0].payload_sha256
    assert outbox.get_origin_stream_id(reopened) == record.origin_stream_id
    reopened.close()


def test_idempotent_retry_does_not_consume_a_sequence(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer-outbox.db")

    first = _append(conn, event_id="event-1")
    retried = _append(conn, event_id="event-1")
    second = _append(conn, event_id="event-2")

    assert retried == first
    assert [record.origin_seq for record in outbox.list_records(conn)] == [1, 2]
    assert second.origin_seq == 2
    conn.close()


def test_reused_event_id_for_different_observation_is_rejected(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer-outbox.db")
    _append(conn, event_id="event-1")

    with pytest.raises(outbox.OutboxIdempotencyError, match="different observation"):
        outbox.append_observation(
            conn,
            event_type="work.completed",
            actor="agent-a",
            payload={"item": "different"},
            event_id="event-1",
        )

    assert [record.origin_seq for record in outbox.list_records(conn)] == [1]
    conn.close()


def test_failed_insert_rolls_back_stream_creation_and_sequence(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer-outbox.db")
    conn.execute(
        """
        CREATE TRIGGER reject_outbox_insert
        BEFORE INSERT ON outbox_record
        BEGIN
            SELECT RAISE(ABORT, 'injected insert failure');
        END;
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        _append(conn, event_id="event-1")

    assert outbox.get_origin_stream_id(conn) is None
    conn.execute("DROP TRIGGER reject_outbox_insert")
    conn.commit()
    record = _append(conn, event_id="event-1")

    assert record.origin_seq == 1
    conn.close()


def test_records_are_immutable(tmp_path):
    conn = outbox.open_outbox(tmp_path / "producer-outbox.db")
    record = _append(conn, event_id="event-1")

    with pytest.raises(sqlite3.IntegrityError, match="outbox records are immutable"):
        conn.execute("UPDATE outbox_record SET actor = 'other' WHERE event_id = ?", (record.event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="outbox records are immutable"):
        conn.execute("DELETE FROM outbox_record WHERE event_id = ?", (record.event_id,))

    conn.rollback()
    assert outbox.list_records(conn) == [record]
    conn.close()


def test_concurrent_appends_use_contiguous_single_stream_sequences(tmp_path):
    path = tmp_path / "producer-outbox.db"
    outbox.open_outbox(path).close()
    barrier = threading.Barrier(3)
    failures: list[Exception] = []

    def worker(actor: str):
        conn = outbox.open_outbox(path)
        try:
            barrier.wait()
            for index in range(12):
                outbox.append_observation(
                    conn,
                    event_type="work.progressed",
                    actor=actor,
                    payload={"index": index},
                    event_id=f"{actor}-{index}",
                )
        except Exception as exc:  # pragma: no cover - asserted through failures
            failures.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(actor,)) for actor in ("agent-a", "agent-b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    conn = outbox.open_outbox(path)
    records = outbox.list_records(conn)
    assert [record.origin_seq for record in records] == list(range(1, 25))
    assert len({record.origin_stream_id for record in records}) == 1
    conn.close()
