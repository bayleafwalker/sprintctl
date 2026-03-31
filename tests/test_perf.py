"""
Performance sanity checks for local-first use.

These are not benchmarks — they assert that common operations stay under
reasonable wall-clock limits on a developer machine with an in-memory DB.
All time budgets are generous enough that a slow CI runner won't flake, but
tight enough to catch O(N²) regressions in the query path.

Scale: a "large sprint" is 200 items across 5 tracks — well above any real
sprint, but realistic as a stress floor for the local SQLite model.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sprintctl import db, maintain
from sprintctl.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LARGE_SPRINT_ITEMS = 200
TRACKS = ["alpha", "beta", "gamma", "delta", "epsilon"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _build_large_sprint(conn) -> dict:
    """Create a sprint with LARGE_SPRINT_ITEMS items spread across TRACKS."""
    sid = db.create_sprint(conn, "PerfSprint", "perf test", "2026-01-01", "2026-06-30", "active")
    track_ids = {name: db.get_or_create_track(conn, sid, name) for name in TRACKS}
    for i in range(LARGE_SPRINT_ITEMS):
        track_name = TRACKS[i % len(TRACKS)]
        db.create_work_item(conn, sid, track_ids[track_name], f"Item {i:04d}")
    return db.get_sprint(conn, sid)


# ---------------------------------------------------------------------------
# Group 1: DB size growth
# ---------------------------------------------------------------------------

class TestDbSizeGrowth:
    def test_large_sprint_db_under_1mb(self, db_path):
        """A sprint with 200 items + events should stay well under 1 MB on disk."""
        conn = db.get_connection(db_path)
        db.init_db(conn)
        sprint = _build_large_sprint(conn)
        # Add one event per item to simulate active use
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        for item in items:
            db.create_event(
                conn, sprint["id"],
                actor="agent-a",
                event_type="note",
                source_type="actor",
                work_item_id=item["id"],
                payload={"summary": f"Progress on {item['title']}"},
            )
        conn.close()
        size_bytes = Path(db_path).stat().st_size
        assert size_bytes < 1_000_000, f"DB is {size_bytes / 1024:.1f} KB — unexpectedly large"

    def test_schema_tables_count(self, conn):
        """Schema must have exactly the expected set of tables — no accidental bloat."""
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected = {"sprint", "track", "work_item", "event", "claim", "ref", "dep", "schema_version"}
        assert tables == expected, f"Unexpected tables: {tables ^ expected}"


# ---------------------------------------------------------------------------
# Group 2: Query timing — list operations
# ---------------------------------------------------------------------------

class TestQueryTiming:
    def test_list_work_items_large_sprint_under_50ms(self, conn):
        sprint = _build_large_sprint(conn)
        start = time.monotonic()
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        elapsed = _ms(start)
        assert len(items) == LARGE_SPRINT_ITEMS
        assert elapsed < 50, f"list_work_items took {elapsed:.1f} ms"

    def test_list_work_items_filtered_under_20ms(self, conn):
        sprint = _build_large_sprint(conn)
        start = time.monotonic()
        items = db.list_work_items(conn, sprint_id=sprint["id"], track_name="alpha")
        elapsed = _ms(start)
        assert len(items) == LARGE_SPRINT_ITEMS // len(TRACKS)
        assert elapsed < 20, f"list_work_items (filtered) took {elapsed:.1f} ms"

    def test_list_events_large_sprint_under_50ms(self, conn):
        sprint = _build_large_sprint(conn)
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        for item in items[:50]:  # 50 events is enough to stress the query
            db.create_event(
                conn, sprint["id"], actor="a", event_type="note",
                source_type="actor", work_item_id=item["id"],
                payload={"summary": "note"},
            )
        start = time.monotonic()
        events = db.list_events(conn, sprint["id"])
        elapsed = _ms(start)
        assert elapsed < 50, f"list_events took {elapsed:.1f} ms"

    def test_get_ready_items_large_sprint_under_100ms(self, conn):
        """get_ready_items does N+1 dep queries — verify it stays linear at scale."""
        sprint = _build_large_sprint(conn)
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        # Add deps on every other item to make the traversal non-trivial
        for i in range(0, len(items) - 1, 2):
            db.add_dep(conn, items[i]["id"], items[i + 1]["id"])
        start = time.monotonic()
        ready = db.get_ready_items(conn, sprint["id"])
        elapsed = _ms(start)
        assert elapsed < 100, f"get_ready_items took {elapsed:.1f} ms"

    def test_render_large_sprint_under_100ms(self, conn):
        from sprintctl.cli import cli
        from click.testing import CliRunner
        sprint = _build_large_sprint(conn)
        runner = CliRunner()
        start = time.monotonic()
        result = runner.invoke(cli, ["render", "--sprint-id", str(sprint["id"])])
        elapsed = _ms(start)
        assert result.exit_code == 0, result.output
        assert elapsed < 100, f"render took {elapsed:.1f} ms"


# ---------------------------------------------------------------------------
# Group 3: Write throughput
# ---------------------------------------------------------------------------

class TestWriteThroughput:
    def test_bulk_item_creation_under_500ms(self, conn):
        """Creating 200 items sequentially must complete in under 500 ms."""
        sid = db.create_sprint(conn, "Bulk", "", "2026-01-01", "2026-06-30", "active")
        tid = db.get_or_create_track(conn, sid, "eng")
        start = time.monotonic()
        for i in range(LARGE_SPRINT_ITEMS):
            db.create_work_item(conn, sid, tid, f"Bulk item {i}")
        elapsed = _ms(start)
        assert elapsed < 500, f"bulk item creation took {elapsed:.1f} ms"

    def test_bulk_event_creation_under_500ms(self, conn):
        """Creating 200 events sequentially must complete in under 500 ms."""
        sid = db.create_sprint(conn, "BulkEv", "", "2026-01-01", "2026-06-30", "active")
        tid = db.get_or_create_track(conn, sid, "eng")
        iid = db.create_work_item(conn, sid, tid, "Task")
        start = time.monotonic()
        for i in range(LARGE_SPRINT_ITEMS):
            db.create_event(
                conn, sid, actor="a", event_type="note",
                source_type="actor", work_item_id=iid,
                payload={"summary": f"event {i}"},
            )
        elapsed = _ms(start)
        assert elapsed < 500, f"bulk event creation took {elapsed:.1f} ms"

    def test_bulk_ref_creation_under_200ms(self, conn):
        """Attaching 100 refs to a single item must complete in under 200 ms."""
        sid = db.create_sprint(conn, "RefBulk", "", "2026-01-01", "2026-06-30", "active")
        tid = db.get_or_create_track(conn, sid, "eng")
        iid = db.create_work_item(conn, sid, tid, "Big task")
        start = time.monotonic()
        for i in range(100):
            db.add_ref(conn, iid, "doc", f"https://docs.example.com/page-{i}")
        elapsed = _ms(start)
        assert elapsed < 200, f"bulk ref creation took {elapsed:.1f} ms"


# ---------------------------------------------------------------------------
# Group 4: Maintain sweep at scale
# ---------------------------------------------------------------------------

class TestSweepAtScale:
    def test_sweep_200_items_under_200ms(self, conn):
        """sweep over 200 active items (all stale) must finish in under 200 ms."""
        sprint = _build_large_sprint(conn)
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        # Activate all items and back-date their updated_at so they're stale
        for item in items:
            db.set_work_item_status(conn, item["id"], "active")
        conn.execute(
            "UPDATE work_item SET updated_at = '2020-01-01T00:00:00Z' WHERE sprint_id = ?",
            (sprint["id"],),
        )
        conn.commit()
        start = time.monotonic()
        result = maintain.sweep(conn, sprint["id"], _now(), threshold=timedelta(hours=1))
        elapsed = _ms(start)
        assert len(result["blocked_items"]) == LARGE_SPRINT_ITEMS
        assert elapsed < 200, f"sweep took {elapsed:.1f} ms"

    def test_purge_expired_claims_at_scale_under_100ms(self, conn):
        """Purging 100 expired claims must complete in under 100 ms."""
        sprint = _build_large_sprint(conn)
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        for item in items[:100]:
            db.create_claim(conn, item["id"], agent="agent-x")
        conn.execute(
            "UPDATE claim SET expires_at = '2000-01-01T00:00:00Z'"
        )
        conn.commit()
        start = time.monotonic()
        purged = maintain.purge_expired_claims(conn, sprint["id"])
        elapsed = _ms(start)
        assert purged == 100
        assert elapsed < 100, f"purge_expired_claims took {elapsed:.1f} ms"


# ---------------------------------------------------------------------------
# Group 5: usage --context at scale
# ---------------------------------------------------------------------------

class TestUsageContextAtScale:
    def test_usage_context_large_sprint_under_200ms(self, db_path):
        """usage --context on a 200-item sprint must complete in under 200 ms."""
        from click.testing import CliRunner
        conn = db.get_connection(db_path)
        db.init_db(conn)
        sprint = _build_large_sprint(conn)
        items = db.list_work_items(conn, sprint_id=sprint["id"])
        # Make half active with claims, other half pending
        for item in items[:100]:
            db.set_work_item_status(conn, item["id"], "active")
            db.create_claim(conn, item["id"], agent="agent-a")
        conn.close()
        runner = CliRunner()
        start = time.monotonic()
        result = runner.invoke(cli, ["usage", "--context", "--sprint-id", str(sprint["id"])])
        elapsed = _ms(start)
        assert result.exit_code == 0, result.output
        assert elapsed < 200, f"usage --context took {elapsed:.1f} ms"
