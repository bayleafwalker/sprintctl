"""
Tests that sprint-taken-up and sprint-released payloads survive the
sqlite -> NDJSON export -> NDJSON parse round-trip unchanged.

No real postgres connection is needed: we export from sqlite, parse the NDJSON,
and compare event payloads to the originals.
"""

import io
import json

import pytest

from sprintctl import db, contracts
from sprintctl.pg import export_ndjson


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    c = db.get_connection(path)
    db.init_db(c)
    return c


@pytest.fixture
def active_sprint(conn):
    sid = db.create_sprint(conn, "S1", "Goal", "2026-01-01", "2026-03-31", "active")
    return db.get_sprint(conn, sid)


def _export_events(conn, repo_id="myrepo"):
    """Export all events to NDJSON and return the parsed event records."""
    buf = io.StringIO()
    export_ndjson(conn, repo_id, buf)
    lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    return [r["data"] for r in lines if r["table"] == "event"]


class TestTakeupPayloadRoundTrip:
    def test_sprint_taken_up_payload_survives_ndjson_export(self, conn, active_sprint):
        """sprint-taken-up canonical payload round-trips through NDJSON unchanged."""
        takeup_payload = contracts.canonicalize_event_payload("sprint-taken-up", {
            "summary": "sprint takeup",
            "detail": None,
            "actor_kind": "agent",
            "hostname": "testhost",
            "pid": 1234,
            "instance_id": "inst-abc",
            "runtime_session_id": "session-xyz",
        })
        eid = db.create_event(
            conn,
            active_sprint["id"],
            actor="agent-1",
            event_type="sprint-taken-up",
            source_type="actor",
            payload=takeup_payload,
        )

        events = _export_events(conn)
        exported = next((e for e in events if e["id"] == eid), None)
        assert exported is not None

        # Payload in NDJSON must be a dict (deserialized from JSON)
        exported_payload = exported["payload"]
        assert isinstance(exported_payload, dict), "Payload should be a dict after export"

        # Canonicalized payload must be unchanged
        re_canonicalized = contracts.canonicalize_event_payload(
            "sprint-taken-up", exported_payload
        )
        assert re_canonicalized == takeup_payload, (
            "sprint-taken-up payload changed after NDJSON round-trip"
        )

    def test_sprint_released_payload_survives_ndjson_export(self, conn, active_sprint):
        """sprint-released canonical payload round-trips through NDJSON unchanged."""
        release_payload = contracts.canonicalize_event_payload("sprint-released", {
            "summary": "sprint release",
            "detail": None,
            "actor_kind": "agent",
            "hostname": "testhost",
            "pid": 1234,
            "instance_id": "inst-abc",
            "runtime_session_id": "session-xyz",
            "reason": "session ended",
            "matched_takeup_event_id": 42,
        })
        eid = db.create_event(
            conn,
            active_sprint["id"],
            actor="agent-1",
            event_type="sprint-released",
            source_type="actor",
            payload=release_payload,
        )

        events = _export_events(conn)
        exported = next((e for e in events if e["id"] == eid), None)
        assert exported is not None

        exported_payload = exported["payload"]
        assert isinstance(exported_payload, dict)

        re_canonicalized = contracts.canonicalize_event_payload(
            "sprint-released", exported_payload
        )
        assert re_canonicalized == release_payload

    def test_active_takeup_ndjson_preserves_identity_fields(self, conn, active_sprint):
        """Instance and session identity fields survive NDJSON export."""
        eid = db.create_event(
            conn,
            active_sprint["id"],
            actor="agent-1",
            event_type="sprint-taken-up",
            source_type="actor",
            payload={
                "summary": "takeup",
                "detail": None,
                "actor_kind": "agent",
                "hostname": "mymachine",
                "pid": 9999,
                "instance_id": "uuid-12345678",
                "runtime_session_id": "sess-abcdef",
            },
        )

        events = _export_events(conn)
        exported = next((e for e in events if e["id"] == eid), None)
        assert exported is not None
        p = exported["payload"]
        assert p.get("instance_id") == "uuid-12345678"
        assert p.get("runtime_session_id") == "sess-abcdef"
        assert p.get("hostname") == "mymachine"
        assert p.get("pid") == 9999

    def test_multiple_takeup_and_release_events_all_exported(self, conn, active_sprint):
        """A sequence of takeup/release events all appear in NDJSON."""
        event_ids = []
        for i in range(3):
            eid = db.create_event(
                conn,
                active_sprint["id"],
                actor=f"agent-{i}",
                event_type="sprint-taken-up",
                source_type="actor",
                payload={"summary": "takeup", "detail": None, "actor_kind": "agent",
                         "hostname": "h", "pid": i, "instance_id": f"inst-{i}",
                         "runtime_session_id": None},
            )
            event_ids.append(eid)
            rid = db.create_event(
                conn,
                active_sprint["id"],
                actor=f"agent-{i}",
                event_type="sprint-released",
                source_type="actor",
                payload={"summary": "release", "detail": None, "actor_kind": "agent",
                         "hostname": "h", "pid": i, "instance_id": f"inst-{i}",
                         "runtime_session_id": None, "reason": None,
                         "matched_takeup_event_id": eid},
            )
            event_ids.append(rid)

        events = _export_events(conn)
        exported_ids = {e["id"] for e in events}
        for eid in event_ids:
            assert eid in exported_ids, f"Event #{eid} missing from NDJSON"

    def test_release_without_prior_takeup_exports_cleanly(self, conn, active_sprint):
        """An unmatched release event round-trips without error."""
        eid = db.create_event(
            conn,
            active_sprint["id"],
            actor="agent-orphan",
            event_type="sprint-released",
            source_type="actor",
            payload={
                "summary": "release",
                "detail": None,
                "actor_kind": "agent",
                "hostname": "h",
                "pid": 1,
                "instance_id": "orphan-inst",
                "runtime_session_id": None,
                "reason": "orphan",
                "matched_takeup_event_id": None,
            },
        )
        events = _export_events(conn)
        exported = next((e for e in events if e["id"] == eid), None)
        assert exported is not None
        assert exported["payload"]["reason"] == "orphan"
        assert exported["payload"]["matched_takeup_event_id"] is None
