"""PostgreSQL integration tests: Claim.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    pg,
    ClaimConflict,
    _uid,
    PG_MARKS,
    _PG_URL,
    json,
    threading,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


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

    def test_explicit_lost_proof_adoption_rotates_remote_claim_token(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Adopt-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-from", ttl_seconds=300)
        claim = pg.get_claim(store, cid, include_secret=True)

        with pytest.raises(ValueError, match="Invalid claim_token"):
            pg.handoff_claim(
                store, cid, "not-the-token", actor="ag-to", allow_legacy_adopt=True
            )

        adopted = pg.handoff_claim(
            store, cid, None, actor="ag-to", allow_legacy_adopt=True, mode="rotate"
        )
        assert adopted["claim_token"] != claim["claim_token"]

        events = pg.list_events(store, sprint_id)
        handoff = [event for event in events if event["event_type"] == "claim-handoff"][-1]
        payload = json.loads(handoff["payload"])
        assert payload["lost_proof_adopted"] is True

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

    def test_concurrent_exclusive_claims_serialize_on_repo_authority_lock(
        self, store, sprint_id, track_id, monkeypatch
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cr-{_uid()}")
        first_locked = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        second_locked = threading.Event()
        original_lock = pg._ClaimPg.lock_capability_arbitration

        def instrumented_lock(self):
            if threading.current_thread().name == "claim-worker-b":
                second_attempted.set()
            original_lock(self)
            if threading.current_thread().name == "claim-worker-a":
                first_locked.set()
                assert release_first.wait(timeout=5)
            else:
                second_locked.set()

        monkeypatch.setattr(pg._ClaimPg, "lock_capability_arbitration", instrumented_lock)
        outcomes = []
        outcomes_lock = threading.Lock()

        def worker(actor):
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            independent_store = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                claim_id = pg.create_claim(independent_store, iid, actor, ttl_seconds=300)
                outcome = {"actor": actor, "result": "accepted", "claim_id": claim_id}
            except ClaimConflict as exc:
                outcome = {"actor": actor, "result": "rejected", "error": str(exc)}
            finally:
                conn.close()
            with outcomes_lock:
                outcomes.append(outcome)

        first = threading.Thread(target=worker, args=("ag-a",), name="claim-worker-a")
        second = threading.Thread(target=worker, args=("ag-b",), name="claim-worker-b")
        first.start()
        assert first_locked.wait(timeout=5)
        second.start()
        assert second_attempted.wait(timeout=5)
        assert not second_locked.wait(timeout=0.1), "second claim bypassed the arbitration lock"
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()

        assert sorted(outcome["result"] for outcome in outcomes) == ["accepted", "rejected"]
        with store.conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS count FROM claim
                WHERE repo_id = %s AND work_item_id = %s AND exclusive = true
                  AND expires_at > now()
                """,
                (store.repo_id, iid),
            )
            assert cur.fetchone()["count"] == 1


# ---------------------------------------------------------------------------
# Ref
# ---------------------------------------------------------------------------
