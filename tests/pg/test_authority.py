"""PostgreSQL integration tests: AuthorityFaultHistories, AuthorityCommandArbitration.

Split from tests/test_pg_integration.py (P4.2); see tests/pg/_shared.py for the shared
pg_test_scope/store fixtures (registered for this directory by tests/pg/conftest.py),
skip machinery, and helpers.
"""
from __future__ import annotations

import pytest

from tests.pg._shared import (
    authority,
    contracts,
    pg,
    projection,
    sync,
    outbox,
    InvalidTransition,
    assert_disposable_connection,
    _uid,
    _authority_repo_uuid,
    _append_authority_command,
    PG_MARKS,
    _PG_URL,
    replace,
    hashlib,
    json,
    uuid,
    psycopg,
    dict_row,
)

pytestmark = PG_MARKS


class TestAuthorityFaultHistories:
    def _independent_store(self, store):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        return pg.PgStore(conn=conn, repo_id=store.repo_id)

    def test_partition_reassignment_then_stale_touch_is_rejected(self, store):
        """A displaced session cannot keep its reservation alive after a takeover.

        The retired claim path proved this with lease expiry and a rejected
        heartbeat. v3 drops the TTL ceremony: an explicit takeover interrupts
        the old row outright, and the partitioned session learns it lost the
        reservation on its next touch rather than by silently renewing a dead
        lease.  The takeover has to be asked for -- the replacement session
        would otherwise have been allowed to reserve alongside the partitioned
        one, and both rows would have stayed active.
        """
        sprint_id = pg.create_sprint(store, f"Partition-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Lease-{_uid()}")
        old = pg.reserve(
            store, item_id, actor="partitioned-owner", session_id="session-partitioned"
        )
        replacement = self._independent_store(store)
        try:
            new = pg.reserve(
                replacement,
                item_id,
                actor="replacement-owner",
                session_id="session-replacement",
                interrupt_existing=True,
            )

            with pytest.raises(ValueError, match="is interrupted"):
                pg.touch_reservation(store, old["id"], session_id="session-partitioned")

            active_ids = {
                row["id"]
                for row in pg.list_reservations(replacement, item_id, active_only=True)
            }
            assert active_ids == {new["id"]}
            history = pg.list_reservations(replacement, item_id, active_only=False)
            by_id = {row["id"]: row for row in history}
            assert set(by_id) == {old["id"], new["id"]}
            assert by_id[old["id"]]["state"] == "interrupted"
            assert by_id[new["id"]]["state"] == "active"
        finally:
            replacement.conn.close()

    def test_stale_item_and_sprint_commands_reject_without_second_mutation(self, store):
        sprint_id = pg.create_sprint(store, f"Stale-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Stale-item-{_uid()}")
        pg.set_work_item_status(store, item_id, "active")
        actor_b = self._independent_store(store)
        try:
            assert pg.get_work_item(actor_b, item_id)["status"] == "active"
            assert pg.get_sprint(actor_b, sprint_id)["status"] == "active"

            pg.set_work_item_status(store, item_id, "done")
            with pytest.raises(InvalidTransition, match="done -> done"):
                pg.set_work_item_status(actor_b, item_id, "done")
            assert pg.get_work_item(actor_b, item_id)["status"] == "done"

            boundary_id = pg.close_sprint_with_boundary_event(store, sprint_id, "actor-a")
            with pytest.raises(InvalidTransition, match="sprint closed -> closed"):
                pg.close_sprint_with_boundary_event(actor_b, sprint_id, "actor-b")
            boundaries = [
                event
                for event in pg.list_events(actor_b, sprint_id)
                if event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
            ]
            assert [event["id"] for event in boundaries] == [boundary_id]
            assert pg.get_sprint(actor_b, sprint_id)["status"] == "closed"
        finally:
            actor_b.conn.close()


# ---------------------------------------------------------------------------
# Durable authority command arbitration
# ---------------------------------------------------------------------------

