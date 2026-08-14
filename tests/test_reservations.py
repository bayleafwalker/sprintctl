from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import db


def _item(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "reservations")
    return db.create_work_item(conn, active_sprint["id"], track, "advisory work")


def test_reserve_conflict_override_and_audit(conn, active_sprint):
    item = _item(conn, active_sprint)
    first = db.reserve(conn, item, actor="one", session_id="s1")
    with pytest.raises(db.ReservationConflict, match="use --override"):
        db.reserve(conn, item, actor="two", session_id="s2")

    replacement = db.reserve(conn, item, actor="two", session_id="s2", override=True,
                             correlation_ref="actionq:execution:42")
    assert db.get_reservation(conn, first["id"])["state"] == "interrupted"
    assert replacement["correlation_ref"] == "actionq:execution:42"
    events = db.list_events(conn, active_sprint["id"])
    assert {event["event_type"] for event in events} >= {"reservation.reserved", "reservation.interrupted"}


def test_touch_requires_same_session_reassign_and_release_are_proof_free(conn, active_sprint):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    with pytest.raises(ValueError, match="another session"):
        db.touch_reservation(conn, row["id"], session_id="other")
    reassigned = db.reassign_reservation(conn, row["id"], actor="two", session_id="s2")
    assert reassigned["actor"] == "two"
    assert db.touch_reservation(conn, row["id"], session_id="s2")["state"] == "active"
    assert db.release_reservation(conn, row["id"], actor="operator")["state"] == "released"


def test_four_hour_stale_display_and_seven_day_sweep(conn, active_sprint):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    then = datetime.now(timezone.utc) - timedelta(hours=4, seconds=1)
    conn.execute("UPDATE reservation SET last_activity_at = ? WHERE id = ?", (then.strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]))
    conn.commit()
    assert db.get_reservation(conn, row["id"])["stale"] is True
    swept = db.sweep_stale_reservations(conn, now=(datetime.now(timezone.utc) + timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert [value["id"] for value in swept] == [row["id"]]
    assert db.get_reservation(conn, row["id"])["state"] == "interrupted"
