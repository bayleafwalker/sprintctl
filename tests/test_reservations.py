from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import db
from sprintctl import reservation as _reservation
from sprintctl import reservation_policy
from sprintctl.work_application import WorkApplication
from types import SimpleNamespace


def _item(conn, active_sprint, title="advisory work"):
    track = db.get_or_create_track(conn, active_sprint["id"], "reservations")
    return db.create_work_item(conn, active_sprint["id"], track, title)


def test_overlapping_execution_reservations_both_commit_and_are_reported(conn, active_sprint):
    """Overlap is the tested behavior, not an error path.

    A reservation is a detector, not a lease: refusing the second session
    would not prevent it from working, only from being recorded, which is the
    one outcome a coordination ledger must not produce.
    """
    item = _item(conn, active_sprint)
    first = db.reserve(conn, item, actor="one", session_id="s1")
    second = db.reserve(conn, item, actor="two", session_id="s2")

    assert first["conflict"] is False
    assert first["conflict_severity"] == "none"
    assert second["conflict"] is True
    assert second["conflict_severity"] == "warning"
    assert [row["id"] for row in second["conflicting_reservations"]] == [first["id"]]

    active = db.list_reservations(conn, item, active_only=True)
    assert {row["id"] for row in active} == {first["id"], second["id"]}
    assert db.get_reservation(conn, first["id"])["state"] == "active"


def test_non_execution_overlap_is_informational(conn, active_sprint):
    item = _item(conn, active_sprint)
    db.reserve(conn, item, actor="one", session_id="s1")
    reviewer = db.reserve(conn, item, actor="two", session_id="s2", role="verification")
    observer = db.reserve(conn, item, actor="three", session_id="s3", role="observation")

    assert reviewer["conflict_severity"] == "informational"
    assert observer["conflict_severity"] == "informational"
    assert len(observer["conflicting_reservations"]) == 2


def test_interrupt_existing_is_the_only_way_to_displace_another_session(conn, active_sprint):
    item = _item(conn, active_sprint)
    first = db.reserve(conn, item, actor="one", session_id="s1")
    reviewer = db.reserve(conn, item, actor="reviewer", session_id="s-review", role="verification")

    replacement = db.reserve(conn, item, actor="two", session_id="s2", interrupt_existing=True,
                             correlation_ref="actionq:execution:42")

    displaced = db.get_reservation(conn, first["id"])
    assert displaced["state"] == "interrupted"
    assert displaced["interruption_reason"] == "interrupted by two (s2)"
    # A takeover displaces execution only: it is not a licence to clear the
    # item of everybody else's coordination signals.
    assert db.get_reservation(conn, reviewer["id"])["state"] == "active"
    assert replacement["correlation_ref"] == "actionq:execution:42"
    assert [row["id"] for row in replacement["conflicting_reservations"]] == [reviewer["id"]]

    events = db.list_events(conn, active_sprint["id"])
    assert {event["event_type"] for event in events} >= {"reservation.reserved", "reservation.interrupted"}


def test_roles_are_the_work_relationship_and_legacy_names_fold_in(conn, active_sprint):
    item = _item(conn, active_sprint)
    assert db.RESERVATION_ROLES == ("execution", "verification", "observation")
    assert db.reserve(conn, item, actor="a", session_id="s1", role="execute")["role"] == "execution"
    assert db.reserve(conn, item, actor="b", session_id="s2", role="review")["role"] == "verification"
    # Orchestration is session and project context, not a relationship to the
    # item, so a coordinator is an observer of the work it coordinates.
    assert db.reserve(conn, item, actor="c", session_id="s3", role="coordinate")["role"] == "observation"
    with pytest.raises(ValueError, match="invalid reservation role"):
        db.reserve(conn, item, actor="d", session_id="s4", role="lease")


def test_touch_requires_same_session_reassign_and_release_are_proof_free(conn, active_sprint):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    with pytest.raises(ValueError, match="another session"):
        db.touch_reservation(conn, row["id"], session_id="other")
    reassigned = db.reassign_reservation(conn, row["id"], actor="two", session_id="s2")
    assert reassigned["actor"] == "two"
    assert db.touch_reservation(conn, row["id"], session_id="s2")["state"] == "active"
    assert db.release_reservation(conn, row["id"], actor="operator")["state"] == "released"


