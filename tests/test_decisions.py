"""Work decisions on the SQLite backend (schema 23), mirroring PostgreSQL 14.

A decision is the only writer of terminal status.  SQLite has no deferred
triggers, so its guards fire immediately; the write path always records the
decision before binding the item to it.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from sprintctl import contracts, db, decisions
from sprintctl import maintain as maint


def _item(conn, status="active"):
    sprint_id = db.create_sprint(conn, f"Decisions {uuid.uuid4().hex[:6]}", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "decisions")
    item_id = db.create_work_item(conn, sprint_id, track_id, "decide me")
    if status in {"active", "blocked"}:
        db.set_work_item_status(conn, item_id, "active")
    if status == "blocked":
        db.set_work_item_status(conn, item_id, "blocked")
    return sprint_id, track_id, item_id


def _refused(conn, statement, params, match):
    with pytest.raises(sqlite3.IntegrityError, match=match):
        conn.execute(statement, params)
    conn.rollback()


class TestGuards:
    def test_done_without_a_decision_is_refused(self, conn):
        _sprint_id, _track_id, item_id = _item(conn)
        _refused(
            conn,
            "UPDATE work_item SET status = 'done' WHERE id = ?",
            (item_id,),
            "done without a terminal decision",
        )
        assert db.get_work_item(conn, item_id)["status"] == "active"

    def test_work_decision_refuses_update_and_delete(self, conn):
        _sprint_id, _track_id, item_id = _item(conn)
        decision = db.record_decision(conn, item_id, "revise", actor="reviewer")
        _refused(
            conn,
            "UPDATE work_decision SET rationale = 'rewritten' WHERE id = ?",
            (decision["id"],),
            "append-only",
        )
        _refused(conn, "DELETE FROM work_decision WHERE id = ?", (decision["id"],), "append-only")

    def test_legacy_is_immutable_and_never_set_on_insert(self, conn):
        sprint_id, track_id, item_id = _item(conn)
        _refused(
            conn,
            "UPDATE work_item SET legacy = 1 WHERE id = ?",
            (item_id,),
            "legacy is immutable",
        )
        _refused(
            conn,
            "INSERT INTO work_item (sprint_id, track_id, title, legacy, aggregate_uuid) "
            "VALUES (?, ?, 'forged', 1, ?)",
            (sprint_id, track_id, str(uuid.uuid4())),
            "legacy is set only by the schema 23 migration",
        )
        assert db.get_work_item(conn, item_id)["legacy"] is False

    def test_done_never_transitions_back(self, conn):
        _sprint_id, _track_id, item_id = _item(conn)
        db.set_work_item_status(conn, item_id, "done", actor="closer")
        _refused(
            conn,
            "UPDATE work_item SET status = 'pending' WHERE id = ?",
            (item_id,),
            # Two guards refuse this row; SQLite does not order them.
            "terminal",
        )

    def test_resolution_must_match_the_decision_kind(self, conn):
        _sprint_id, _track_id, item_id = _item(conn)
        decision_id = conn.execute(
            "INSERT INTO work_decision (kind, work_item_id, actor) VALUES ('reject', ?, 'forger')",
            (item_id,),
        ).lastrowid
        _refused(
            conn,
            "UPDATE work_item SET status = 'done', resolution = 'accepted', "
            "terminal_decision_id = ? WHERE id = ?",
            (decision_id, item_id),
            "resolution must match",
        )


class TestWritePath:
    def test_set_status_done_is_an_accept_decision(self, conn):
        _sprint_id, _track_id, item_id = _item(conn)
        db.set_work_item_status(conn, item_id, "done", actor="closer")
        item = db.get_work_item(conn, item_id)
        [decision] = db.list_decisions(conn, item_id)
        assert (decision["kind"], decision["actor"]) == ("accept", "closer")
        assert (item["status"], item["resolution"]) == ("done", "accepted")
        assert item["terminal_decision_id"] == decision["id"]

    def test_record_decision_closes_with_the_mapped_resolution(self, conn):
        for kind, resolution in decisions.TERMINAL_RESOLUTIONS.items():
            sprint_id, track_id, item_id = _item(conn)
            other = db.create_work_item(conn, sprint_id, track_id, "successor")
            db.record_decision(
                conn,
                item_id,
                kind,
                actor="owner",
                evidence_digests=["d" * 64],
                superseded_by_item_id=other if kind == "supersede" else None,
            )
            item = db.get_work_item(conn, item_id)
            assert (item["status"], item["resolution"]) == ("done", resolution)

    def test_revise_leaves_the_item_open(self, conn):
        _sprint_id, _track_id, item_id = _item(conn)
        db.record_decision(conn, item_id, "revise", actor="reviewer")
        item = db.get_work_item(conn, item_id)
        assert (item["status"], item["resolution"]) == ("active", None)

    def test_terminal_item_and_invalid_arguments_are_refused(self, conn):
        _sprint_id, _track_id, item_id = _item(conn, status="pending")
        with pytest.raises(db.InvalidTransition, match="requires an active item"):
            db.record_decision(conn, item_id, "accept", actor="owner")
        with pytest.raises(ValueError, match="must name the superseding item"):
            db.record_decision(conn, item_id, "supersede", actor="owner")
        with pytest.raises(ValueError, match="lowercase hexadecimal"):
            db.record_decision(conn, item_id, "reject", actor="owner", evidence_digests=["X"])
        db.record_decision(conn, item_id, "withdraw", actor="owner")
        with pytest.raises(db.InvalidTransition, match="terminal"):
            db.record_decision(conn, item_id, "revise", actor="owner")
        assert [d["kind"] for d in db.list_decisions(conn, item_id)] == ["withdraw"]

    def test_carryover_supersedes_with_the_new_item(self, conn):
        from_sprint, _track_id, item_id = _item(conn, status="blocked")
        to_sprint = db.create_sprint(conn, "Carry target", status="active")
        [new_item] = maint.carryover(conn, from_sprint, to_sprint)
        [decision] = db.list_decisions(conn, item_id)
        assert decision["kind"] == "supersede"
        assert decision["superseded_by_item_id"] == new_item["id"]
        original = db.get_work_item(conn, item_id)
        assert (original["status"], original["resolution"]) == ("done", "superseded")


class TestContracts:
    @pytest.mark.parametrize(
        "record_type", ["capability-receipt.accept", "capability-receipt.accepted"]
    )
    def test_retired_receipt_record_types_are_refused(self, record_type):
        with pytest.raises(ValueError, match="not classified"):
            contracts.record_class_for_type(record_type)

    @pytest.mark.parametrize(
        "event_type",
        [
            "capability-receipt-drafted",
            "capability-receipt-drafted-imported",
            "capability-receipt.accept",
            "capability-receipt.anything",
        ],
    )
    def test_retired_receipt_event_types_are_refused(self, conn, event_type):
        sprint_id, _track_id, _item_id = _item(conn)
        with pytest.raises(ValueError, match="capability receipts were retired"):
            db.create_event(conn, sprint_id, "agent", event_type, payload={})

    def test_decision_record_payload_contract(self):
        canonical = contracts._canonical_authority_payload(
            "decision.record",
            {"kind": "reject", "evidence_digests": ["e" * 64, "e" * 64]},
        )
        assert canonical == {"kind": "reject", "rationale": "", "evidence_digests": ["e" * 64]}
        with pytest.raises(ValueError, match="superseded_by_aggregate_uuid"):
            contracts._canonical_authority_payload("decision.record", {"kind": "supersede"})
        with pytest.raises(ValueError, match="decision kind"):
            contracts._canonical_authority_payload("decision.record", {"kind": "approve"})


class TestMigration23:
    def test_v22_rows_become_legacy_and_drafted_receipts_become_evidence(
        self, db_path, monkeypatch
    ):
        conn = db.get_connection(db_path)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(db, "_migration_23", lambda _conn: None)
                db.init_db(conn)
            conn.execute("UPDATE schema_version SET version = 22")
            sprint_id = conn.execute(
                "INSERT INTO sprint (name, start_date, end_date, status, aggregate_uuid) "
                "VALUES ('old', '2026-01-01', '2026-01-31', 'closed', ?)",
                (str(uuid.uuid4()),),
            ).lastrowid
            track_id = conn.execute(
                "INSERT INTO track (sprint_id, name) VALUES (?, 't')", (sprint_id,)
            ).lastrowid
            for title, status in (("finished", "done"), ("open", "pending")):
                conn.execute(
                    "INSERT INTO work_item (sprint_id, track_id, title, status, aggregate_uuid) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sprint_id, track_id, title, status, str(uuid.uuid4())),
                )
            for event_type in ["capability-receipt-drafted"] * 2 + ["note"]:
                conn.execute(
                    "INSERT INTO event (sprint_id, source_type, actor, event_type, payload) "
                    "VALUES (?, 'actor', 'agent', ?, '{}')",
                    (sprint_id, event_type),
                )
            conn.commit()

            db.init_db(conn)
            db.init_db(conn)  # a second run is a no-op

            items = {
                row["title"]: dict(row)
                for row in conn.execute("SELECT * FROM work_item")
            }
            assert items["finished"]["legacy"] == 1
            assert items["finished"]["terminal_decision_id"] is None
            assert items["open"]["legacy"] == 1
            assert conn.execute("SELECT count(*) FROM work_decision").fetchone()[0] == 0
            assert [
                tuple(row)
                for row in conn.execute(
                    "SELECT kind, count(*) FROM work_legacy_evidence GROUP BY kind"
                )
            ] == [("capability-receipt-drafted", 2)]
            new_id = db.create_work_item(conn, sprint_id, track_id, "after")
            assert db.get_work_item(conn, new_id)["legacy"] is False
        finally:
            conn.close()


def _legacy_item(conn, status):
    """Insert a row as migration 23 leaves a pre-decision item."""
    sprint_id = db.create_sprint(conn, f"Legacy {uuid.uuid4().hex[:6]}", status="active")
    track_id = db.get_or_create_track(conn, sprint_id, "legacy")
    with db.legacy_import_gate(conn):
        item_id = conn.execute(
            "INSERT INTO work_item (sprint_id, track_id, title, status, legacy, aggregate_uuid) "
            "VALUES (?, ?, 'legacy', ?, 1, ?)",
            (sprint_id, track_id, status, str(uuid.uuid4())),
        ).lastrowid
    conn.commit()
    return sprint_id, track_id, item_id


# The statements a 0.3.7 runtime issues to make an item done.
_OLD_RUNTIME_DONE_WRITES = {
    "set_work_item_status": (
        "UPDATE work_item SET status = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        lambda item_id: ("done", item_id),
    ),
    "force_item_done_for_carryover (maintain.carryover)": (
        "UPDATE work_item SET status = 'done', "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        lambda item_id: (item_id,),
    ),
}


class TestLegacyOpenItems:
    @pytest.mark.parametrize("writer", sorted(_OLD_RUNTIME_DONE_WRITES))
    @pytest.mark.parametrize("status", ["pending", "active", "blocked"])
    def test_old_runtime_done_writes_fail_loudly(self, conn, writer, status):
        _sprint_id, _track_id, item_id = _legacy_item(conn, status)
        statement, params = _OLD_RUNTIME_DONE_WRITES[writer]
        _refused(conn, statement, params(item_id), "cannot become done without a terminal decision")
        item = db.get_work_item(conn, item_id)
        assert (item["status"], item["legacy"]) == (status, True)

    def test_open_legacy_item_closes_through_a_decision(self, conn):
        _sprint_id, _track_id, item_id = _legacy_item(conn, "active")
        db.set_work_item_status(conn, item_id, "done", actor="closer")
        item = db.get_work_item(conn, item_id)
        [decision] = db.list_decisions(conn, item_id)
        assert (item["status"], item["legacy"], item["resolution"]) == ("done", True, "accepted")
        assert item["terminal_decision_id"] == decision["id"]

    def test_done_legacy_item_stays_undecided(self, conn):
        _sprint_id, _track_id, item_id = _legacy_item(conn, "done")
        with pytest.raises(db.InvalidTransition, match="terminal"):
            db.record_decision(conn, item_id, "reject", actor="owner")


def _rows(conn, table, order="id"):
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
    columns = [column[0] for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


class TestRecoveryWithDecisions:
    def test_snapshot_with_decisions_and_evidence_restores(self, conn, tmp_path):
        sprint_id, track_id, legacy_open = _legacy_item(conn, "active")
        _s, _t, legacy_done = _legacy_item(conn, "done")
        db.set_work_item_status(conn, legacy_open, "done", actor="closer")
        fresh = db.create_work_item(conn, sprint_id, track_id, "fresh")
        successor = db.create_work_item(conn, sprint_id, track_id, "successor")
        db.record_decision(conn, fresh, "supersede", actor="p", superseded_by_item_id=successor)
        db.record_decision(conn, successor, "revise", actor="r")
        event_id = conn.execute(
            "INSERT INTO event (sprint_id, source_type, actor, event_type, payload) "
            "VALUES (?, 'actor', 'agent', 'capability-receipt-drafted', ?)",
            (sprint_id, '{"b": 1,  "a": "x"}'),
        ).lastrowid
        conn.execute(
            "INSERT INTO work_legacy_evidence (event_id, sprint_id, payload_sha256, kind) "
            "VALUES (?, ?, ?, 'capability-receipt-drafted')",
            (event_id, sprint_id, decisions.legacy_evidence_digest('{"b": 1,  "a": "x"}')),
        )
        conn.commit()
        snapshot = {
            table: _rows(conn, table, "event_id" if table == "work_legacy_evidence" else "id")
            for table in db._RECOVERY_TABLE_ORDER
        }

        target = db.get_connection(tmp_path / "recovered.db")
        try:
            db.init_db(target)
            counts = db.write_recovery_snapshot(target, snapshot)
            assert counts["work_decision"] == 3
            assert counts["work_legacy_evidence"] == 1
            assert target.execute("PRAGMA foreign_key_check").fetchall() == []
            for table in ("work_item", "work_decision", "work_legacy_evidence"):
                order = "event_id" if table == "work_legacy_evidence" else "id"
                assert _rows(target, table, order) == snapshot[table]
            assert db.get_work_item(target, legacy_done)["terminal_decision_id"] is None
        finally:
            target.close()

    def test_evidence_digest_is_canonical_json(self):
        spaced = '{"b": 1,  "a": "x"}'
        assert decisions.legacy_evidence_digest(spaced) == decisions.legacy_evidence_digest(
            {"a": "x", "b": 1}
        )
        import hashlib

        assert decisions.legacy_evidence_digest({"a": "x", "b": 1}) == hashlib.sha256(
            b'{"a":"x","b":1}'
        ).hexdigest()
