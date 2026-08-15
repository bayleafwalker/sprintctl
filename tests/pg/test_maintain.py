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
    def test_sweep_stale_reservations_interrupts_without_deleting(
        self, store, sprint_id, track_id
    ):
        """The v3 replacement for claim expiry: inactivity interrupts, never deletes.

        A swept reservation stays queryable as 'interrupted' with its reason
        recorded, so an operator can see what the sweep took and why.
        """
        iid = pg.create_work_item(store, sprint_id, track_id, f"Sw-{_uid()}")
        row = pg.reserve(store, iid, actor="ag-sw", session_id="session-sw")
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE reservation SET last_activity_at = now() - interval '30 days'"
                " WHERE repo_id = %s AND id = %s",
                (store.repo_id, row["id"]),
            )
        store.conn.commit()

        swept = pg.sweep_stale_reservations(store)

        assert [entry["id"] for entry in swept] == [row["id"]]
        after = pg.get_reservation(store, row["id"])
        assert after is not None
        assert after["state"] == "interrupted"
        assert after["interruption_reason"] == "7-day inactivity sweep"

    def test_sweep_leaves_recently_active_reservations_alone(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Fresh-{_uid()}")
        row = pg.reserve(store, iid, actor="ag-fresh", session_id="session-fresh")

        assert pg.sweep_stale_reservations(store) == []
        assert pg.get_reservation(store, row["id"])["state"] == "active"

    def test_reassign_then_takeover_retains_the_full_ownership_history(
        self, store, sprint_id, track_id
    ):
        """Ownership changes accumulate rows; nothing is rewritten in place.

        The retired claim path proved this with a rotating token and a
        lease_epoch counter, both dropped in v3. Reservations carry the same
        auditability without a secret: reassign renames the live row, and an
        explicit takeover interrupts it and opens a new one beside it.
        """
        iid = pg.create_work_item(store, sprint_id, track_id, f"Hist-{_uid()}")
        first = pg.reserve(store, iid, actor="old-owner", session_id="session-old")

        reassigned = pg.reassign_reservation(
            store, first["id"], actor="rotated-owner", session_id="session-rotated"
        )
        assert reassigned["id"] == first["id"]
        assert reassigned["actor"] == "rotated-owner"
        assert reassigned["state"] == "active"

        second = pg.reserve(
            store, iid, actor="new-owner", session_id="session-new", interrupt_existing=True
        )

        history = pg.list_reservations(store, iid, active_only=False)
        by_id = {entry["id"]: entry for entry in history}
        assert set(by_id) == {first["id"], second["id"]}
        assert by_id[first["id"]]["state"] == "interrupted"
        assert by_id[first["id"]]["actor"] == "rotated-owner"
        assert by_id[second["id"]]["state"] == "active"
        assert by_id[second["id"]]["actor"] == "new-owner"

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