class TestAuthorityCommandArbitration:
    def _independent_store(self, store):
        conn = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(conn)
        return pg.PgStore(conn=conn, repo_id=store.repo_id)

    def test_request_and_decision_receive_consecutive_repository_offsets(
        self, pg_test_scope, store, tmp_path
    ):
        isolated = pg.PgStore(
            conn=store.conn,
            repo_id=pg_test_scope("cursor-command-pair"),
        )
        isolated.authority_repo_uuid = _authority_repo_uuid(isolated)
        sprint_id = pg.create_sprint(isolated, f"Cursor-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(isolated, sprint_id, "authority")
        item_id = pg.create_work_item(isolated, sprint_id, track_id, "Cursor command")
        item = pg.get_work_item(isolated, item_id)
        producer = outbox.open_outbox(tmp_path / "cursor-command-pair.db")
        try:
            command = _append_authority_command(
                producer,
                isolated,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            decision = authority.arbitrate_command(isolated, command)
        finally:
            producer.close()

        history = pg.list_ingested_records(isolated)
        assert [entry.record.event_id for entry in history] == [
            command.event_id,
            decision.decision_event_id,
        ]
        assert [entry.ingest_offset for entry in history] == [1, 2]
        assert decision.decision_ingest_offset == 2
        assert pg.get_ingest_high_water(isolated) == 2

    def test_authenticated_actor_mismatch_is_durably_rejected_and_consumes_sequence(
        self, store, tmp_path
    ):
        sprint_id = pg.create_sprint(store, f"Actor-mismatch-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, "Actor mismatch command")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "actor-mismatch.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
                actor="stale-actor",
            )
            rejected = authority.arbitrate_command(
                store, command, authenticated_actor="served-actor"
            )
            retried = authority.arbitrate_command(
                store, command, authenticated_actor="served-actor"
            )
        finally:
            producer.close()

        assert rejected.outcome == "rejected"
        assert rejected.reason_code == "actor-mismatch"
        assert retried.to_dict() == {**rejected.to_dict(), "duplicate": True}
        assert pg.get_work_item(store, item_id)["status"] == "pending"

    def test_item_transition_retry_and_stale_rejection_are_durable(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Command-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-item.db")
        try:
            first = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            accepted = authority.arbitrate_command(store, first)
            retried = authority.arbitrate_command(store, first)

            assert accepted.accepted is True
            assert accepted.decision_type == "item.transitioned"
            assert retried.to_dict() == {**accepted.to_dict(), "duplicate": True}
            assert pg.get_work_item(store, item_id)["status"] == "active"

            stale = _append_authority_command(
                producer,
                store,
                record_type="item.done",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "done"},
            )
            rejected = authority.arbitrate_command(store, stale)

            assert rejected.accepted is False
            assert rejected.decision_type == "command.rejected"
            assert rejected.reason_code == "stale-basis"
            assert pg.get_work_item(store, item_id)["status"] == "active"
            assert [decision.outcome for decision in authority.list_authority_decisions(store)][-2:] == [
                "accepted",
                "rejected",
            ]

            current = pg.get_work_item(store, item_id)
            done = _append_authority_command(
                producer,
                store,
                record_type="item.done",
                aggregate_type="item",
                aggregate_uuid=current["aggregate_uuid"],
                basis_revision=authority.item_revision(current),
                payload={"to_status": "done"},
            )
            completed = authority.arbitrate_command(store, done)
            assert completed.accepted is True
            assert completed.effect["status"] == "done"
            assert pg.get_work_item(store, item_id)["status"] == "done"
        finally:
            producer.close()

    def test_repository_uuid_mismatch_is_durably_rejected(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Repo-mismatch-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Repo-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-repo-mismatch.db")
        try:
            command = contracts.AuthorityCommand(
                event_id=str(uuid.uuid4()),
                record_type="item.transition",
                schema_version="1",
                actor="wrong-repository",
                authored_at="2026-07-14T18:00:00Z",
                refs={
                    "repo_id": str(uuid.uuid4()),
                    "aggregate_type": "item",
                    "aggregate_uuid": item["aggregate_uuid"],
                },
                payload={"to_status": "active"},
                basis_revision=authority.item_revision(item),
            )
            durable = outbox.append_authority_command(producer, command)
            decision = authority.arbitrate_command(store, durable)
            assert decision.accepted is False
            assert decision.reason_code == "repository-mismatch"
            assert pg.get_work_item(store, item_id)["status"] == "pending"
        finally:
            producer.close()

    def test_arbitrate_command_succeeds_without_a_committed_repository_uuid(
        self, pg_test_scope, store, tmp_path
    ):
        """Regression test for the served (Vuoro work-adapter) composition
        path, which never sets ``authority_repo_uuid`` -- there is no
        server-side repo-UUID registry to populate it from, because served
        callers are already tenant-isolated by identity before
        WorkApplication.invoke ever runs (see vuoro_service.composition).

        Before this fix, ``_apply_command`` treated an unset
        authority_repo_uuid as a hard `AuthorityProtocolError`, which meant
        every served item/sprint/claim authority command failed
        unconditionally -- discovered 2026-07-24 while reconciling sprintctl
        #1220/#1221, which had been silently blocked by this since the
        served work.lifecycle.arbitrate route shipped in #1195. Every other
        test in this class supplies a matching authority_repo_uuid on both
        sides and would not have caught this.
        """
        isolated = pg.PgStore(
            conn=store.conn,
            repo_id=pg_test_scope("served-no-committed-uuid"),
        )
        assert isolated.authority_repo_uuid is None
        sprint_id = pg.create_sprint(isolated, f"Served-uuid-{_uid()}", status="active")
        track_id = pg.get_or_create_track(isolated, sprint_id, "authority")
        item_id = pg.create_work_item(isolated, sprint_id, track_id, "Served item")
        item = pg.get_work_item(isolated, item_id)
        producer = outbox.open_outbox(tmp_path / "served-no-committed-uuid.db")
        try:
            command = contracts.AuthorityCommand(
                event_id=str(uuid.uuid4()),
                record_type="item.transition",
                schema_version="1",
                actor="served-client",
                authored_at="2026-07-24T18:00:00Z",
                refs={
                    # A served client has no committed authority UUID of its
                    # own either; any UUID-shaped value is accepted here
                    # since the mismatch check is skipped when the store has
                    # nothing to check it against (see isolated above).
                    "repo_id": str(uuid.uuid4()),
                    "aggregate_type": "item",
                    "aggregate_uuid": item["aggregate_uuid"],
                },
                payload={"to_status": "active"},
                basis_revision=authority.item_revision(item),
            )
            durable = outbox.append_authority_command(producer, command)
            decision = authority.arbitrate_command(isolated, durable)
        finally:
            producer.close()

        assert decision.accepted is True
        assert pg.get_work_item(isolated, item_id)["status"] == "active"

    def test_malformed_embedded_command_is_durably_rejected(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Malformed-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Malformed-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-malformed.db")
        try:
            valid = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            malformed_payload = {"malformed": True}
            encoded = json.dumps(
                malformed_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            malformed = replace(
                valid,
                payload=malformed_payload,
                payload_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
            )

            decision = authority.arbitrate_command(store, malformed)
            assert decision.accepted is False
            assert decision.reason_code == "invalid-command"
            assert pg.get_work_item(store, item_id)["status"] == "pending"
            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM ingest_record "
                    "WHERE repo_id = %s AND event_id IN (%s, %s)",
                    (store.repo_id, malformed.event_id, decision.decision_event_id),
                )
                assert cur.fetchone()["count"] == 2
        finally:
            producer.close()

    def test_sync_stops_at_pending_command_then_resumes_stream_in_order(
        self, store, tmp_path
    ):
        sprint_id = pg.create_sprint(store, f"Pending-sync-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Pending-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-pending-sync.db")
        cache = projection.open_cached_projection(tmp_path / "authority-pending-cache.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="item.transition",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "active"},
            )
            observation = outbox.append_observation(
                producer,
                event_type="work.completed",
                actor="producer-after-command",
                payload={"item_id": item_id},
                occurred_at="2026-07-14T18:00:01Z",
            )

            pending = sync.synchronize_outbox(
                producer,
                store,
                cache,
                apply_ingest_projection=False,
            )
            assert pending.pending_command_event_ids == (command.event_id,)
            assert pending.uploaded == ()
            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM ingest_record "
                    "WHERE repo_id = %s AND event_id IN (%s, %s)",
                    (store.repo_id, command.event_id, observation.event_id),
                )
                assert cur.fetchone()["count"] == 0

            resumed = sync.synchronize_outbox(
                producer,
                store,
                cache,
                credential_resolver=lambda _record: {},
                apply_ingest_projection=False,
            )
            assert [decision.request_event_id for decision in resumed.command_decisions] == [
                command.event_id
            ]
            assert [result.record.event_id for result in resumed.uploaded] == [
                observation.event_id
            ]
            assert resumed.pending_command_event_ids == ()
            assert pg.get_work_item(store, item_id)["status"] == "active"
        finally:
            producer.close()
            cache.close()

    def test_sprint_activate_is_remotely_arbitrated(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Activate-command-{_uid()}", status="planned")
        sprint = pg.get_sprint(store, sprint_id)
        producer = outbox.open_outbox(tmp_path / "authority-activate.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="sprint.activate",
                aggregate_type="sprint",
                aggregate_uuid=sprint["aggregate_uuid"],
                basis_revision=authority.sprint_revision(sprint),
                payload={},
            )
            decision = authority.arbitrate_command(store, command)
            assert decision.accepted is True
            assert decision.decision_type == "sprint-activated"
            assert pg.get_sprint(store, sprint_id)["status"] == "active"
        finally:
            producer.close()

    def test_decision_insert_failure_rolls_back_request_and_effect(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Atomic-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Atomic-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-atomic.db")
        suffix = uuid.uuid4().hex
        function_name = f"reject_authority_decision_{suffix}"
        trigger_name = f"reject_authority_decision_{suffix}"
        command = _append_authority_command(
            producer,
            store,
            record_type="item.transition",
            aggregate_type="item",
            aggregate_uuid=item["aggregate_uuid"],
            basis_revision=authority.item_revision(item),
            payload={"to_status": "active"},
        )
        try:
            with store.conn.cursor() as cur:
                cur.execute(
                    f"CREATE FUNCTION {function_name}() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'injected decision failure'; END $$"
                )
                cur.execute(
                    f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON authority_decision "
                    f"FOR EACH ROW WHEN (NEW.repo_id = '{store.repo_id}') "
                    f"EXECUTE FUNCTION {function_name}()"
                )
            store.conn.commit()

            with pytest.raises(psycopg.Error, match="injected decision failure"):
                authority.arbitrate_command(store, command)

            assert pg.get_work_item(store, item_id)["status"] == "pending"
            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM ingest_record "
                    "WHERE repo_id = %s AND event_id = %s",
                    (store.repo_id, command.event_id),
                )
                assert cur.fetchone()["count"] == 0
                cur.execute(
                    "SELECT count(*) AS count FROM authority_decision "
                    "WHERE repo_id = %s AND request_event_id = %s",
                    (store.repo_id, command.event_id),
                )
                assert cur.fetchone()["count"] == 0
        finally:
            store.conn.rollback()
            with store.conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON authority_decision")
                cur.execute(f"DROP FUNCTION IF EXISTS {function_name}()")
            store.conn.commit()
            producer.close()

    def test_sprint_close_boundary_and_decision_commit_atomically(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Close-command-{_uid()}", status="active")
        sprint = pg.get_sprint(store, sprint_id)
        producer = outbox.open_outbox(tmp_path / "authority-close.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="sprint.close",
                aggregate_type="sprint",
                aggregate_uuid=sprint["aggregate_uuid"],
                basis_revision=authority.sprint_revision(sprint),
                payload={},
            )
            decision = authority.arbitrate_command(store, command)
            retried = authority.arbitrate_command(store, command)

            assert decision.accepted is True
            assert decision.decision_type == "sprint-closed"
            assert retried.duplicate is True
            assert pg.get_sprint(store, sprint_id)["status"] == "closed"
            boundaries = [
                event
                for event in pg.list_events(store, sprint_id)
                if event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
            ]
            assert [event["id"] for event in boundaries] == [decision.effect["boundary_event_id"]]
        finally:
            producer.close()

    def _active_item(self, store, label):
        sprint_id = pg.create_sprint(store, f"{label}-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "decisions")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"{label} item")
        pg.set_work_item_status(store, item_id, "active")
        return sprint_id, track_id, item_id

    def test_old_client_item_done_becomes_an_accept_decision(self, store, tmp_path):
        _sprint_id, _track_id, item_id = self._active_item(store, "Old-client-done")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-done.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="item.done",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={"to_status": "done"},
            )
            decision = authority.arbitrate_command(store, command)
        finally:
            producer.close()

        assert decision.accepted is True
        assert decision.decision_type == "item.transitioned"
        assert decision.effect["status"] == "done"
        assert decision.effect["resolution"] == "accepted"
        assert decision.effect["decision_kind"] == "accept"
        closed = pg.get_work_item(store, item_id)
        [recorded] = pg.list_decisions(store, item_id)
        assert recorded["kind"] == "accept"
        assert recorded["actor"] == "authority-test"
        assert closed["status"] == "done"
        assert closed["resolution"] == "accepted"
        assert closed["terminal_decision_id"] == recorded["id"]
        assert closed["legacy"] is False

    def test_decision_record_supersedes_by_aggregate_uuid(self, store, tmp_path):
        sprint_id, track_id, item_id = self._active_item(store, "Decision-record")
        replacement_id = pg.create_work_item(store, sprint_id, track_id, "replacement")
        item = pg.get_work_item(store, item_id)
        replacement = pg.get_work_item(store, replacement_id)
        producer = outbox.open_outbox(tmp_path / "authority-decision.db")
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="decision.record",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "kind": "supersede",
                    "rationale": "split into a narrower item",
                    "evidence_digests": ["a" * 64],
                    "superseded_by_aggregate_uuid": replacement["aggregate_uuid"],
                },
            )
            decision = authority.arbitrate_command(store, command)
            closed = pg.get_work_item(store, item_id)
            again = _append_authority_command(
                producer,
                store,
                record_type="decision.record",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(closed),
                payload={"kind": "withdraw"},
            )
            refused = authority.arbitrate_command(store, again)
        finally:
            producer.close()

        assert decision.accepted is True
        assert decision.decision_type == "work-decision.recorded"
        assert decision.effect["superseded_by_item_id"] == replacement_id
        assert closed["resolution"] == "superseded"
        [recorded] = pg.list_decisions(store, item_id)
        assert recorded["superseded_by_item_id"] == replacement_id
        assert recorded["evidence_digests"] == ["a" * 64]
        assert refused.accepted is False
        assert refused.reason_code == "invalid-transition"
        assert len(pg.list_decisions(store, item_id)) == 1

    def test_retired_capability_receipt_commands_are_refused(self, store):
        sprint_id = pg.create_sprint(store, f"Retired-receipt-{_uid()}", status="active")
        sprint = pg.get_sprint(store, sprint_id)
        for record_type in ("capability-receipt.accept", "capability-receipt.accepted"):
            with pytest.raises(ValueError, match="not classified"):
                contracts.AuthorityCommand(
                    event_id=str(uuid.uuid4()),
                    record_type=record_type,
                    schema_version="1",
                    actor="authority-test",
                    authored_at="2026-07-14T18:00:00Z",
                    refs={
                        "repo_id": _authority_repo_uuid(store),
                        "aggregate_type": "sprint",
                        "aggregate_uuid": sprint["aggregate_uuid"],
                    },
                    payload={"pointer": {}},
                    basis_revision="event:1",
                    correlation_id=str(uuid.uuid4()),
                )


# ---------------------------------------------------------------------------
# Takeup
# ---------------------------------------------------------------------------
