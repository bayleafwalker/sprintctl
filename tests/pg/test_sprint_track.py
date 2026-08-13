"""PostgreSQL integration tests: Sprint, Track.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    db,
    pg,
    assert_disposable_connection,
    _uid,
    PG_MARKS,
    _PG_URL,
    json,
    threading,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


class TestSprint:
    def test_create_and_get(self, store):
        sid = pg.create_sprint(store, "SprintA", "G", "2026-01-01", "2026-12-31", "active")
        row = pg.get_sprint(store, sid)
        assert row is not None
        assert row["name"] == "SprintA"
        assert row["repo_id"] == store.repo_id

    def test_get_missing_returns_none(self, store):
        assert pg.get_sprint(store, 9_999_999) is None

    def test_get_active_sprint(self, store):
        pg.create_sprint(store, f"Active-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        result = pg.get_active_sprint(store)
        assert result is not None

    def test_list_active_sprints(self, store):
        pg.create_sprint(store, f"La-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        rows = pg.list_active_sprints(store)
        assert isinstance(rows, list)
        assert all(r["repo_id"] == store.repo_id for r in rows)

    def test_list_sprints(self, store):
        rows = pg.list_sprints(store)
        assert isinstance(rows, list)

    def test_set_sprint_status_active_to_closed(self, store):
        sid = pg.create_sprint(store, f"St-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        pg.set_sprint_status(store, sid, "closed")
        assert pg.get_sprint(store, sid)["status"] == "closed"

    def test_close_rejects_stale_revision_without_second_boundary_event(self, store):
        sid = pg.create_sprint(store, f"Scas-{_uid()}", "G", status="active")
        sprint = pg.get_sprint(store, sid)
        assert sprint is not None
        basis = db.sprint_status_revision(sprint)

        pg.close_sprint_with_boundary_event(
            store, sid, "winner", expected_revision=basis
        )
        events_after_accept = pg.list_events(store, sid)
        with pytest.raises(db.StatusConflict, match="status revision mismatch"):
            pg.close_sprint_with_boundary_event(
                store, sid, "stale", expected_revision=basis
            )

        assert pg.get_sprint(store, sid)["status"] == "closed"
        assert pg.list_events(store, sid) == events_after_accept

    def test_two_connections_accept_exactly_one_sprint_close_cas_writer(self, store):
        sid = pg.create_sprint(store, f"Scrace-{_uid()}", "G", status="active")
        sprint = pg.get_sprint(store, sid)
        assert sprint is not None
        basis = db.sprint_status_revision(sprint)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def close() -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            assert_disposable_connection(conn)
            independent = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                barrier.wait(timeout=10)
                try:
                    pg.close_sprint_with_boundary_event(
                        independent, sid, "closer", expected_revision=basis
                    )
                except db.StatusConflict:
                    outcomes.append("conflict")
                else:
                    outcomes.append("accepted")
            finally:
                conn.close()

        threads = [threading.Thread(target=close) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcomes) == ["accepted", "conflict"]
        assert pg.get_sprint(store, sid)["status"] == "closed"
        boundaries = [
            event for event in pg.list_events(store, sid)
            if event["event_type"] == "sprint-close-boundary"
        ]
        assert len(boundaries) == 1

    def test_explicit_close_appends_boundary_event_atomically(self, store):
        sid = pg.create_sprint(
            store,
            f"Cb-{_uid()}",
            "G",
            "2026-01-01",
            "2026-12-31",
            "active",
        )

        event_id = pg.close_sprint_with_boundary_event(store, sid, "operator")

        assert pg.get_sprint(store, sid)["status"] == "closed"
        event = next(
            event for event in pg.list_events(store, sid) if event["id"] == event_id
        )
        assert event["event_type"] == "sprint-close-boundary"
        assert event["actor"] == "operator"
        assert json.loads(event["payload"]) == {
            "previous_status": "active",
            "status": "closed",
        }

    def test_explicit_close_rolls_back_when_boundary_insert_fails(self, store, monkeypatch):
        sid = pg.create_sprint(
            store,
            f"Cr-{_uid()}",
            "G",
            "2026-01-01",
            "2026-12-31",
            "active",
        )

        def fail_insert(*args, **kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(pg, "_insert_event", fail_insert)
        with pytest.raises(RuntimeError, match="injected event failure"):
            pg.close_sprint_with_boundary_event(store, sid, "operator")

        assert pg.get_sprint(store, sid)["status"] == "active"
        assert pg.list_events(store, sid) == []

    def test_set_sprint_kind(self, store):
        sid = pg.create_sprint(store, f"Sk-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        pg.set_sprint_kind(store, sid, "backlog")
        assert pg.get_sprint(store, sid)["kind"] == "backlog"

    def test_timestamps_are_iso_strings(self, store):
        sid = pg.create_sprint(store, f"Ts-{_uid()}", "G", "2026-01-01", "2026-12-31", "active")
        row = pg.get_sprint(store, sid)
        assert isinstance(row["created_at"], str)
        assert "T" in row["created_at"]


# ---------------------------------------------------------------------------
# Track
# ---------------------------------------------------------------------------

class TestTrack:
    def test_get_or_create_idempotent(self, store, sprint_id):
        tid = pg.get_or_create_track(store, sprint_id, "eng")
        assert pg.get_or_create_track(store, sprint_id, "eng") == tid

    def test_list_tracks(self, store, sprint_id):
        pg.get_or_create_track(store, sprint_id, f"trk-{_uid()}")
        tracks = pg.list_tracks(store, sprint_id)
        assert len(tracks) >= 1

    def test_get_track(self, store, sprint_id):
        tid = pg.get_or_create_track(store, sprint_id, f"gt-{_uid()}")
        track = pg.get_track(store, tid)
        assert track is not None
        assert track["sprint_id"] == sprint_id


# ---------------------------------------------------------------------------
# WorkItem
# ---------------------------------------------------------------------------
