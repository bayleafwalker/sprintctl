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
    _receipt_bytes,
    _receipt_payload,
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

    def test_partition_expiry_reassignment_then_stale_heartbeat_is_rejected(
        self,
        store,
    ):
        sprint_id = pg.create_sprint(store, f"Partition-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "protocol")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Lease-{_uid()}")
        old_claim_id = pg.create_claim(store, item_id, "partitioned-owner")
        old_claim = pg.get_claim(store, old_claim_id, include_secret=True)
        replacement = self._independent_store(store)
        try:
            with replacement.conn.cursor() as cur:
                cur.execute(
                    "UPDATE claim SET expires_at = now() - interval '1 second' "
                    "WHERE repo_id = %s AND id = %s",
                    (store.repo_id, old_claim_id),
                )
            replacement.conn.commit()
            assert pg.list_claims(store, item_id, active_only=True) == []

            new_claim_id = pg.create_claim(replacement, item_id, "replacement-owner")
            with pytest.raises(ValueError, match="expired and is no longer active"):
                pg.heartbeat_claim(
                    store,
                    old_claim_id,
                    old_claim["claim_token"],
                    actor="partitioned-owner",
                )

            active_ids = {
                claim["claim_id"] for claim in pg.list_claims(replacement, item_id, active_only=True)
            }
            assert active_ids == {new_claim_id}
            history = pg.list_claims(replacement, item_id, active_only=False)
            assert [claim["claim_id"] for claim in history] == [old_claim_id, new_claim_id]
            assert [claim["status"] for claim in history] == ["expired", "active"]
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

    def test_expired_claim_cannot_be_revived_after_reassignment(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Claim-command-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Claim-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-claim.db")
        old_token = "old-" + uuid.uuid4().hex
        new_token = "new-" + uuid.uuid4().hex
        try:
            first = _append_authority_command(
                producer,
                store,
                record_type="claim.acquire",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "agent": "old-owner",
                    "claim_type": "execute",
                    "exclusive": True,
                    "ttl_seconds": 300,
                    "credential_ref": authority.credential_ref(old_token),
                    "metadata": {},
                },
            )
            granted = authority.arbitrate_command(
                store,
                first,
                credentials={authority.credential_ref(old_token): old_token},
            )
            old_claim_id = granted.effect["claim_id"]
            with store.conn.cursor() as cur:
                cur.execute(
                    "UPDATE claim SET expires_at = now() - interval '1 second' "
                    "WHERE repo_id = %s AND id = %s",
                    (store.repo_id, old_claim_id),
                )
            store.conn.commit()
            expired = pg.get_claim(store, old_claim_id, include_secret=True)

            replacement = _append_authority_command(
                producer,
                store,
                record_type="claim.acquire",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "agent": "new-owner",
                    "claim_type": "execute",
                    "exclusive": True,
                    "ttl_seconds": 300,
                    "credential_ref": authority.credential_ref(new_token),
                    "metadata": {},
                },
            )
            replacement_grant = authority.arbitrate_command(
                store,
                replacement,
                credentials={authority.credential_ref(new_token): new_token},
            )
            assert replacement_grant.accepted is True

            stale_renew = _append_authority_command(
                producer,
                store,
                record_type="claim.renew",
                aggregate_type="claim",
                claim_id=old_claim_id,
                basis_revision=authority.claim_revision(expired),
                payload={
                    "claim_id": old_claim_id,
                    "ttl_seconds": 300,
                    "credential_ref": authority.credential_ref(old_token),
                },
            )
            rejected = authority.arbitrate_command(
                store,
                stale_renew,
                credentials={authority.credential_ref(old_token): old_token},
            )

            assert rejected.accepted is False
            assert rejected.reason_code == "expired-grant"
            active_claims = pg.list_claims(store, item_id, active_only=True)
            assert [claim["claim_id"] for claim in active_claims] == [
                replacement_grant.effect["claim_id"]
            ]
        finally:
            producer.close()

    def test_claim_lifecycle_decisions_are_secret_safe(self, store, tmp_path):
        sprint_id = pg.create_sprint(store, f"Claim-lifecycle-{_uid()}", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "authority")
        item_id = pg.create_work_item(store, sprint_id, track_id, f"Claim-life-item-{_uid()}")
        item = pg.get_work_item(store, item_id)
        producer = outbox.open_outbox(tmp_path / "authority-claim-lifecycle.db")
        first_token = "first-" + uuid.uuid4().hex
        rotated_token = "rotated-" + uuid.uuid4().hex
        first_ref = authority.credential_ref(first_token)
        rotated_ref = authority.credential_ref(rotated_token)
        try:
            acquire = _append_authority_command(
                producer,
                store,
                record_type="claim.acquire",
                aggregate_type="item",
                aggregate_uuid=item["aggregate_uuid"],
                basis_revision=authority.item_revision(item),
                payload={
                    "agent": "owner-a",
                    "claim_type": "execute",
                    "exclusive": True,
                    "ttl_seconds": 300,
                    "credential_ref": first_ref,
                    "metadata": {"runtime_session_id": "session-a"},
                },
            )
            granted = authority.arbitrate_command(
                store, acquire, credentials={first_ref: first_token}
            )
            claim_id = granted.effect["claim_id"]

            claim = pg.get_claim(store, claim_id, include_secret=True)
            renew = _append_authority_command(
                producer,
                store,
                record_type="claim.renew",
                aggregate_type="claim",
                claim_id=claim_id,
                basis_revision=authority.claim_revision(claim),
                payload={
                    "claim_id": claim_id,
                    "ttl_seconds": 600,
                    "credential_ref": first_ref,
                },
            )
            renewed = authority.arbitrate_command(
                store, renew, credentials={first_ref: first_token}
            )
            assert renewed.decision_type == "claim.renewed"

            claim = pg.get_claim(store, claim_id, include_secret=True)
            handoff = _append_authority_command(
                producer,
                store,
                record_type="claim.handoff",
                aggregate_type="claim",
                claim_id=claim_id,
                basis_revision=authority.claim_revision(claim),
                payload={
                    "claim_id": claim_id,
                    "to_actor": "owner-b",
                    "mode": "rotate",
                    "ttl_seconds": 600,
                    "credential_ref": first_ref,
                    "proposed_credential_ref": rotated_ref,
                    "metadata": {"runtime_session_id": "session-b"},
                },
            )
            handed_off = authority.arbitrate_command(
                store,
                handoff,
                credentials={first_ref: first_token, rotated_ref: rotated_token},
            )
            assert handed_off.decision_type == "claim.handed-off"
            assert handed_off.effect["actor"] == "owner-b"

            claim = pg.get_claim(store, claim_id, include_secret=True)
            release = _append_authority_command(
                producer,
                store,
                record_type="claim.release",
                aggregate_type="claim",
                claim_id=claim_id,
                basis_revision=authority.claim_revision(claim),
                payload={"claim_id": claim_id, "credential_ref": rotated_ref},
            )
            released = authority.arbitrate_command(
                store, release, credentials={rotated_ref: rotated_token}
            )
            assert released.decision_type == "claim.released"
            assert released.effect["released"] is True
            assert pg.get_claim(store, claim_id, include_secret=True) is None

            with store.conn.cursor() as cur:
                cur.execute(
                    "SELECT payload::text FROM ingest_record WHERE repo_id = %s",
                    (store.repo_id,),
                )
                durable_text = "\n".join(row["payload"] for row in cur.fetchall())
                cur.execute(
                    "SELECT effect::text || coalesce(reason_detail, '') AS text "
                    "FROM authority_decision WHERE repo_id = %s",
                    (store.repo_id,),
                )
                durable_text += "\n" + "\n".join(row["text"] for row in cur.fetchall())
            assert first_token not in durable_text
            assert rotated_token not in durable_text
        finally:
            producer.close()

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

    def test_capability_receipt_acceptance_is_a_remote_decision(
        self, store, tmp_path, monkeypatch
    ):
        sprint_id = pg.create_sprint(store, f"Receipt-command-{_uid()}", status="active")
        boundary_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        sprint = pg.get_sprint(store, sprint_id)
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_id)
        pointer = _receipt_payload(store, receipt_bytes)
        producer = outbox.open_outbox(tmp_path / "authority-receipt.db")
        monkeypatch.setattr(
            contracts,
            "verify_capability_receipt_draft_pointer",
            lambda payload, *, sprint_id, boundary_event_id: payload,
        )
        try:
            command = _append_authority_command(
                producer,
                store,
                record_type="capability-receipt.accept",
                aggregate_type="sprint",
                aggregate_uuid=sprint["aggregate_uuid"],
                basis_revision=f"event:{boundary_id}",
                payload={"pointer": pointer},
            )
            decision = authority.arbitrate_command(store, command)
            assert decision.accepted is True
            assert decision.decision_type == "capability-receipt.accepted"
            assert decision.effect["boundary_revision"] == f"event:{boundary_id}"
            event_types = [event["event_type"] for event in pg.list_events(store, sprint_id)]
            assert event_types == [
                contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE,
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
            ]
        finally:
            producer.close()

    def test_unavailable_capability_artifact_rejects_pointer_without_event(
        self,
        store,
        monkeypatch,
    ):
        sprint_id = pg.create_sprint(store, f"Artifact-{_uid()}", status="active")
        boundary_id = pg.close_sprint_with_boundary_event(store, sprint_id, "operator")
        receipt_bytes = _receipt_bytes(store, sprint_id, boundary_id)
        payload = _receipt_payload(store, receipt_bytes)

        def missing(receipt_path):
            raise ValueError(f"capability receipt file does not exist: {receipt_path}")

        monkeypatch.setattr(contracts, "_read_capability_receipt_bytes", missing)
        drafting_agent = self._independent_store(store)
        try:
            with pytest.raises(ValueError, match="file does not exist"):
                pg.create_event(
                    drafting_agent,
                    sprint_id,
                    "drafting-agent",
                    contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                    payload=payload,
                )
            event_types = [event["event_type"] for event in pg.list_events(store, sprint_id)]
            assert event_types == [contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE]
        finally:
            drafting_agent.conn.close()


# ---------------------------------------------------------------------------
# Takeup
# ---------------------------------------------------------------------------
