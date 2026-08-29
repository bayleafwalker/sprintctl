"""The handoff delta must survive both backends' timestamp types.

Regression for a served failure found by running the resumability outcome rather
than reading the code: `sprintctl handoff` against the shared served backend returned
`operation-handler-failed` for every sprint that had an active reservation, and
succeeded again the moment the reservation was released. Every other served surface --
context, next-work, context-candidates, reservation list -- kept working, which is what
localised it to the one comparison only the handoff bundle makes.

The cause is a type seam, not a logic error. `pg.py` stores timestamps as `timestamptz`
and returns `datetime`; `db.py` (sqlite) returns ISO strings; `reservation.display`
copies the row through without converting either. `_delta_since_last_handoff` compared
`row["last_activity_at"] > previous_handoff_at` directly, so on pg it compared a
`datetime` with a `str` and raised `TypeError`.

Why it mattered more than an ordinary 500: an interrupted session leaves an active
reservation behind, and the handoff bundle is the first thing a resuming session is
told to read. The surface failed precisely in the state it exists to serve.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import handoff

CUTOFF_TEXT = "2026-08-28T17:10:47.247860Z"
CUTOFF = datetime(2026, 8, 28, 17, 10, 47, 247860, tzinfo=timezone.utc)
PREVIOUS_HANDOFF = {"id": 100, "created_at": CUTOFF_TEXT, "event_type": "handoff-generated"}


def _delta(*, items, reservations, previous=PREVIOUS_HANDOFF, events=()):
    return handoff._delta_since_last_handoff(
        previous_handoff=previous,
        items=list(items),
        all_events=list(events),
        active_reservations=list(reservations),
    )


def test_datetime_reservation_rows_do_not_raise():
    """The pg shape: a `datetime` row against a string cutoff."""
    delta = _delta(
        items=[],
        reservations=[
            {"id": 41, "last_activity_at": CUTOFF + timedelta(hours=13)},
            {"id": 42, "last_activity_at": CUTOFF - timedelta(days=2)},
        ],
    )
    assert delta["reservation_ids_touched"] == [41]


def test_string_reservation_rows_still_work():
    """The sqlite shape, unchanged."""
    delta = _delta(
        items=[],
        reservations=[
            {"id": 43, "last_activity_at": "2026-08-29T06:49:47Z"},
            {"id": 44, "last_activity_at": "2026-08-27T06:49:47Z"},
        ],
    )
    assert delta["reservation_ids_touched"] == [43]


def test_mixed_backends_agree_on_the_same_instant():
    """The same moment, spelled either way, has to answer the same."""
    moment = CUTOFF + timedelta(seconds=30)
    as_datetime = _delta(items=[], reservations=[{"id": 45, "last_activity_at": moment}])
    as_text = _delta(
        items=[],
        reservations=[{"id": 45, "last_activity_at": moment.isoformat().replace("+00:00", "Z")}],
    )
    assert as_datetime["reservation_ids_touched"] == as_text["reservation_ids_touched"] == [45]


def test_items_are_compared_as_instants_not_as_strings():
    """`"...:47Z"` sorts after `"...:47.5Z"` because `Z` > `.`; instants do not.

    The item is a hundredth of a second *before* the cutoff and must not count as
    touched, which string comparison got wrong in the same expression.
    """
    delta = _delta(
        items=[
            {"id": 1, "updated_at": "2026-08-28T17:10:47Z"},
            {"id": 2, "updated_at": CUTOFF + timedelta(seconds=1)},
        ],
        reservations=[],
    )
    assert delta["item_ids_touched"] == [2]


def test_a_missing_or_unparseable_timestamp_is_not_a_failure():
    """A bundle is worth more than a perfect delta: an odd row is skipped, not raised."""
    delta = _delta(
        items=[{"id": 1, "updated_at": None}, {"id": 2}],
        reservations=[
            {"id": 46},
            {"id": 47, "last_activity_at": None},
            {"id": 48, "last_activity_at": "not a timestamp"},
        ],
    )
    assert delta["item_ids_touched"] == []
    assert delta["reservation_ids_touched"] == []


def test_no_previous_handoff_reports_every_event():
    delta = _delta(items=[], reservations=[], previous=None, events=[{"id": 1}, {"id": 2}])
    assert delta == {
        "previous_handoff_at": None,
        "item_ids_touched": [],
        "event_count": 2,
        "reservation_ids_touched": [],
    }


@pytest.mark.parametrize("cutoff_value", [CUTOFF_TEXT, CUTOFF])
def test_the_cutoff_itself_may_arrive_as_either_type(cutoff_value):
    """`previous_handoff["created_at"]` comes from the same two backends."""
    previous = {"id": 100, "created_at": cutoff_value}
    delta = _delta(
        items=[],
        reservations=[{"id": 49, "last_activity_at": CUTOFF + timedelta(minutes=1)}],
        previous=previous,
    )
    assert delta["reservation_ids_touched"] == [49]
    assert delta["previous_handoff_at"] == cutoff_value
