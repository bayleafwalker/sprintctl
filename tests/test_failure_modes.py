"""
Failure-mode tests: claim token collisions, concurrent write patterns,
expired claim edge cases, ref integrity, and dep edge cases.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from sprintctl import db, maintain
from sprintctl.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _item(conn, sprint_id, title="Task"):
    tid = db.get_or_create_track(conn, sprint_id, "eng")
    return db.create_work_item(conn, sprint_id, tid, title)


def _claim(conn, item_id, agent="agent-a", **kwargs) -> dict:
    cid = db.create_claim(conn, item_id, agent=agent, **kwargs)
    return db.get_claim(conn, cid, include_secret=True)


def _expire(conn, claim_id):
    """Manually back-date expires_at so the claim reads as expired."""
    conn.execute(
        "UPDATE claim SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
        (claim_id,),
    )
    conn.commit()


def _status(conn, item_id, new_status):
    db.set_work_item_status(conn, item_id, new_status, actor="a")


# ---------------------------------------------------------------------------
# Group 1: Claim — expired claim edge cases
# ---------------------------------------------------------------------------

class TestExpiredClaims:
    def test_expired_claim_not_in_active_list(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        active = db.list_claims(conn, iid, active_only=True)
        assert all(c["id"] != claim["claim_id"] for c in active)

    def test_expired_claim_visible_without_active_only(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        all_claims = db.list_claims(conn, iid, active_only=False)
        assert any(c["id"] == claim["claim_id"] for c in all_claims)

    def test_exclusive_claim_allowed_after_expiry(self, conn, active_sprint):
        """After a claim expires, a new exclusive claim on the same item must succeed."""
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        cid2 = db.create_claim(conn, iid, agent="agent-b")
        assert cid2 is not None

    def test_heartbeat_on_expired_claim_still_refreshes(self, conn, active_sprint):
        """Heartbeat refreshes expires_at even if the claim was expired — token proves ownership."""
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        db.heartbeat_claim(conn, claim["claim_id"], claim["claim_token"], ttl_seconds=300)
        row = conn.execute(
            "SELECT expires_at FROM claim WHERE id = ?", (claim["claim_id"],)
        ).fetchone()
        assert row["expires_at"] > "2000-01-01"

    def test_sweep_purges_expired_claim_once(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        now = _now()
        result1 = maintain.sweep(conn, active_sprint["id"], now)
        assert result1["expired_claims_purged"] >= 1
        result2 = maintain.sweep(conn, active_sprint["id"], now)
        assert result2["expired_claims_purged"] == 0

    def test_release_expired_claim_with_valid_token_succeeds(self, conn, active_sprint):
        """An agent can release their own claim even after it expires, as long as token is valid."""
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        db.release_claim(conn, claim["claim_id"], claim["claim_token"], actor="agent-a")
        row = conn.execute(
            "SELECT id FROM claim WHERE id = ?", (claim["claim_id"],)
        ).fetchone()
        assert row is None

    def test_release_expired_claim_wrong_token_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        _expire(conn, claim["claim_id"])
        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.release_claim(conn, claim["claim_id"], "wrong-token", actor="agent-b")


# ---------------------------------------------------------------------------
# Group 2: Claim — invalid / missing token edge cases
# ---------------------------------------------------------------------------

class TestClaimTokenEdgeCases:
    def test_heartbeat_nonexistent_claim_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.heartbeat_claim(conn, 9999, "any-token")

    def test_release_nonexistent_claim_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.release_claim(conn, 9999, "any-token")

    def test_create_claim_invalid_type_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid claim_type"):
            db.create_claim(conn, iid, agent="agent-a", claim_type="bogus")

    def test_create_claim_on_nonexistent_item_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.create_claim(conn, 9999, agent="agent-a")

    def test_null_token_claim_heartbeat_emits_ambiguity_event(self, conn, active_sprint):
        """Claims with NULL token should emit claim-ambiguity-detected on bad heartbeat."""
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        conn.execute("UPDATE claim SET claim_token = NULL WHERE id = ?", (cid,))
        conn.commit()
        with pytest.raises(ValueError):
            db.heartbeat_claim(conn, cid, "some-token", actor="agent-b")
        events = db.list_events(conn, active_sprint["id"])
        ambiguity = [e for e in events if e["event_type"] == "claim-ambiguity-detected"]
        assert ambiguity, "Expected claim-ambiguity-detected event"

    def test_null_token_claim_release_emits_ambiguity_event(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        cid = db.create_claim(conn, iid, agent="agent-a")
        conn.execute("UPDATE claim SET claim_token = NULL WHERE id = ?", (cid,))
        conn.commit()
        with pytest.raises(ValueError):
            db.release_claim(conn, cid, "some-token", actor="agent-b")
        events = db.list_events(conn, active_sprint["id"])
        ambiguity = [e for e in events if e["event_type"] == "claim-ambiguity-detected"]
        assert ambiguity

    def test_wrong_token_heartbeat_emits_coordination_failure(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.heartbeat_claim(conn, claim["claim_id"], "bad-token", actor="agent-b")
        events = db.list_events(conn, active_sprint["id"])
        coord_fail = [e for e in events if e["event_type"] == "coordination-failure"]
        assert coord_fail

    def test_token_uniqueness_across_claims(self, conn, active_sprint):
        """Two claims on different items must have distinct tokens."""
        iid1 = _item(conn, active_sprint["id"], "Task A")
        iid2 = _item(conn, active_sprint["id"], "Task B")
        c1 = _claim(conn, iid1, agent="agent-a")
        c2 = _claim(conn, iid2, agent="agent-b")
        assert c1["claim_token"] != c2["claim_token"]

    def test_token_rotated_on_handoff(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        original_token = claim["claim_token"]
        handed = db.handoff_claim(
            conn, claim["claim_id"], original_token, actor="agent-a", mode="rotate"
        )
        assert handed["claim_token"] != original_token

    def test_old_token_invalid_after_handoff(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        db.handoff_claim(
            conn, claim["claim_id"], claim["claim_token"], actor="agent-a", mode="rotate"
        )
        with pytest.raises(ValueError, match="Invalid claim_token"):
            db.heartbeat_claim(conn, claim["claim_id"], claim["claim_token"], actor="agent-a")

    def test_create_claim_retries_on_token_collision(self, conn, active_sprint, monkeypatch):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        tokens = iter(["fixed-token", "fixed-token", "unique-token"])
        monkeypatch.setattr(db, "_generate_claim_token", lambda: next(tokens))

        c1 = db.create_claim(conn, iid_a, agent="agent-a")
        c2 = db.create_claim(conn, iid_b, agent="agent-b")

        claim1 = db.get_claim(conn, c1, include_secret=True)
        claim2 = db.get_claim(conn, c2, include_secret=True)
        assert claim1["claim_token"] == "fixed-token"
        assert claim2["claim_token"] == "unique-token"

    def test_create_claim_raises_after_repeated_token_collision(self, conn, active_sprint, monkeypatch):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        monkeypatch.setattr(db, "_generate_claim_token", lambda: "always-collide")

        db.create_claim(conn, iid_a, agent="agent-a")
        with pytest.raises(RuntimeError, match="unique claim token"):
            db.create_claim(conn, iid_b, agent="agent-b")


# ---------------------------------------------------------------------------
# Group 3: Claim — concurrent write patterns
# ---------------------------------------------------------------------------

class TestConcurrentClaimWrites:
    def test_second_exclusive_claim_raises_conflict(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a")
        with pytest.raises(db.ClaimConflict):
            db.create_claim(conn, iid, agent="agent-b")

    def test_non_exclusive_claim_does_not_block_another_non_exclusive(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", exclusive=False)
        cid2 = db.create_claim(conn, iid, agent="agent-b", exclusive=False)
        assert cid2 is not None

    def test_existing_exclusive_blocks_new_exclusive(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.create_claim(conn, iid, agent="agent-a", claim_type="execute", exclusive=True)
        with pytest.raises(db.ClaimConflict):
            db.create_claim(conn, iid, agent="agent-b", claim_type="inspect", exclusive=True)

    def test_threaded_race_only_one_claim_wins(self, db_path):
        """Two threads race to claim the same item; exactly one should succeed."""
        conn_main = db.get_connection(db_path)
        db.init_db(conn_main)
        sid = db.create_sprint(conn_main, "Race Sprint", "", "2026-03-01", "2026-03-31", "active")
        tid = db.get_or_create_track(conn_main, sid, "eng")
        iid = db.create_work_item(conn_main, sid, tid, "Raced Task")
        conn_main.close()

        results = []
        errors = []

        def try_claim(agent_name):
            c = db.get_connection(db_path)
            db.init_db(c)
            try:
                cid = db.create_claim(c, iid, agent=agent_name)
                results.append(("ok", agent_name, cid))
            except db.ClaimConflict:
                results.append(("conflict", agent_name, None))
            except Exception as e:
                errors.append((agent_name, e))
            finally:
                c.close()

        t1 = threading.Thread(target=try_claim, args=("agent-x",))
        t2 = threading.Thread(target=try_claim, args=("agent-y",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Unexpected errors: {errors}"
        ok_results = [r for r in results if r[0] == "ok"]
        assert len(ok_results) == 1, f"Expected exactly 1 winner, got: {results}"

    def test_claim_after_release_allowed(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        claim = _claim(conn, iid, agent="agent-a")
        db.release_claim(conn, claim["claim_id"], claim["claim_token"], actor="agent-a")
        cid2 = db.create_claim(conn, iid, agent="agent-b")
        assert cid2 is not None


# ---------------------------------------------------------------------------
# Group 4: Ref — failure modes
# ---------------------------------------------------------------------------

class TestRefFailureModes:
    def test_add_ref_invalid_type_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid ref_type"):
            db.add_ref(conn, iid, "bogus", "https://example.com")

    def test_add_ref_nonexistent_item_raises(self, conn, active_sprint):
        with pytest.raises(ValueError, match="not found"):
            db.add_ref(conn, 9999, "pr", "https://example.com")

    def test_add_ref_invalid_target_url_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="Invalid ref URL"):
            db.add_ref(conn, iid, "doc", "bad-target")

    def test_remove_ref_wrong_item_raises(self, conn, active_sprint):
        iid1 = _item(conn, active_sprint["id"], "Item A")
        iid2 = _item(conn, active_sprint["id"], "Item B")
        ref_id = db.add_ref(conn, iid1, "pr", "https://github.com/org/repo/pull/1")
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, ref_id, iid2)

    def test_remove_ref_nonexistent_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, 9999, iid)

    def test_remove_ref_twice_raises_on_second(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        ref_id = db.add_ref(conn, iid, "doc", "https://docs.example.com")
        db.remove_ref(conn, ref_id, iid)
        with pytest.raises(ValueError, match="not found"):
            db.remove_ref(conn, ref_id, iid)

    def test_list_refs_deleted_item_returns_empty(self, conn, active_sprint):
        """After item cascade-delete, its refs must be gone."""
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/99")
        conn.execute("DELETE FROM work_item WHERE id = ?", (iid,))
        conn.commit()
        refs = db.list_refs(conn, iid)
        assert refs == []

    def test_multiple_refs_same_item_all_returned(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/1")
        db.add_ref(conn, iid, "issue", "https://github.com/org/repo/issues/42")
        db.add_ref(conn, iid, "doc", "https://docs.example.com/design")
        refs = db.list_refs(conn, iid)
        assert len(refs) == 3
        assert {r["ref_type"] for r in refs} == {"pr", "issue", "doc"}

    def test_ref_on_done_item_allowed(self, conn, active_sprint):
        """Refs can be attached to done items — no status restriction."""
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        ref_id = db.add_ref(conn, iid, "pr", "https://github.com/org/repo/pull/5")
        assert ref_id is not None


# ---------------------------------------------------------------------------
# Group 5: Dep — failure modes and edge cases
# ---------------------------------------------------------------------------

class TestDepFailureModes:
    def test_add_dep_nonexistent_blocker_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            db.add_dep(conn, 9999, iid)

    def test_add_dep_nonexistent_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            db.add_dep(conn, iid, 9999)

    def test_remove_dep_nonexistent_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(ValueError, match="not found"):
            db.remove_dep(conn, 9999, iid)

    def test_remove_dep_wrong_item_raises(self, conn, active_sprint):
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        iid_c = _item(conn, active_sprint["id"], "C")
        dep_id = db.add_dep(conn, iid_a, iid_b)
        with pytest.raises(ValueError, match="not found"):
            db.remove_dep(conn, dep_id, iid_c)

    def test_blocked_item_not_in_ready_list(self, conn, active_sprint):
        iid_blocker = _item(conn, active_sprint["id"], "Blocker")
        iid_blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, iid_blocker, iid_blocked)
        ready_ids = {it["id"] for it in db.get_ready_items(conn, active_sprint["id"])}
        assert iid_blocked not in ready_ids
        assert iid_blocker in ready_ids

    def test_item_becomes_ready_after_blocker_done(self, conn, active_sprint):
        iid_blocker = _item(conn, active_sprint["id"], "Blocker")
        iid_blocked = _item(conn, active_sprint["id"], "Blocked")
        db.add_dep(conn, iid_blocker, iid_blocked)
        _status(conn, iid_blocker, "active")
        _status(conn, iid_blocker, "done")
        ready_ids = {it["id"] for it in db.get_ready_items(conn, active_sprint["id"])}
        assert iid_blocked in ready_ids

    def test_dep_deleted_item_cascade(self, conn, active_sprint):
        """Deleting an item must cascade-delete its deps."""
        iid_a = _item(conn, active_sprint["id"], "A")
        iid_b = _item(conn, active_sprint["id"], "B")
        db.add_dep(conn, iid_a, iid_b)
        conn.execute("DELETE FROM work_item WHERE id = ?", (iid_a,))
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM dep WHERE item_id = ? OR blocked_item_id = ?", (iid_a, iid_a)
        ).fetchall()
        assert rows == []

    def test_ready_items_no_deps_all_pending_included(self, conn, active_sprint):
        iid1 = _item(conn, active_sprint["id"], "Free A")
        iid2 = _item(conn, active_sprint["id"], "Free B")
        ready_ids = {it["id"] for it in db.get_ready_items(conn, active_sprint["id"])}
        assert iid1 in ready_ids
        assert iid2 in ready_ids


# ---------------------------------------------------------------------------
# Group 6: State transition — exhaustive invalid paths
# ---------------------------------------------------------------------------

class TestStateTransitionFailureModes:
    def test_pending_to_done_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "done")

    def test_pending_to_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "blocked")

    def test_done_to_active_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "active")

    def test_done_to_pending_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "pending")

    def test_done_to_blocked_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "done")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "blocked")

    def test_blocked_to_done_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "blocked")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "done")

    def test_blocked_to_pending_raises(self, conn, active_sprint):
        iid = _item(conn, active_sprint["id"])
        _status(conn, iid, "active")
        _status(conn, iid, "blocked")
        with pytest.raises(db.InvalidTransition):
            _status(conn, iid, "pending")

    def test_sprint_planned_to_closed_raises(self, conn):
        sid = db.create_sprint(conn, "P", "", "2026-01-01", "2026-01-31", "planned")
        with pytest.raises(db.InvalidTransition):
            db.set_sprint_status(conn, sid, "closed")

    def test_sprint_closed_to_active_raises(self, conn):
        sid = db.create_sprint(conn, "P", "", "2026-01-01", "2026-01-31", "planned")
        db.set_sprint_status(conn, sid, "active")
        db.set_sprint_status(conn, sid, "closed")
        with pytest.raises(db.InvalidTransition):
            db.set_sprint_status(conn, sid, "active")

    def test_set_item_status_unknown_item_raises(self, conn, active_sprint):
        with pytest.raises(ValueError):
            _status(conn, 9999, "active")

    def test_set_sprint_status_unknown_sprint_raises(self, conn):
        with pytest.raises((ValueError, AttributeError)):
            db.set_sprint_status(conn, 9999, "active")


# ---------------------------------------------------------------------------
# Group 7: Maintain sweep — edge cases not covered elsewhere
# ---------------------------------------------------------------------------

class TestMaintainSweepEdgeCases:
    def test_sweep_unknown_sprint_returns_empty(self, conn):
        """sweep on an unknown sprint_id silently returns empty results (no items to sweep)."""
        result = maintain.sweep(conn, 9999, _now())
        assert result["blocked_items"] == []
        assert result["expired_claims_purged"] == 0

    def test_check_unknown_sprint_raises(self, conn):
        with pytest.raises((ValueError, Exception), match="not found"):
            maintain.check(conn, 9999, _now())

    def test_sweep_does_not_affect_other_sprint_claims(self, conn):
        """Expired claims in sprint A must not be purged when sweeping sprint B."""
        sid_a = db.create_sprint(conn, "A", "", "2026-01-01", "2026-01-31", "active")
        sid_b = db.create_sprint(conn, "B", "", "2026-02-01", "2026-02-28", "active")
        iid_a = db.create_work_item(conn, db.get_or_create_track(conn, sid_a, "eng"), sid_a, "Task A")
        cid_a = db.create_claim(conn, iid_a, agent="agent-a")
        _expire(conn, cid_a)

        result = maintain.sweep(conn, sid_b, _now())
        assert result["expired_claims_purged"] == 0

        row = conn.execute("SELECT id FROM claim WHERE id = ?", (cid_a,)).fetchone()
        assert row is not None

    def test_sweep_stale_threshold_env(self, conn, active_sprint, monkeypatch):
        """SPRINTCTL_STALE_THRESHOLD=0 makes all active items immediately stale."""
        monkeypatch.setenv("SPRINTCTL_STALE_THRESHOLD", "0")
        iid = _item(conn, active_sprint["id"], "Active task")
        _status(conn, iid, "active")
        result = maintain.sweep(conn, active_sprint["id"], _now(), threshold=timedelta(seconds=0))
        assert len(result["blocked_items"]) >= 1
