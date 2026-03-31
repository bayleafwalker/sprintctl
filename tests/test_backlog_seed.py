"""
Tests for Phase 7: kctl synthesis + backlog seeding.

`sprint backlog-seed` reads knowledge candidate events from a source sprint
and creates pending work items in a target backlog sprint.
"""

import json

import pytest

from sprintctl import db
from sprintctl.cli import cli


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


def _knowledge_event(conn, sprint_id, work_item_id, event_type="pattern-noted", summary="candidate"):
    return db.create_event(
        conn, sprint_id, "agent",
        event_type=event_type,
        work_item_id=work_item_id,
        payload={"summary": summary},
    )


@pytest.fixture
def source_sprint(conn):
    sid = db.create_sprint(conn, "S0", "Source sprint", "2026-01-01", "2026-01-31", "active")
    return db.get_sprint(conn, sid)


@pytest.fixture
def backlog_sprint(conn):
    sid = db.create_sprint(conn, "BL", "Backlog", status="active", kind="backlog")
    return db.get_sprint(conn, sid)


# ---------------------------------------------------------------------------
# DB layer — backlog_seed_from_candidates
# ---------------------------------------------------------------------------


class TestBacklogSeedDB:
    def test_creates_items_for_each_candidate(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"], "Source item")
        _knowledge_event(conn, source_sprint["id"], iid, summary="pattern A")
        _knowledge_event(conn, source_sprint["id"], iid, "lesson-learned", summary="lesson B")

        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        assert len(seeded) == 2

    def test_seeded_items_are_pending(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="foo")

        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        new_item = db.get_work_item(conn, seeded[0]["id"])
        assert new_item["status"] == "pending"

    def test_seeded_items_title_from_candidate_summary(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="anchor-first naming pattern")

        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        new_item = db.get_work_item(conn, seeded[0]["id"])
        assert "anchor-first naming pattern" in new_item["title"]

    def test_seeded_items_land_in_target_sprint(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="p")

        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        new_item = db.get_work_item(conn, seeded[0]["id"])
        assert new_item["sprint_id"] == backlog_sprint["id"]

    def test_seeded_item_notes_source_sprint_and_item(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"], "Original task")
        _knowledge_event(conn, source_sprint["id"], iid, summary="p")

        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        events = db.list_events(conn, backlog_sprint["id"])
        seed_events = [e for e in events if e["event_type"] == "backlog-seeded"]
        assert len(seed_events) == 1
        payload = json.loads(seed_events[0]["payload"])
        assert payload["source_sprint_id"] == source_sprint["id"]
        assert payload["source_item_id"] == iid

    def test_no_candidates_returns_empty(self, conn, source_sprint, backlog_sprint):
        _item(conn, source_sprint["id"])  # no knowledge events
        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        assert seeded == []

    def test_invalid_source_sprint_raises(self, conn, backlog_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.backlog_seed_from_candidates(conn, 9999, backlog_sprint["id"])

    def test_invalid_target_sprint_raises(self, conn, source_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.backlog_seed_from_candidates(conn, source_sprint["id"], 9999)

    def test_seeded_items_go_to_knowledge_track(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="p")

        seeded = db.backlog_seed_from_candidates(
            conn, source_sprint["id"], backlog_sprint["id"]
        )
        new_item = db.get_work_item(conn, seeded[0]["id"])
        track = db.get_track(conn, new_item["track_id"])
        assert track["name"] == "knowledge"

    def test_idempotent_seed_does_not_duplicate(self, conn, source_sprint, backlog_sprint):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="p")

        db.backlog_seed_from_candidates(conn, source_sprint["id"], backlog_sprint["id"])
        seeded2 = db.backlog_seed_from_candidates(conn, source_sprint["id"], backlog_sprint["id"])
        assert seeded2 == []  # already seeded, nothing new


# ---------------------------------------------------------------------------
# CLI: sprint backlog-seed
# ---------------------------------------------------------------------------


class TestBacklogSeedCLI:
    def test_basic_seed(self, runner, conn, source_sprint, backlog_sprint, db_path):
        iid = _item(conn, source_sprint["id"], "Source work")
        _knowledge_event(conn, source_sprint["id"], iid, summary="important pattern")

        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", str(backlog_sprint["id"]),
        ])
        assert result.exit_code == 0, result.output
        assert "1" in result.output  # seeded 1 item
        items = db.list_work_items(conn, sprint_id=backlog_sprint["id"])
        assert len(items) == 1
        assert "important pattern" in items[0]["title"]

    def test_seed_multiple_candidates(self, runner, conn, source_sprint, backlog_sprint, db_path):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="pattern A")
        _knowledge_event(conn, source_sprint["id"], iid, "lesson-learned", summary="lesson B")
        _knowledge_event(conn, source_sprint["id"], iid, "risk-accepted", summary="risk C")

        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", str(backlog_sprint["id"]),
        ])
        assert result.exit_code == 0, result.output
        items = db.list_work_items(conn, sprint_id=backlog_sprint["id"])
        assert len(items) == 3

    def test_seed_no_candidates_reports_zero(self, runner, conn, source_sprint, backlog_sprint, db_path):
        _item(conn, source_sprint["id"])
        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", str(backlog_sprint["id"]),
        ])
        assert result.exit_code == 0, result.output
        assert "0" in result.output or "No" in result.output

    def test_seed_invalid_source_sprint_fails(self, runner, backlog_sprint, db_path):
        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", "9999",
            "--to-sprint-id", str(backlog_sprint["id"]),
        ])
        assert result.exit_code == 1

    def test_seed_invalid_target_sprint_fails(self, runner, source_sprint, db_path):
        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", "9999",
        ])
        assert result.exit_code == 1

    def test_seed_json_output(self, runner, conn, source_sprint, backlog_sprint, db_path):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="pattern")

        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", str(backlog_sprint["id"]),
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert "id" in data[0]
        assert "title" in data[0]

    def test_seed_idempotent_second_run_empty(self, runner, conn, source_sprint, backlog_sprint, db_path):
        iid = _item(conn, source_sprint["id"])
        _knowledge_event(conn, source_sprint["id"], iid, summary="p")

        runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", str(backlog_sprint["id"]),
        ])
        result = runner.invoke(cli, [
            "sprint", "backlog-seed",
            "--from-sprint-id", str(source_sprint["id"]),
            "--to-sprint-id", str(backlog_sprint["id"]),
        ])
        assert result.exit_code == 0, result.output
        assert "0" in result.output or "No" in result.output
        items = db.list_work_items(conn, sprint_id=backlog_sprint["id"])
        assert len(items) == 1  # still only 1, not 2
