"""PostgreSQL: S3 PR5 (schema 16).

The legacy re-mark triggers and migration, the narrowed import exemption,
open-only sweeps and the repository scope of ``list_unbound``.  The served
contract runs in ``tests/pg/test_served_decisions.py``.
"""
from __future__ import annotations

import io
import json
import uuid

import pytest

from tests.pg._shared import (
    PG_MARKS,
    _PG_URL,
    _uid,
    assert_disposable_connection,
    dict_row,
    pg,
    pg_migrations,
    psycopg,
)
from tests.pg.test_decisions import _legacy_item

pytestmark = PG_MARKS

EVIDENCE = "cd" * 32


def _active_item(store, sprint_id=None, track_id=None):
    if sprint_id is None:
        sprint_id = pg.create_sprint(store, f"Remark-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "t")
    item_id = pg.create_work_item(store, sprint_id, track_id, f"open {_uid()}")
    pg.set_work_item_status(store, item_id, "active")
    return item_id


def _remark_sql(store, item_id, *, kind="reject", rationale="why", evidence=(EVIDENCE,),
                legacy_source=None, subject=None):
    """Insert a decision, then bind it -- the order record_decision uses."""
    with store.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_decision (repo_id, kind, work_item_id, evidence_digests, "
            "rationale, actor, legacy_source) VALUES (%s, %s, %s, %s::jsonb, %s, 'owner', %s) "
            "RETURNING id",
            (store.repo_id, kind, subject or item_id, json.dumps(list(evidence)), rationale,
             legacy_source),
        )
        decision_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE work_item SET resolution = %s, terminal_decision_id = %s "
            "WHERE repo_id = %s AND id = %s",
            ({"reject": "rejected", "accept": "accepted"}[kind], decision_id,
             store.repo_id, item_id),
        )
    store.conn.commit()
    return decision_id


class TestRemarkTriggers:
    @pytest.mark.parametrize(
        "decision",
        [
            {"rationale": ""},
            {"rationale": "\t\r\n"},
            {"rationale": "\u00a0 \u00a0"},
            {"evidence": ()},
            {"evidence": ("",)},
            {"evidence": (1,)},
            {"evidence": (EVIDENCE, "AB" * 32)},
            {"evidence": ("ab" * 31,)},
            {"legacy_source": "capability-receipt"},
        ],
    )
    def test_an_unauthored_or_unevidenced_remark_is_refused(self, store, decision):
        _s, _t, item_id = _legacy_item(store, "done")
        with pytest.raises(psycopg.errors.CheckViolation, match="legacy re-mark"):
            _remark_sql(store, item_id, **decision)
        store.conn.rollback()
        assert pg.get_work_item(store, item_id)["terminal_decision_id"] is None

    def test_a_decision_about_another_item_is_refused(self, store):
        _s, _t, item_id = _legacy_item(store, "done")
        _s2, _t2, other = _legacy_item(store, "active")
        with pytest.raises(psycopg.errors.CheckViolation, match="legacy re-mark"):
            _remark_sql(store, item_id, subject=other)
        store.conn.rollback()

    def test_a_remark_binds_once(self, store):
        _s, _t, item_id = _legacy_item(store, "done")
        decision_id = _remark_sql(store, item_id)
        item = pg.get_work_item(store, item_id)
        assert (item["status"], item["resolution"], item["terminal_decision_id"]) == (
            "done", "rejected", decision_id
        )
        with pytest.raises(psycopg.errors.Error, match="immutable"):
            _remark_sql(store, item_id, kind="accept")
        store.conn.rollback()

    def test_record_decision_remarks_and_keeps_updated_at(self, store):
        _s, _t, item_id = _legacy_item(store, "done")
        before = pg.get_work_item(store, item_id)
        decision = pg.record_decision(
            store, item_id, "reject", actor="owner", rationale="never built",
            evidence_digests=[EVIDENCE], idempotency_key=f"remark-{_uid()}",
        )
        after = pg.get_work_item(store, item_id)
        assert after["terminal_decision_id"] == decision["id"]
        assert after["updated_at"] == before["updated_at"]
        with pytest.raises(pg.InvalidTransition, match="terminal"):
            pg.record_decision(store, item_id, "accept", actor="owner", rationale="x",
                               evidence_digests=[EVIDENCE])


