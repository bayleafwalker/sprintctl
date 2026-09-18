"""PostgreSQL integration tests: S1 hardening (schema 13).

Authority evidence is immutable for every role, and deleting a sprint can no
longer erase its events.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    authority,
    outbox,
    pg,
    pg_migrations,
    _append_authority_command,
    _uid,
    PG_MARKS,
    uuid,
    psycopg,
)

pytestmark = PG_MARKS


def _ingest_one(store, tmp_path):
    conn = outbox.open_outbox(tmp_path / "s1-outbox.db")
    try:
        record = outbox.append_observation(
            conn,
            event_type="work.completed",
            actor="producer-s1",
            payload={"index": 1},
            event_id=f"s1-{uuid.uuid4().hex}",
            occurred_at="2026-09-18T12:00:00Z",
        )
    finally:
        conn.close()
    return pg.ingest_records(store, [record])[0]


def _decide_one(store, tmp_path):
    sprint_id = pg.create_sprint(store, f"S1-{_uid()}", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "s1")
    item_id = pg.create_work_item(store, sprint_id, track_id, f"S1-item-{_uid()}")
    item = pg.get_work_item(store, item_id)
    producer = outbox.open_outbox(tmp_path / "s1-authority.db")
    try:
        command = _append_authority_command(
            producer,
            store,
            record_type="item.transition",
            aggregate_type="item",
            aggregate_uuid=item["aggregate_uuid"],
            basis_revision=authority.item_revision(item),
            payload={"to_status": "active"},
        )
        return authority.arbitrate_command(store, command)
    finally:
        producer.close()


def _refused(store, statement, params=()):
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with store.conn.cursor() as cur:
            cur.execute(statement, params)
    store.conn.rollback()


class TestSchema13:
    def test_schema_is_13(self):
        assert pg_migrations.CURRENT_SCHEMA_VERSION == 13
        assert pg_migrations.MINIMUM_SCHEMA_VERSION == 13

    def test_ingest_records_refuse_update_delete_and_truncate(self, store, tmp_path):
        admitted = _ingest_one(store, tmp_path)
        store.conn.commit()
        _refused(
            store,
            "UPDATE ingest_record SET actor='tampered' WHERE repo_id=%s AND ingest_offset=%s",
            (store.repo_id, admitted.ingest_offset),
        )
        _refused(
            store,
            "DELETE FROM ingest_record WHERE repo_id=%s AND ingest_offset=%s",
            (store.repo_id, admitted.ingest_offset),
        )
        _refused(store, "TRUNCATE ingest_record CASCADE")

    def test_authority_decisions_refuse_update_delete_and_truncate(self, store, tmp_path):
        decision = _decide_one(store, tmp_path)
        assert decision.accepted is True
        store.conn.commit()
        _refused(
            store,
            "UPDATE authority_decision SET outcome='rejected' WHERE repo_id=%s",
            (store.repo_id,),
        )
        _refused(store, "DELETE FROM authority_decision WHERE repo_id=%s", (store.repo_id,))
        _refused(store, "TRUNCATE authority_decision")

    def test_deleting_a_sprint_with_events_is_refused(self, store):
        sprint_id = pg.create_sprint(store, f"S1-restrict-{_uid()}", status="active")
        pg.create_event(store, sprint_id, "s1-actor", "note", payload={"text": "history"})
        store.conn.commit()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with store.conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sprint WHERE repo_id=%s AND id=%s",
                    (store.repo_id, sprint_id),
                )
        store.conn.rollback()
        assert pg.get_sprint(store, sprint_id) is not None

    def test_migration_is_idempotent_at_13(self, store):
        pg_migrations.migrate_schema(store)
        result = pg_migrations.migrate_schema(store)
        assert result["applied_versions"] == []
        assert result["to_version"] == 13
        with store.conn.cursor() as cur:
            cur.execute(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid='event'::regclass AND confrelid='sprint'::regclass"
            )
            assert [row["confdeltype"] for row in cur.fetchall()] == ["r"]
        store.conn.rollback()
