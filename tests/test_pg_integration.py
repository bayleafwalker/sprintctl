"""
PostgreSQL integration tests for sprintctl.pg.

Requires a real PostgreSQL instance. Set SPRINTCTL_TEST_PG_URL to run:

    SPRINTCTL_TEST_PG_URL=postgresql://localhost/testdb pytest tests/test_pg_integration.py -v

All tests are automatically skipped when the variable is unset or psycopg is unavailable.
"""
from __future__ import annotations

import io
import json
import os
import uuid

import pytest

_PG_URL: str | None = os.environ.get("SPRINTCTL_TEST_PG_URL")

try:
    import psycopg
    from psycopg.rows import dict_row
    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False

_SKIP = not _PG_URL or not _PSYCOPG_AVAILABLE
_SKIP_REASON = (
    "SPRINTCTL_TEST_PG_URL not set"
    if not _PG_URL
    else "psycopg not installed — run: pip install 'sprintctl[remote]'"
)

pytestmark = pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)

# Safe unconditional imports: pg.py handles missing psycopg gracefully.
from sprintctl import pg
from sprintctl.db import ClaimConflict, InvalidTransition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def store():
    """Create a PgStore with a unique repo_id; clean up all rows at module teardown."""
    if _SKIP:
        pytest.skip(_SKIP_REASON)
    repo_id = f"itest-{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(_PG_URL, row_factory=dict_row)
    s = pg.PgStore(conn=conn, repo_id=repo_id)
    pg.init_db(s)
    yield s
    with conn.cursor() as cur:
        for table in ("dep", "ref", "claim", "event", "work_item", "track", "sprint"):
            cur.execute(f"DELETE FROM {table} WHERE repo_id = %s", (repo_id,))
    conn.commit()
    conn.close()


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def sprint_id(store):
    return pg.create_sprint(store, f"S-{_uid()}", "Goal", "2026-01-01", "2026-12-31", "active")


@pytest.fixture
def track_id(store, sprint_id):
    return pg.get_or_create_track(store, sprint_id, "eng")