class TestSchema16Migration:
    def test_15_to_16_admits_the_remark_and_rerun_is_a_no_op(self, pg_test_scope, monkeypatch):
        schema = "legacy_remark_" + uuid.uuid4().hex
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        repo_id = pg_test_scope("remark-migration")
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{schema}"')
                cur.execute(f'SET search_path TO "{schema}"')
            conn.commit()
            store = pg.PgStore(conn, repo_id)
            # Build exactly schema 15: run the ladder with 16 withheld.
            with monkeypatch.context() as patch:
                patch.setattr(pg, "_apply_schema_version_16", lambda cur: None)
                pg_migrations.migrate_schema(store)
            with conn.cursor() as cur:
                cur.execute("UPDATE schema_version SET version = 15")
            conn.commit()
            _s, _t, item_id = _legacy_item(store, "done")
            with pytest.raises(psycopg.errors.Error, match="takes no decision"):
                _remark_sql(store, item_id)
            conn.rollback()

            migrated = pg_migrations.migrate_schema(store)
            assert migrated["applied_versions"] == [16]
            assert migrated["to_version"] == 16
            assert pg_migrations.migrate_schema(store)["applied_versions"] == []
            decision_id = _remark_sql(store, item_id)
            assert pg.get_work_item(store, item_id)["terminal_decision_id"] == decision_id
        finally:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SET search_path TO public")
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()


def _sqlite_export(tmp_path, build, repo_id: str) -> list[dict]:
    from sprintctl import db as _db

    conn = _db.get_connection(tmp_path / f"{_uid()}.db")
    _db.init_db(conn)
    sprint_id = _db.create_sprint(conn, f"Export-{_uid()}", status="active")
    track_id = _db.get_or_create_track(conn, sprint_id, "t")
    build(_db, conn, sprint_id, track_id)
    buf = io.StringIO()
    # Records name their repository; import writes each where it says.
    pg.export_ndjson(conn, repo_id, buf)
    conn.close()
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _import_store(pg_test_scope, label):
    repo_id = pg_test_scope(label)
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    return pg.PgStore(conn=conn, repo_id=repo_id)


