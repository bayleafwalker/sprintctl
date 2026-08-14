"""PostgreSQL integration tests: Maintain.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

from tests.pg._shared import (
    maintain,
    pg,
    _uid,
    PG_MARKS,
    datetime,
    timezone,
)

pytestmark = PG_MARKS


class TestMaintain:
    def test_purge_expired_claims_marks_and_retains_history(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Pu-{_uid()}")
        cid = pg.create_claim(store, iid, "ag-pu", ttl_seconds=300)
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE claim SET expires_at = now() - interval '1 second'"
                " WHERE repo_id = %s AND id = %s",
                (store.repo_id, cid),
            )
        store.conn.commit()
        expired = pg.purge_expired_claims(store, sprint_id)
        assert expired >= 1
        claim = pg.get_claim(store, cid)
        assert claim is not None
        assert claim["status"] == "expired"

    def test_expiry_reacquire_retains_both_rows_and_increments_epoch(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Epoch-{_uid()}")
        old_id = pg.create_claim(store, iid, "old-owner")
        old = pg.get_claim(store, old_id, include_secret=True)
        assert old["lease_epoch"] == 1

        rotated = pg.handoff_claim(
            store,
            old_id,
            old["claim_token"],
            actor="rotated-owner",
            mode="rotate",
        )
        assert rotated["lease_epoch"] == 2
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE claim SET expires_at = now() - interval '1 second' "
                "WHERE repo_id = %s AND id = %s",
                (store.repo_id, old_id),
            )
        store.conn.commit()

        new_id = pg.create_claim(store, iid, "new-owner")
        history = pg.list_claims(store, iid, active_only=False)
        assert [claim["claim_id"] for claim in history] == [old_id, new_id]
        assert [claim["status"] for claim in history] == ["expired", "active"]
        assert [claim["lease_epoch"] for claim in history] == [2, 3]

    def test_truth_findings_match_remote_backend(self, store, sprint_id, track_id):
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Drift-{_uid()}")
        pg.set_work_item_status(store, item_id, "active")
        event_id = pg.create_event(
            store,
            sprint_id,
            actor="wrapper",
            event_type="session.ended",
            source_type="daemon",
            payload={"git": {"commits": ["abc123"]}},
        )

        report = maintain.check(
            store,
            sprint_id,
            datetime.now(timezone.utc),
            _m=pg,
        )

        findings = {finding["reason_code"]: finding for finding in report["findings"]}
        assert findings["active-item-without-reservation"]["item_ids"] == [item_id]
        assert findings["code-evidence-without-item-link"]["event_ids"] == [event_id]


# ---------------------------------------------------------------------------
# Authority fault histories
# ---------------------------------------------------------------------------