def _backdate(conn, reservation_id, **delta):
    then = datetime.now(timezone.utc) - timedelta(**delta)
    conn.execute("UPDATE reservation SET last_activity_at = ? WHERE id = ?",
                 (then.strftime("%Y-%m-%dT%H:%M:%SZ"), reservation_id))
    conn.commit()


def test_item_work_by_the_reserving_session_counts_as_activity(conn, active_sprint):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    _backdate(conn, row["id"], hours=5)
    assert db.get_reservation(conn, row["id"])["stale"] is True

    assert db.note_session_activity(conn, item, session_id="s1")[0]["stale"] is False
    assert db.get_reservation(conn, row["id"])["stale"] is False


def test_activity_is_attributed_to_the_session_not_the_actor_name(conn, active_sprint):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    other_item = _item(conn, active_sprint, title="unrelated")
    _backdate(conn, row["id"], hours=5)

    # Same actor, different session: an actor string is not a session, and a
    # reservation on another item is not evidence about this one.
    assert db.note_session_activity(conn, item, session_id="s-other") == []
    assert db.note_session_activity(conn, other_item, session_id="s1") == []
    assert db.note_session_activity(conn, item, session_id=None) == []
    assert db.get_reservation(conn, row["id"])["stale"] is True


def test_staleness_and_sweep_horizons_are_operator_policy(conn, active_sprint, monkeypatch):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    _backdate(conn, row["id"], hours=4, seconds=1)
    assert db.get_reservation(conn, row["id"])["stale"] is True

    monkeypatch.setenv(reservation_policy.STALE_AFTER_ENV, "8")
    assert db.get_reservation(conn, row["id"])["stale"] is False
    monkeypatch.delenv(reservation_policy.STALE_AFTER_ENV)

    # Nothing expires on its own: the reservation is only interrupted because
    # an operator ran the sweep, and the horizon it applies is configurable.
    assert db.get_reservation(conn, row["id"])["state"] == "active"
    monkeypatch.setenv(reservation_policy.INTERRUPT_AFTER_ENV, "0.125")  # three hours
    swept = db.sweep_stale_reservations(conn)
    assert [value["id"] for value in swept] == [row["id"]]
    assert db.get_reservation(conn, row["id"])["interruption_reason"] == "3-hour inactivity sweep"


def test_seven_day_default_sweep_horizon(conn, active_sprint):
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    swept = db.sweep_stale_reservations(
        conn, now=(datetime.now(timezone.utc) + timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert [value["id"] for value in swept] == [row["id"]]
    assert db.get_reservation(conn, row["id"])["state"] == "interrupted"
    assert db.get_reservation(conn, row["id"])["interruption_reason"] == "7-day inactivity sweep"


def test_catalog_handlers_use_credential_free_reservation_operations(conn, active_sprint):
    item = _item(conn, active_sprint)
    app = WorkApplication(repo_id="test", store=conn, backend=db,
                          ingest_records=lambda _records: [], arbitrate_command=lambda *_args: None,
                          list_records=lambda *_args: [], list_decisions=lambda *_args: [])
    context = SimpleNamespace(identity=object(), request_id="test", repo_id=None)
    reserved = app.invoke("work.reservation.reserve", {"item_id": item, "actor": "one", "session_id": "s1"}, context)
    assert reserved["reservation"]["state"] == "active"
    read = app.invoke("work.read.reservations", {"item_id": item}, context)
    assert read["reservations"][0]["id"] == reserved["reservation"]["id"]
    released = app.invoke("work.reservation.release", {"reservation_id": reserved["reservation"]["id"]}, context)
    assert released["reservation"]["state"] == "released"


def test_role_normalization_is_shared_by_both_backends():
    """The taxonomy lives in one module so the facades cannot drift apart."""
    from sprintctl import pg

    assert pg.RESERVATION_ROLES == db.RESERVATION_ROLES == _reservation.ROLES
    assert pg.DEFAULT_RESERVATION_ROLE == db.DEFAULT_RESERVATION_ROLE == "execution"


def test_release_and_reassign_are_attributable_in_the_event_trail(conn, active_sprint):
    """The SQLite half of the audit-trail parity pinned in tests/pg."""
    item = _item(conn, active_sprint)
    row = db.reserve(conn, item, actor="one", session_id="s1")
    db.reassign_reservation(conn, row["id"], actor="two", session_id="s2")
    db.release_reservation(conn, row["id"], actor="two")

    # list_events reads newest-first; sort by id so the assertion is about
    # the order things happened, not about the read surface's convention.
    events = sorted(
        (event for event in db.list_events(conn, active_sprint["id"])
         if event["event_type"].startswith("reservation.")),
        key=lambda event: event["id"],
    )
    assert [event["event_type"] for event in events] == [
        "reservation.reserved",
        "reservation.reassigned",
        "reservation.released",
    ]
    assert [event["actor"] for event in events] == ["one", "two", "two"]


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("work.item.note", {"note_type": "progress", "summary": "did work"}),
        ("work.event.add", {"event_type": "note", "source_type": "actor"}),
    ],
)
def test_served_item_work_advances_the_activity_clock(conn, active_sprint, operation, arguments):
    """The application half of implicit activity, including the odd key out.

    ``work.event.add`` names its item ``work_item_id`` while every other
    activity-bearing operation uses ``item_id``. Reading only one of them made
    the clock silently stop for that operation, so both shapes are pinned.
    """
    item = _item(conn, active_sprint)
    app = WorkApplication(repo_id="test", store=conn, backend=db,
                          ingest_records=lambda _records: [], arbitrate_command=lambda *_args: None,
                          list_records=lambda *_args: [], list_decisions=lambda *_args: [])
    context = SimpleNamespace(identity=SimpleNamespace(actor="one"), request_id="test", repo_id=None)
    row = app.invoke("work.reservation.reserve",
                     {"item_id": item, "actor": "one", "session_id": "s1"}, context)["reservation"]
    _backdate(conn, row["id"], hours=5)
    assert db.get_reservation(conn, row["id"])["stale"] is True

    item_key = "work_item_id" if operation == "work.event.add" else "item_id"
    payload = {item_key: item, "session_id": "s1", **arguments}
    if operation == "work.event.add":
        payload["sprint_id"] = active_sprint["id"]
    app.invoke(operation, payload, context)

    assert db.get_reservation(conn, row["id"])["stale"] is False