class TestImportExemptionCoversOnlyLegacyRows:
    @staticmethod
    def _accepted_against_a_release(_db, conn, sprint_id, track_id):
        item_id = _db.create_work_item(conn, sprint_id, track_id, "released")
        _db.set_work_item_status(conn, item_id, "active")
        _db.reserve(conn, item_id, actor="a", session_id="s")
        _db.record_decision(conn, item_id, "accept", actor="owner")

    def _tampered(self, tmp_path, repo_id, *, legacy):
        records = _sqlite_export(tmp_path, self._accepted_against_a_release, repo_id)
        for record in records:
            if record["table"] == "work_decision":
                record["data"]["release_digest"] = "e" * 64
            if record["table"] == "work_item":
                record["data"]["legacy"] = 1 if legacy else 0
        return records

    def test_a_decision_naming_a_foreign_release_is_refused_for_new_work(
        self, pg_test_scope, tmp_path
    ):
        target = _import_store(pg_test_scope, "import-strict")
        try:
            with pytest.raises(psycopg.errors.CheckViolation, match="is not a release"):
                pg.import_ndjson(
                    target, self._tampered(tmp_path, target.repo_id, legacy=False),
                    remap_ids=True,
                )
            assert pg.list_work_items(target) == []
        finally:
            target.conn.close()

    def test_legacy_rows_still_carry_their_history(self, pg_test_scope, tmp_path):
        target = _import_store(pg_test_scope, "import-legacy")
        try:
            counts = pg.import_ndjson(
                target, self._tampered(tmp_path, target.repo_id, legacy=True), remap_ids=True
            )
            assert counts["work_decision"] == 1
            [item] = pg.list_work_items(target)
            assert (item["legacy"], item["resolution"]) == (True, "accepted")
        finally:
            target.conn.close()

    @staticmethod
    def _legacy_done(_db, conn, sprint_id, track_id):
        with _db.legacy_import_gate(conn):
            conn.execute(
                "INSERT INTO work_item (sprint_id, track_id, title, status, legacy, "
                "aggregate_uuid) VALUES (?, ?, 'old', 'done', 1, ?)",
                (sprint_id, track_id, str(uuid.uuid4())),
            )
        conn.commit()

    def test_a_missing_flag_is_legacy_only_in_a_pre_decision_archive(
        self, pg_test_scope, tmp_path
    ):
        target = _import_store(pg_test_scope, "import-flagless")
        records = _sqlite_export(tmp_path, self._legacy_done, target.repo_id)
        [item] = [r for r in records if r["table"] == "work_item"]
        del item["data"]["legacy"]
        # The row still carries decision-era columns: not read as legacy.
        try:
            with pytest.raises(psycopg.errors.CheckViolation, match="without a terminal decision"):
                pg.import_ndjson(target, records, remap_ids=True)
        finally:
            target.conn.close()
        # A pre-decision archive has none of them: its rows are legacy.
        for column in ("terminal_decision_id", "resolution"):
            del item["data"][column]
        target = _import_store(pg_test_scope, "import-pre-decision")
        for record in records:
            record["repo_id"] = target.repo_id
        try:
            pg.import_ndjson(target, records, remap_ids=True)
            [imported] = pg.list_work_items(target)
            assert (imported["status"], imported["legacy"]) == ("done", True)
        finally:
            target.conn.close()


class TestOpenOnlySweep:
    def test_a_stale_reservation_of_a_done_item_is_left_alone(self, store):
        sprint_id = pg.create_sprint(store, f"Sweep-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "t")
        open_item = _active_item(store, sprint_id, track_id)
        done_item = _active_item(store, sprint_id, track_id)
        open_row = pg.reserve(store, open_item, actor="a", session_id=f"s-{_uid()}")
        done_row = pg.reserve(store, done_item, actor="a", session_id=f"s-{_uid()}")
        pg.record_decision(store, done_item, "accept", actor="owner")
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation SET last_activity_at = '2020-01-01T00:00:00Z' "
                "WHERE repo_id = %s AND id = ANY(%s)",
                (store.repo_id, [open_row["id"], done_row["id"]]),
            )
        store.conn.commit()

        swept = pg.sweep_stale_reservations(store, now="2021-01-01T00:00:00Z")

        assert open_row["id"] in {row["id"] for row in swept}
        assert done_row["id"] not in {row["id"] for row in swept}
        assert pg.get_reservation(store, open_row["id"])["state"] == "interrupted"
        assert pg.get_reservation(store, done_row["id"])["state"] == "active"


def test_a_remarked_legacy_item_is_not_unbound(store):
    sprint_id, _t, item_id = _legacy_item(store, "done")
    _remark_sql(store, item_id)
    result = pg.list_unbound(store, sprint_id=sprint_id)
    assert all(section["count"] == 0 for section in result["categories"].values())
    assert result["resolutions"]["legacy_remarked"] == 1
    assert result["resolutions"]["rejected"] == 1


def test_list_unbound_is_scoped_to_the_repository(store, pg_test_scope):
    _legacy_item(store, "done")
    other = _import_store(pg_test_scope, "unbound-scope")
    try:
        pg.init_db(other)
        result = pg.list_unbound(other)
        assert all(section["count"] == 0 for section in result["categories"].values())
        assert result["resolutions"]["done"] == 0
        _legacy_item(other, "done")
        assert pg.list_unbound(other)["categories"]["legacy_done"]["count"] == 1
    finally:
        other.conn.close()
    assert pg.list_unbound(store)["categories"]["legacy_done"]["count"] >= 1