@pytest.fixture
def work_item_id(store, sprint_id, track_id):
    return pg.create_work_item(store, sprint_id, track_id, f"Item-{_uid()}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_idempotent(self, store):
        pg.init_db(store)
        pg.init_db(store)


# ---------------------------------------------------------------------------
# Sprint
# ---------------------------------------------------------------------------

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

class TestWorkItem:
    def test_create_and_get(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "WI title", "desc")
        item = pg.get_work_item(store, iid)
        assert item is not None
        assert item["title"] == "WI title"
        assert item["status"] == "pending"

    def test_get_missing_returns_none(self, store):
        assert pg.get_work_item(store, 9_999_999) is None

    def test_list_by_sprint(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Li-{_uid()}")
        items = pg.list_work_items(store, sprint_id=sprint_id)
        assert any(i["id"] == iid for i in items)

    def test_list_by_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ls-{_uid()}")
        pending = pg.list_work_items(store, sprint_id=sprint_id, status="pending")
        assert any(i["id"] == iid for i in pending)

    def test_set_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ss-{_uid()}")
        pg.set_work_item_status(store, iid, "active")
        assert pg.get_work_item(store, iid)["status"] == "active"

    def test_set_status_invalid_transition_raises(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Inv-{_uid()}")
        with pytest.raises(InvalidTransition):
            pg.set_work_item_status(store, iid, "done")  # pending → done not allowed

    def test_claimed_item_requires_proof_for_status(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cp-{_uid()}")
        claim_id = pg.create_claim(store, iid, "ag", ttl_seconds=300)
        claim = pg.get_claim(store, claim_id, include_secret=True)
        token = claim["claim_token"]
        pg.set_work_item_status(store, iid, "active", claim_id=claim_id, claim_token=token)
        with pytest.raises(ClaimConflict):
            pg.set_work_item_status(store, iid, "done")


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class TestEvent:
    def test_create_and_list(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "hi"})
        events = pg.list_events(store, sprint_id)
        assert any(e["event_type"] == "note" for e in events)

    def test_payload_is_string(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                        payload={"summary": "str-check"})
        events = pg.list_events(store, sprint_id)
        note = next(e for e in reversed(events) if e["event_type"] == "note")
        assert isinstance(note["payload"], str)
        assert json.loads(note["payload"])["summary"] is not None

    def test_list_events_limited(self, store, sprint_id):
        for _ in range(4):
            pg.create_event(store, sprint_id, "ag", "note", source_type="actor",
                            payload={"summary": "x"})
        limited = pg.list_events_limited(store, sprint_id, limit=2)
        assert len(limited) == 2

    def test_list_knowledge_candidates(self, store, sprint_id, work_item_id):
        pg.create_event(store, sprint_id, "ag", "decision", source_type="actor",
                        work_item_id=work_item_id, payload={"summary": "chose X"})
        candidates = pg.list_knowledge_candidates(store, sprint_id)
        assert any(c["event_type"] == "decision" for c in candidates)
        assert all(isinstance(c["payload"], dict) for c in candidates)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

class TestClaim:
    def test_create_and_get(self, store, work_item_id):
        cid = pg.create_claim(store, work_item_id, "ag-A", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        assert claim is not None
        assert claim["agent"] == "ag-A"
        assert claim["claim_token"] is not None

    def test_get_missing_returns_none(self, store):
        assert pg.get_claim(store, 9_999_999) is None

    def test_token_not_exposed_by_default(self, store, work_item_id):
        cid = pg.create_claim(store, work_item_id, "ag-hidden", ttl_seconds=300)
        claim = pg.get_claim(store, cid)
        assert "claim_token" not in claim or claim.get("claim_token_redacted") is True

    def test_heartbeat_extends_ttl(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Hb-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-hb", ttl_seconds=60)
        claim = pg.get_claim(store, cid, include_secret=True)
        pg.heartbeat_claim(store, cid, claim["claim_token"], ttl_seconds=600)
        updated = pg.get_claim(store, cid)
        assert updated is not None

    def test_release_deletes_claim(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Rel-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-rel", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        pg.release_claim(store, cid, claim["claim_token"])
        assert pg.get_claim(store, cid) is None

    def test_handoff_rotates_token_and_changes_agent(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Ho-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-from", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)
        old_token = claim["claim_token"]
        new_claim = pg.handoff_claim(store, cid, old_token, actor="ag-to")
        assert new_claim["agent"] == "ag-to"
        assert new_claim["claim_token"] != old_token

    def test_find_claim_by_instance_id(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Fi-{_uid()}")
        inst = f"inst-{_uid()}"
        pg.create_claim(store, iid, "ag-fi", ttl_seconds=300, instance_id=inst)
        found = pg.find_claim_by_identity(store, instance_id=inst)
        assert len(found) == 1
        assert found[0]["instance_id"] == inst

    def test_list_claims_by_sprint(self, store, sprint_id, work_item_id):
        pg.create_claim(store, work_item_id, "ag-ls", ttl_seconds=300)
        claims = pg.list_claims_by_sprint(store, sprint_id)
        assert any(c["work_item_id"] == work_item_id for c in claims)

    def test_list_claims(self, store, work_item_id):
        pg.create_claim(store, work_item_id, "ag-lc", ttl_seconds=300)
        claims = pg.list_claims(store, work_item_id)
        assert isinstance(claims, list)

    def test_conflict_on_double_exclusive_claim(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cc-{_uid()}")
        pg.create_claim(store, iid, "ag-1", ttl_seconds=300)
        with pytest.raises(ClaimConflict):
            pg.create_claim(store, iid, "ag-2", ttl_seconds=300)


# ---------------------------------------------------------------------------
# Ref
# ---------------------------------------------------------------------------

class TestRef:
    def test_add_list_remove(self, store, work_item_id):
        rid = pg.add_ref(store, work_item_id, "pr",
                         "https://github.com/org/repo/pull/1")
        refs = pg.list_refs(store, work_item_id)
        assert any(r["id"] == rid for r in refs)
        pg.remove_ref(store, rid, work_item_id)
        assert not any(r["id"] == rid for r in pg.list_refs(store, work_item_id))

    def test_invalid_ref_type_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_ref(store, work_item_id, "tweet", "https://example.com")


# ---------------------------------------------------------------------------
# Dep
# ---------------------------------------------------------------------------

class TestDep:
    def test_add_and_list(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Da-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Db-{_uid()}")
        pg.add_dep(store, a, b)
        assert any(d["item_id"] == a for d in pg.list_deps_blocking(store, b))
        assert any(d["blocked_item_id"] == b for d in pg.list_deps_blocked_by(store, a))

    def test_remove_dep(self, store, sprint_id, track_id):
        x = pg.create_work_item(store, sprint_id, track_id, f"Dx-{_uid()}")
        y = pg.create_work_item(store, sprint_id, track_id, f"Dy-{_uid()}")
        dep_id = pg.add_dep(store, x, y)
        pg.remove_dep(store, dep_id, x)
        assert pg.list_deps_blocking(store, y) == []

    def test_get_ready_items(self, store, sprint_id, track_id):
        a = pg.create_work_item(store, sprint_id, track_id, f"Ra-{_uid()}")
        b = pg.create_work_item(store, sprint_id, track_id, f"Rb-{_uid()}")
        pg.add_dep(store, a, b)
        ready_ids = {i["id"] for i in pg.get_ready_items(store, sprint_id)}
        assert a in ready_ids
        assert b not in ready_ids

    def test_self_dep_raises(self, store, work_item_id):
        with pytest.raises(ValueError):
            pg.add_dep(store, work_item_id, work_item_id)


# ---------------------------------------------------------------------------
# NDJSON round-trip (export sqlite → import pg)
# ---------------------------------------------------------------------------

class TestNdjsonRoundTrip:
    def test_sqlite_to_pg_full_round_trip(self, store, tmp_path):
        """Export from a fresh sqlite db, import into pg, verify data survives."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rt.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        rt_repo_id = f"rt-{_uid()}"
        sid = _db.create_sprint(conn, "RT Sprint", "G", "2026-01-01", "2026-12-31", "active")
        tid = _db.get_or_create_track(conn, sid, "eng")
        iid = _db.create_work_item(conn, sid, tid, "RT Item")
        _db.create_event(conn, sid, actor="a", event_type="note", source_type="actor",
                         work_item_id=iid, payload={"summary": "round-trip"})
        _db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/99")
        conn.commit()

        buf = io.StringIO()
        export_counts = pg.export_ndjson(conn, rt_repo_id, buf)
        conn.close()

        rt_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row),
                              repo_id=rt_repo_id)
        pg.init_db(rt_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            import_counts = pg.import_ndjson(rt_store, records)

            assert import_counts["sprint"] == export_counts["sprint"] == 1
            assert import_counts["track"] == export_counts["track"] == 1
            assert import_counts["work_item"] == export_counts["work_item"] == 1
            assert import_counts["event"] == export_counts["event"] == 1
            assert import_counts["ref"] == export_counts["ref"] == 1

            sprints = pg.list_sprints(rt_store)
            assert any(s["name"] == "RT Sprint" for s in sprints)
            items = pg.list_work_items(rt_store)
            assert any(i["title"] == "RT Item" for i in items)
        finally:
            with rt_store.conn.cursor() as cur:
                for table in ("dep", "ref", "claim", "event", "work_item", "track", "sprint"):
                    cur.execute(f"DELETE FROM {table} WHERE repo_id = %s", (rt_repo_id,))
            rt_store.conn.commit()
            rt_store.conn.close()

    def test_import_with_replace_clears_existing_data(self, store, tmp_path):
        """import_ndjson with replace=True should delete existing rows first."""
        from sprintctl import db as _db

        sqlite_path = tmp_path / "rep.db"
        conn = _db.get_connection(sqlite_path)
        _db.init_db(conn)
        repl_repo_id = f"repl-{_uid()}"
        sid = _db.create_sprint(conn, "Repl Sprint", "G", "2026-01-01", "2026-12-31", "active")
        conn.commit()

        buf = io.StringIO()
        pg.export_ndjson(conn, repl_repo_id, buf)
        conn.close()

        repl_store = pg.PgStore(conn=psycopg.connect(_PG_URL, row_factory=dict_row),
                                repo_id=repl_repo_id)
        pg.init_db(repl_store)
        try:
            records = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            pg.import_ndjson(repl_store, records)
            # Re-importing with replace should succeed without unique-violation errors
            pg.import_ndjson(repl_store, records, replace=True)
            assert len(pg.list_sprints(repl_store)) == 1
        finally:
            with repl_store.conn.cursor() as cur:
                for table in ("dep", "ref", "claim", "event", "work_item", "track", "sprint"):
                    cur.execute(f"DELETE FROM {table} WHERE repo_id = %s", (repl_repo_id,))
            repl_store.conn.commit()
            repl_store.conn.close()


# ---------------------------------------------------------------------------
# Maintain
# ---------------------------------------------------------------------------

class TestMaintain:
    def test_purge_expired_claims(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Pu-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-pu", ttl_seconds=300)
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE claim SET expires_at = now() - interval '1 second'"
                " WHERE repo_id = %s AND id = %s",
                (store.repo_id, cid),
            )
        store.conn.commit()
        deleted = pg.purge_expired_claims(store, sprint_id)
        assert deleted >= 1


# ---------------------------------------------------------------------------
# Takeup
# ---------------------------------------------------------------------------

class TestTakeup:
    def test_list_takeup_history_structure(self, store, sprint_id):
        pg.create_event(store, sprint_id, "ag", "sprint-taken-up", source_type="actor",
                        payload={"summary": "up", "detail": None, "actor_kind": "agent",
                                 "hostname": "h", "pid": 1, "instance_id": "i1",
                                 "runtime_session_id": None})
        history = pg.list_takeup_history(store, sprint_id)
        assert "active_takeups" in history
        assert isinstance(history["active_takeups"], list)

    def test_list_active_takeups(self, store, sprint_id):
        result = pg.list_active_takeups(store, sprint_id)
        assert isinstance(result, list)