def test_a_stranger_session_cannot_move_the_clock_through_the_application(conn, active_sprint):
    item = _item(conn, active_sprint)
    app = WorkApplication(repo_id="test", store=conn, backend=db,
                          ingest_records=lambda _records: [], arbitrate_command=lambda *_args: None,
                          list_records=lambda *_args: [], list_decisions=lambda *_args: [])
    context = SimpleNamespace(identity=SimpleNamespace(actor="one"), request_id="test", repo_id=None)
    row = app.invoke("work.reservation.reserve",
                     {"item_id": item, "actor": "one", "session_id": "s1"}, context)["reservation"]
    _backdate(conn, row["id"], hours=5)

    app.invoke("work.item.note",
               {"item_id": item, "session_id": "someone-else", "note_type": "progress",
                "summary": "not mine"}, context)

    assert db.get_reservation(conn, row["id"])["stale"] is True


def test_served_reservation_rejection_reads_like_every_other_served_failure(monkeypatch):
    """A server-side refusal must not reach the operator as a traceback.

    Reservation commands are registered without the runtime table the other
    served commands receive, so ``_served_result`` called the transport
    directly.  On 2026-08-28 ``reservation reserve --actor <someone-else>``
    printed a full Python traceback ending in
    ``vuoro_client.errors.InvocationRejectedError: actor-mismatch: ...``.
    The refusal is correct -- it is the served authority binding the actor to
    the authenticated identity -- and it should read that way.
    """

    from click.testing import CliRunner

    from sprintctl import backend as _backend
    from sprintctl import served as _served
    from sprintctl.cli import cli

    class _Rejected(Exception):
        pass

    config = SimpleNamespace(
        mode="served",
        served_profile="profile.json",
        repo_id="test-repo",
    )
    monkeypatch.setattr(_backend, "load_backend_config", lambda **_kwargs: config)

    def _reject(*_args, **_kwargs):
        raise _Rejected(
            "actor-mismatch: reservation actor must match the authenticated identity"
        )

    monkeypatch.setattr(_served, "reservation_operation", _reject)

    result = CliRunner().invoke(
        cli,
        [
            "reservation", "reserve",
            "--item-id", "1",
            "--actor", "someone-else",
            "--session-id", "s1",
        ],
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "actor-mismatch" in result.output
    assert result.output.startswith("Error: served reserve reservation failed:")
