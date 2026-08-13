"""PostgreSQL integration tests: WorkItem.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    contracts,
    db,
    pg,
    ClaimConflict,
    InvalidTransition,
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


class TestWorkItem:
    def test_create_and_get(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "WI title", "desc")
        item = pg.get_work_item(store, iid)
        assert item is not None
        assert item["title"] == "WI title"
        assert item["description"] == "desc"
        assert item["status"] == "pending"

    def test_update_description(self, store, sprint_id, track_id):
        iid = pg.create_work_item(store, sprint_id, track_id, "Editable", "Old scope")
        current = pg.get_work_item_with_edit_revision(store, iid)
        assert current is not None

        edited = pg.update_work_item_description(
            store,
            iid,
            "New shaped scope",
            expected_revision=current[1],
            actor="pg-editor",
        )

        assert pg.get_work_item(store, iid)["description"] == "New shaped scope"
        assert edited["previous_revision"] == current[1]
        assert edited["revision"] != current[1]
        with pytest.raises(db.EditConflict, match="revision mismatch"):
            pg.update_work_item_description(
                store,
                iid,
                "Stale scope",
                expected_revision=current[1],
                actor="stale-editor",
            )
        events = pg.list_events(store, sprint_id)
        audit = next(event for event in events if event["id"] == edited["event_id"])
        assert audit["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
        assert audit["actor"] == "pg-editor"
        assert set(json.loads(audit["payload"])) == {
            "summary",
            "field",
            "previous_description",
            "description",
            "previous_revision",
            "revision",
        }

    def test_edit_rolls_back_when_audit_insert_fails(
        self, store, sprint_id, track_id, monkeypatch
    ):
        iid = pg.create_work_item(
            store, sprint_id, track_id, "Rollback edit", "Original scope"
        )
        current = pg.get_work_item_with_edit_revision(store, iid)
        assert current is not None

        def fail_event_insert(*args, **kwargs):
            raise RuntimeError("injected pg audit insert failure")

        monkeypatch.setattr(pg, "_insert_event", fail_event_insert)
        with pytest.raises(RuntimeError, match="injected pg audit insert failure"):
            pg.update_work_item_description(
                store,
                iid,
                "Must roll back",
                expected_revision=current[1],
                actor="pg-editor",
            )

        assert pg.get_work_item(store, iid)["description"] == "Original scope"
        assert not [
            event
            for event in pg.list_events(store, sprint_id)
            if event["work_item_id"] == iid
        ]

    def test_overlapping_edit_writers_accept_exactly_one_revision(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(
            store, sprint_id, track_id, "Concurrent edit", "Original scope"
        )
        current = pg.get_work_item_with_edit_revision(store, iid)
        assert current is not None
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []

        def edit(label: str) -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            assert_disposable_connection(conn)
            independent = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                barrier.wait(timeout=10)
                try:
                    pg.update_work_item_description(
                        independent,
                        iid,
                        f"{label} scope",
                        expected_revision=current[1],
                        actor=label,
                    )
                except db.EditConflict:
                    outcomes.append((label, "conflict"))
                else:
                    outcomes.append((label, "accepted"))
            finally:
                conn.close()

        threads = [
            threading.Thread(target=edit, args=("writer-a",)),
            threading.Thread(target=edit, args=("writer-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcome for _label, outcome in outcomes) == [
            "accepted",
            "conflict",
        ]

        item = pg.get_work_item(store, iid)
        assert item is not None
        accepted_actor = next(
            label for label, outcome in outcomes if outcome == "accepted"
        )
        assert item["description"] == f"{accepted_actor} scope"
        edits = [
            event
            for event in pg.list_events(store, sprint_id)
            if event["work_item_id"] == iid
            and event["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
        ]
        assert len(edits) == 1
        assert edits[0]["actor"] == accepted_actor

    def test_update_description_validation_and_missing_item(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, "Validated", "Keep scope")

        with pytest.raises(ValueError, match="non-whitespace"):
            pg.update_work_item_description(store, iid, "  \n  ")
        with pytest.raises(ValueError, match="NUL"):
            pg.update_work_item_description(store, iid, "invalid\x00scope")
        with pytest.raises(ValueError, match="Item #999999999 not found"):
            pg.update_work_item_description(store, 999_999_999, "Valid scope")
        assert pg.get_work_item(store, iid)["description"] == "Keep scope"

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

    def test_status_cas_rejects_stale_basis_without_row_or_event_effect(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Cas-{_uid()}")
        item = pg.get_work_item(store, iid)
        assert item is not None
        basis = db.item_status_revision(item)

        pg.set_work_item_status(store, iid, "active", expected_revision=basis)
        events_after_accept = pg.list_events(store, sprint_id)
        with pytest.raises(db.StatusConflict, match="status revision mismatch"):
            pg.set_work_item_status(store, iid, "done", expected_revision=basis)

        assert pg.get_work_item(store, iid)["status"] == "active"
        assert pg.list_events(store, sprint_id) == events_after_accept

    def test_two_connections_accept_exactly_one_status_cas_writer(
        self, store, sprint_id, track_id
    ):
        iid = pg.create_work_item(store, sprint_id, track_id, f"Race-{_uid()}")
        item = pg.get_work_item(store, iid)
        assert item is not None
        basis = db.item_status_revision(item)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def transition() -> None:
            conn = psycopg.connect(_PG_URL, row_factory=dict_row)
            assert_disposable_connection(conn)
            independent = pg.PgStore(conn=conn, repo_id=store.repo_id)
            try:
                barrier.wait(timeout=10)
                try:
                    pg.set_work_item_status(
                        independent, iid, "active", expected_revision=basis
                    )
                except db.StatusConflict:
                    outcomes.append("conflict")
                else:
                    outcomes.append("accepted")
            finally:
                conn.close()

        threads = [threading.Thread(target=transition) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcomes) == ["accepted", "conflict"]
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
