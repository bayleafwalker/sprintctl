"""Direct unit coverage for `application_common._parse_utc_timestamp`.

agentops#2499: the served PostgreSQL backend preserves fractional seconds on
read (`rows.py:iso_timestamp`), but `_parse_utc_timestamp` used to parse with
the strict `"%Y-%m-%dT%H:%M:%SZ"` format, which has no `%f` component, and so
raised an uncaught `ValueError` on any real `created_at` carrying fractional
seconds. These tests pin the fix directly against the helper, independent of
the fuller bucket replay in test_replay_2431_checkpointed_unacked.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sprintctl.application_common import _parse_utc_timestamp


def test_parses_whole_second_timestamp():
    result = _parse_utc_timestamp("2026-09-19T17:07:01Z")
    assert result == datetime(2026, 9, 19, 17, 7, 1, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_parses_fractional_second_timestamp():
    result = _parse_utc_timestamp("2026-09-19T17:07:01.250294Z")
    assert result == datetime(2026, 9, 19, 17, 7, 1, 250294, tzinfo=timezone.utc)
    assert result.tzinfo is timezone.utc


def test_none_returns_none():
    assert _parse_utc_timestamp(None) is None


def test_empty_string_returns_none():
    assert _parse_utc_timestamp("") is None


def test_naive_timestamp_is_treated_as_utc_not_local_offset():
    # No trailing "Z" and no offset: the helper is a UTC parser by name and
    # by its old contract, so this must be interpreted as UTC directly, not
    # shifted by the host's local timezone via `.astimezone()`.
    result = _parse_utc_timestamp("2026-09-19T17:07:01.250294")
    assert result == datetime(2026, 9, 19, 17, 7, 1, 250294, tzinfo=timezone.utc)


def test_non_utc_offset_timestamp_normalises_to_utc():
    result = _parse_utc_timestamp("2026-09-19T20:07:01.250294+03:00")
    assert result == datetime(2026, 9, 19, 17, 7, 1, 250294, tzinfo=timezone.utc)
