"""S3 PR5 on the SQLite backend (schema 25).

The legacy re-mark and its triggers, open-only sweeps, the refusal of
decision-like event types, resolution metrics and the ``item unbound`` CLI.
The served contract for both backends lives in
``tests/test_served_decisions.py``; PostgreSQL's triggers and migration in
``tests/pg/test_legacy_remark.py``.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from sprintctl import calc, contracts, db
from sprintctl.cli import cli
from tests.test_decisions import _legacy_item, _refused

EVIDENCE = "cd" * 32


def _active_item(conn, sprint_id=None, track_id=None):
    if sprint_id is None:
        sprint_id = db.create_sprint(conn, "Remark", status="active")
        track_id = db.get_or_create_track(conn, sprint_id, "t")
    item_id = db.create_work_item(conn, sprint_id, track_id, "open")
    db.set_work_item_status(conn, item_id, "active")
    return item_id


def _insert_decision(conn, item_id, *, kind="reject", rationale="why", evidence=(EVIDENCE,),
                     legacy_source=None):
    return conn.execute(
        "INSERT INTO work_decision (kind, work_item_id, evidence_digests, rationale, actor, "
        "legacy_source) VALUES (?, ?, ?, ?, 'owner', ?)",
        (kind, item_id, json.dumps(list(evidence)), rationale, legacy_source),
    ).lastrowid


class TestRemark:
    def test_record_decision_remarks_a_legacy_done_item(self, conn):
        _s, _t, item_id = _legacy_item(conn, "done")
        before = db.get_work_item(conn, item_id)
        decision = db.record_decision(
            conn, item_id, "withdraw", actor="owner", rationale="dropped",
            evidence_digests=[EVIDENCE],
        )
        item = db.get_work_item(conn, item_id)
        assert (item["status"], item["resolution"], item["terminal_decision_id"]) == (
            "done", "withdrawn", decision["id"]
        )
        assert item["updated_at"] == before["updated_at"]
        with pytest.raises(db.InvalidTransition, match="terminal"):
            db.record_decision(conn, item_id, "accept", actor="owner", rationale="x",
                               evidence_digests=[EVIDENCE])

    @pytest.mark.parametrize(
        "decision",
        [
            {"rationale": ""},
            {"rationale": "   "},
            {"evidence": ()},
            {"legacy_source": "capability-receipt"},
        ],
    )
    def test_the_trigger_refuses_an_unauthored_or_unevidenced_remark(self, conn, decision):
        _s, _t, item_id = _legacy_item(conn, "done")
        decision_id = _insert_decision(conn, item_id, **decision)
        _refused(
            conn,
            "UPDATE work_item SET resolution = 'rejected', terminal_decision_id = ? WHERE id = ?",
            (decision_id, item_id),
            "legacy re-mark",
        )
        assert db.get_work_item(conn, item_id)["terminal_decision_id"] is None

    def test_the_trigger_refuses_a_decision_about_another_item(self, conn):
        _s, _t, item_id = _legacy_item(conn, "done")
        _s2, _t2, other = _legacy_item(conn, "active")
        decision_id = _insert_decision(conn, other)
        _refused(
            conn,
            "UPDATE work_item SET resolution = 'rejected', terminal_decision_id = ? WHERE id = ?",
            (decision_id, item_id),
            "legacy re-mark",
        )

    def test_the_trigger_admits_a_remark_and_keeps_it_one_shot(self, conn):
        _s, _t, item_id = _legacy_item(conn, "done")
        first = _insert_decision(conn, item_id)
        conn.execute(
            "UPDATE work_item SET resolution = 'rejected', terminal_decision_id = ? WHERE id = ?",
            (first, item_id),
        )
        conn.commit()
        second = _insert_decision(conn, item_id, kind="accept")
        _refused(
            conn,
            "UPDATE work_item SET resolution = 'accepted', terminal_decision_id = ? WHERE id = ?",
            (second, item_id),
            "immutable",
        )
        _refused(
            conn,
            "UPDATE work_item SET resolution = NULL, terminal_decision_id = NULL WHERE id = ?",
            (item_id,),
            "immutable",
        )

    def test_a_decided_done_item_is_still_immutable(self, conn):
        item_id = _active_item(conn)
        db.set_work_item_status(conn, item_id, "done")
        decision_id = _insert_decision(conn, item_id)
        _refused(
            conn,
            "UPDATE work_item SET resolution = 'rejected', terminal_decision_id = ? WHERE id = ?",
            (decision_id, item_id),
            "immutable",
        )


class TestMigration25:
    def test_migrating_24_to_25_admits_the_remark(self, db_path):
        conn = db.get_connection(db_path)
        foreign_keys_off = {5, 14, 15, 22}
        for version in range(1, 25):
            db._run_migration(
                conn, version, getattr(db, f"_migration_{version}"),
                foreign_keys_off=version in foreign_keys_off,
            )
        _s, _t, item_id = _legacy_item(conn, "done")
        with pytest.raises(sqlite3.IntegrityError, match="takes no decision"):
            db.record_decision(conn, item_id, "reject", actor="owner", rationale="why",
                               evidence_digests=[EVIDENCE])
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 24

        db.init_db(conn)
        db.init_db(conn)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 25
        triggers = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert "work_item_legacy_done_remark" in triggers
        assert "work_item_legacy_done_takes_no_decision" not in triggers
        db.record_decision(conn, item_id, "reject", actor="owner", rationale="why",
                           evidence_digests=[EVIDENCE])
        assert db.get_work_item(conn, item_id)["resolution"] == "rejected"
        conn.close()


class TestSweepsTouchOnlyOpenItems:
    def test_a_stale_reservation_of_a_done_item_is_left_alone(self, conn):
        sprint_id = db.create_sprint(conn, "Sweep", status="active")
        track_id = db.get_or_create_track(conn, sprint_id, "t")
        open_item = _active_item(conn, sprint_id, track_id)
        done_item = _active_item(conn, sprint_id, track_id)
        open_row = db.reserve(conn, open_item, actor="a", session_id="s1")
        done_row = db.reserve(conn, done_item, actor="a", session_id="s2")
        db.record_decision(conn, done_item, "accept", actor="owner")
        conn.execute("UPDATE reservation SET last_activity_at = '2020-01-01T00:00:00Z'")
        conn.commit()

        swept = db.sweep_stale_reservations(conn, now="2026-01-01T00:00:00Z")

        assert [row["id"] for row in swept] == [open_row["id"]]
        assert db.get_reservation(conn, open_row["id"])["state"] == "interrupted"
        assert db.get_reservation(conn, done_row["id"])["state"] == "active"
        events = [
            e for e in db.list_events(conn, sprint_id)
            if e["event_type"] == "reservation.interrupted"
        ]
        assert [e["work_item_id"] for e in events] == [open_item]


class TestDecisionLikeEvents:
    @pytest.mark.parametrize(
        "event_type",
        ["item-decided", "item.done", "decision.record", "reject", "accepted", "item-closed"],
    )
    def test_the_generic_writer_refuses_them(self, conn, event_type):
        item_id = _active_item(conn)
        sprint_id = db.get_work_item(conn, item_id)["sprint_id"]
        with pytest.raises(contracts.DecisionLikeEventType, match="reserved"):
            db.create_event(conn, sprint_id, "agent", event_type, work_item_id=item_id)

    def test_knowledge_decision_notes_stay_open(self, conn):
        item_id = _active_item(conn)
        sprint_id = db.get_work_item(conn, item_id)["sprint_id"]
        assert db.create_event(
            conn, sprint_id, "agent", "decision", work_item_id=item_id,
            payload={"summary": "pin the contract"},
        )

    def test_event_add_cli_refuses_them(self, runner, conn):
        item_id = _active_item(conn)
        sprint_id = db.get_work_item(conn, item_id)["sprint_id"]
        result = runner.invoke(
            cli,
            ["event", "add", "--sprint-id", str(sprint_id), "--type", "item.done",
             "--actor", "agent"],
        )
        assert result.exit_code != 0
        assert "decision-like" in result.output
        assert db.get_work_item(conn, item_id)["status"] == "active"


class TestResolutionMetrics:
    def test_resolution_counts_keep_legacy_done_apart(self):
        items = [
            {"status": "done", "resolution": "accepted", "legacy": False},
            {"status": "done", "resolution": "rejected", "legacy": True},
            {"status": "done", "resolution": None, "legacy": True},
            {"status": "active", "resolution": None, "legacy": True},
        ]
        assert calc.resolution_counts(items) == {
            "accepted": 1, "rejected": 1, "withdrawn": 0, "superseded": 0,
            "decided_done": 2, "legacy_done": 1, "done": 3,
        }
        health = calc.track_health(items)
        assert health["counts"]["done"] == 3
        assert health["resolutions"]["legacy_done"] == 1

    def test_sprint_show_detail_reports_resolutions(self, runner, conn):
        sprint_id, _t, _legacy = _legacy_item(conn, "done")
        track_id = db.get_or_create_track(conn, sprint_id, "legacy")
        item_id = _active_item(conn, sprint_id, track_id)
        db.record_decision(conn, item_id, "reject", actor="owner")
        result = runner.invoke(cli, ["sprint", "show", "--id", str(sprint_id), "--detail"])
        assert result.exit_code == 0, result.output
        assert "done by resolution: 1 rejected, 1 legacy_done" in result.output
        as_json = runner.invoke(
            cli, ["sprint", "show", "--id", str(sprint_id), "--detail", "--json"]
        )
        payload = json.loads(as_json.output)
        health = payload["detail"]["track_health"]["legacy"]
        assert health["resolutions"]["decided_done"] == 1


class TestItemUnboundCli:
    def test_lists_the_categories_locally(self, runner, conn):
        sprint_id, track_id, legacy = _legacy_item(conn, "done")
        closed = _active_item(conn, sprint_id, track_id)
        db.record_decision(conn, closed, "accept", actor="owner")
        picked = _active_item(conn, sprint_id, track_id)
        db.reserve(conn, picked, actor="a", session_id="s")

        result = runner.invoke(cli, ["item", "unbound", "--sprint-id", str(sprint_id)])
        assert result.exit_code == 0, result.output
        assert "Done: 2 (1 decided: 1 accepted" in result.output
        assert "1 legacy)" in result.output
        assert f"#{legacy} [done] legacy" in result.output
        assert f"#{closed} [done]" in result.output
        assert f"#{picked} [active]" in result.output

        as_json = runner.invoke(
            cli, ["item", "unbound", "--category", "legacy_done", "--json"]
        )
        payload = json.loads(as_json.output)
        assert list(payload["categories"]) == ["legacy_done"]
        assert [i["id"] for i in payload["categories"]["legacy_done"]["items"]] == [legacy]

    def test_unknown_sprint_exits_nonzero(self, runner, conn):
        result = runner.invoke(cli, ["item", "unbound", "--sprint-id", "999999"])
        assert result.exit_code == 1


@pytest.mark.skipif(sys.version_info < (3, 12), reason="served mode requires Python 3.12+")
def test_item_unbound_is_a_served_catalog_command():
    from sprintctl import served_routes

    [route] = served_routes.routes_for("item.unbound")
    assert route.operation == "work.read.unbound"
    assert served_routes.disposition_for("item unbound") == "catalog"
