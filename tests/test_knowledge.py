"""
Tests for knowledge event typing + evidence links.

Knowledge event types: decision, pattern-noted, lesson-learned, risk-accepted.
Evidence links attach item/event IDs to the payload for kctl to follow.
"""

import json

import pytest

from sprintctl import db
from sprintctl.cli import cli

KNOWLEDGE_TYPES = ("decision", "pattern-noted", "lesson-learned", "risk-accepted")


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


# ---------------------------------------------------------------------------
# DB layer — list_knowledge_candidates
# ---------------------------------------------------------------------------


class TestListKnowledgeCandidates:
    def test_returns_decision(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        eid = db.create_event(
            conn, active_sprint["id"], "agent",
            event_type="decision",
            work_item_id=iid,
            payload={"summary": "freeze usage contract v1"},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert any(r["id"] == eid for r in results)

    def test_returns_pattern_noted(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        eid = db.create_event(
            conn, active_sprint["id"], "agent",
            event_type="pattern-noted",
            work_item_id=iid,
            payload={"summary": "anchor-first naming"},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert any(r["id"] == eid for r in results)

    def test_returns_lesson_learned(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        eid = db.create_event(
            conn, active_sprint["id"], "agent",
            event_type="lesson-learned",
            work_item_id=iid,
            payload={"summary": "check path conflicts first"},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert any(r["id"] == eid for r in results)

    def test_returns_risk_accepted(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        eid = db.create_event(
            conn, active_sprint["id"], "agent",
            event_type="risk-accepted",
            work_item_id=iid,
            payload={"summary": "no ID validation — manual review catches it"},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert any(r["id"] == eid for r in results)

    def test_excludes_non_knowledge_types(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_event(
            conn, active_sprint["id"], "agent",
            event_type="update",
            work_item_id=iid,
            payload={"summary": "made progress"},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert results == []

    def test_ordered_by_created_at_asc(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        e1 = db.create_event(conn, active_sprint["id"], "agent", event_type="pattern-noted",
                              work_item_id=iid, payload={"summary": "first"})
        e2 = db.create_event(conn, active_sprint["id"], "agent", event_type="lesson-learned",
                              work_item_id=iid, payload={"summary": "second"})
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        ids = [r["id"] for r in results]
        assert ids.index(e1) < ids.index(e2)

    def test_payload_deserialized(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_event(conn, active_sprint["id"], "agent", event_type="pattern-noted",
                        work_item_id=iid, payload={"summary": "foo", "tags": ["bar"]})
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        payload = results[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["summary"] == "foo"


# ---------------------------------------------------------------------------
# item note with evidence links
# ---------------------------------------------------------------------------


class TestItemNoteEvidenceLinks:
    def test_evidence_item_id_stored_in_payload(self, conn, active_sprint):
        iid_src = _item(conn, active_sprint["id"], "Source item")
        iid_target = _item(conn, active_sprint["id"], "Pattern item")
        eid = db.create_event(
            conn, active_sprint["id"], "agent",
            event_type="pattern-noted",
            work_item_id=iid_target,
            payload={"summary": "good pattern", "evidence_item_id": iid_src},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        result = next(r for r in results if r["id"] == eid)
        assert result["payload"]["evidence_item_id"] == iid_src

    def test_evidence_event_id_stored_in_payload(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        source_eid = db.create_event(
            conn, active_sprint["id"], "agent", event_type="decision",
            work_item_id=iid, payload={"summary": "chose X"},
        )
        candidate_eid = db.create_event(
            conn, active_sprint["id"], "agent", event_type="pattern-noted",
            work_item_id=iid,
            payload={"summary": "pattern from decision", "evidence_event_id": source_eid},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        result = next(r for r in results if r["id"] == candidate_eid)
        assert result["payload"]["evidence_event_id"] == source_eid

    def test_both_evidence_fields_coexist(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        source_eid = db.create_event(conn, active_sprint["id"], "agent", event_type="decision",
                                     work_item_id=iid, payload={"summary": "x"})
        eid = db.create_event(
            conn, active_sprint["id"], "agent", event_type="lesson-learned",
            work_item_id=iid,
            payload={"summary": "lesson", "evidence_item_id": iid, "evidence_event_id": source_eid},
        )
        results = db.list_knowledge_candidates(conn, active_sprint["id"])
        r = next(x for x in results if x["id"] == eid)
        assert r["payload"]["evidence_item_id"] == iid
        assert r["payload"]["evidence_event_id"] == source_eid


# ---------------------------------------------------------------------------
# CLI: item note with --evidence-item-id / --evidence-event-id
# ---------------------------------------------------------------------------


class TestItemNoteCLIEvidence:
    def test_item_note_evidence_item_id(self, runner, conn, active_sprint, db_path):
        iid_src = _item(conn, active_sprint["id"], "Source")
        iid_target = _item(conn, active_sprint["id"], "Target")
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid_target),
            "--type", "pattern-noted",
            "--summary", "learned from source item",
            "--evidence-item-id", str(iid_src),
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        candidates = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert len(candidates) == 1
        assert candidates[0]["payload"]["evidence_item_id"] == iid_src

    def test_item_note_evidence_event_id(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        src_eid = db.create_event(conn, active_sprint["id"], "agent", event_type="decision",
                                  work_item_id=iid, payload={"summary": "prior decision"})
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "lesson-learned",
            "--summary", "lesson from that decision",
            "--evidence-event-id", str(src_eid),
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        candidates = db.list_knowledge_candidates(conn, active_sprint["id"])
        lesson = next(candidate for candidate in candidates if candidate["event_type"] == "lesson-learned")
        assert lesson["payload"]["evidence_event_id"] == src_eid

    def test_item_note_both_evidence_fields(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        src_eid = db.create_event(conn, active_sprint["id"], "agent", event_type="decision",
                                  work_item_id=iid, payload={"summary": "x"})
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "risk-accepted",
            "--summary", "risk with evidence",
            "--evidence-item-id", str(iid),
            "--evidence-event-id", str(src_eid),
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        candidates = db.list_knowledge_candidates(conn, active_sprint["id"])
        risk = next(candidate for candidate in candidates if candidate["event_type"] == "risk-accepted")
        assert risk["payload"]["evidence_item_id"] == iid
        assert risk["payload"]["evidence_event_id"] == src_eid

    def test_item_note_no_evidence_still_works(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        result = runner.invoke(cli, [
            "item", "note",
            "--id", str(iid),
            "--type", "pattern-noted",
            "--summary", "standalone pattern",
            "--actor", "agent",
        ])
        assert result.exit_code == 0, result.output
        candidates = db.list_knowledge_candidates(conn, active_sprint["id"])
        assert len(candidates) == 1
        assert "evidence_item_id" not in candidates[0]["payload"]


# ---------------------------------------------------------------------------
# CLI: event list --knowledge flag
# ---------------------------------------------------------------------------


class TestEventListKnowledgeFlag:
    def test_knowledge_flag_returns_only_knowledge_events(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.create_event(conn, active_sprint["id"], "agent", event_type="pattern-noted",
                        work_item_id=iid, payload={"summary": "p"})
        db.create_event(conn, active_sprint["id"], "agent", event_type="decision",
                        work_item_id=iid, payload={"summary": "d"})
        result = runner.invoke(cli, [
            "event", "list",
            "--sprint-id", str(active_sprint["id"]),
            "--knowledge",
        ])
        assert result.exit_code == 0, result.output
        assert "pattern-noted" in result.output
        assert "decision" in result.output

    def test_knowledge_flag_json_output(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.create_event(conn, active_sprint["id"], "agent", event_type="lesson-learned",
                        work_item_id=iid, payload={"summary": "s"})
        db.create_event(conn, active_sprint["id"], "agent", event_type="update",
                        work_item_id=iid, payload={"summary": "progress"})
        result = runner.invoke(cli, [
            "event", "list",
            "--sprint-id", str(active_sprint["id"]),
            "--knowledge", "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["event_type"] == "lesson-learned"

    def test_knowledge_flag_empty_when_no_candidates(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        db.create_event(conn, active_sprint["id"], "agent", event_type="update",
                        work_item_id=iid, payload={"summary": "progress"})
        result = runner.invoke(cli, [
            "event", "list",
            "--sprint-id", str(active_sprint["id"]),
            "--knowledge",
        ])
        assert result.exit_code == 0, result.output
        assert "No events found" in result.output

    def test_knowledge_flag_all_three_types(self, runner, conn, active_sprint, db_path):
        iid = _item(conn, active_sprint["id"])
        for ktype in KNOWLEDGE_TYPES:
            db.create_event(conn, active_sprint["id"], "agent", event_type=ktype,
                            work_item_id=iid, payload={"summary": ktype})
        result = runner.invoke(cli, [
            "event", "list",
            "--sprint-id", str(active_sprint["id"]),
            "--knowledge", "--json",
        ])
        data = json.loads(result.output)
        found_types = {e["event_type"] for e in data}
        assert found_types == set(KNOWLEDGE_TYPES)

    def test_knowledge_and_type_flags_are_mutually_exclusive(self, runner, conn, active_sprint, db_path):
        result = runner.invoke(cli, [
            "event", "list",
            "--sprint-id", str(active_sprint["id"]),
            "--knowledge", "--type", "pattern-noted",
        ])
        assert result.exit_code != 0
