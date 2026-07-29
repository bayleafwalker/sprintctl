"""Depth-2 PostgreSQL parity/fault tests for terminal recovery server semantics."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from sprintctl import authority, contracts, outbox, pg
from sprintctl.pg_testing import assert_disposable_connection, cleanup_test_repositories, new_test_repo_uuid
from sprintctl.terminal_recovery_contract import (
    TerminalDisposition, TerminalRecoveryRequest, VerifiedRecoveryCapability,
)
from sprintctl.terminal_recovery_server import (
    append_recovery_audit, append_terminal_settlement, configured_terminal_recovery_adapter,
)


PG_URL = os.environ.get("SPRINTCTL_TEST_PG_URL")
pytestmark = [pytest.mark.pg, pytest.mark.skipif(not PG_URL, reason="SPRINTCTL_TEST_PG_URL not set")]
AUDIT = "ad:01ARZ3NDEKTSV4RRFFQ69G5FAV"


class Verifier:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def verify_online(self, request):
        if self.fail:
            raise OSError("identity authority unavailable")
        return VerifiedRecoveryCapability(
            capability_ref=request.recovery_capability_ref,
            subject_id="coordinator:terminal-recovery",
            repo_id=request.repo_id,
            claim_id=request.claim_id,
            terminal_request_id=request.terminal_request_id,
            terminal_disposition=request.terminal_disposition,
            expected_lease_epoch=request.expected_lease_epoch,
        )


def request(repo_id, **changes):
    values = dict(
        repo_id=repo_id, claim_id=17, terminal_request_id=str(uuid4()),
        expected_lease_epoch=3, terminal_disposition=TerminalDisposition.CLAIM_RELEASE,
        terminal_request_digest="a" * 64,
        recovery_capability_ref=f"capref:{uuid4()}",
        incident_audit_ref=AUDIT, operator_approval_audit_ref=AUDIT,
    )
    values.update(changes)
    return TerminalRecoveryRequest(**values)


def test_postgres_exact_replay_survives_lineage_and_audit_is_at_most_once():
    from psycopg import connect
    from psycopg.rows import dict_row
    conn = connect(PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = new_test_repo_uuid()
    store = pg.PgStore(conn=conn, repo_id=repo_id)
    try:
        pg.init_db(store)
        sprint_id = pg.create_sprint(store, "Terminal recovery", "", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "recovery")
        item_id = pg.create_work_item(store, sprint_id, track_id, "settled terminal request")
        claim = pg.create_claim(store, item_id, "coordinator:terminal-recovery", ttl_seconds=300)
        # Rotate the lease twice, then remove it.  Historical replay must not
        # consult this later lineage state to overturn the original decision.
        for _ in range(2):
            claim = pg.handoff_claim(
                store, claim["id"], claim["claim_token"], actor="coordinator:successor",
                mode="rotate",
            )
        pg.release_claim(store, claim["id"], claim["claim_token"])
        original = request(
            repo_id, claim_id=claim["id"], expected_lease_epoch=claim["lease_epoch"],
        )
        decision_id, event_id = str(uuid4()), str(uuid4())
        append_terminal_settlement(
            store, original, decision_id=decision_id, terminal_event_id=event_id,
            resulting_item_state="done",
        )
        adapter = configured_terminal_recovery_adapter(store, Verifier())
        assert adapter is not None
        first = adapter.recover_terminal(original, authenticated_coordinator_principal="coordinator:terminal-recovery")
        second = adapter.recover_terminal(original, authenticated_coordinator_principal="coordinator:terminal-recovery")
        assert first.result == second.result == "settled"
        assert first.decision_id == decision_id
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM terminal_recovery_audit WHERE repo_id = %s", (repo_id,))
            assert cur.fetchone()["n"] == 1
    finally:
        cleanup_test_repositories(conn, [repo_id])
        conn.close()


def test_postgres_identity_fault_is_unavailable_before_ledger_or_claim_io(monkeypatch):
    from psycopg import connect
    from psycopg.rows import dict_row
    conn = connect(PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = new_test_repo_uuid()
    store = pg.PgStore(conn=conn, repo_id=repo_id)
    try:
        pg.init_db(store)
        adapter = configured_terminal_recovery_adapter(store, Verifier(fail=True))
        assert adapter is not None
        result = adapter.recover_terminal(request(repo_id), authenticated_coordinator_principal="coordinator:terminal-recovery")
        assert result.result == "unavailable"
        # The fault path must not even create durable recovery evidence.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM terminal_recovery_ledger WHERE repo_id = %s", (repo_id,))
            assert cur.fetchone()["n"] == 0
    finally:
        cleanup_test_repositories(conn, [repo_id])
        conn.close()


def test_authority_terminal_release_writes_ledger_and_rolls_back_atomically(tmp_path):
    from psycopg import connect
    from psycopg.rows import dict_row
    conn = connect(PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = new_test_repo_uuid()
    store = pg.PgStore(conn=conn, repo_id=repo_id, authority_repo_uuid=repo_id)
    producer = outbox.open_outbox(tmp_path / "terminal-authority.db")
    try:
        pg.init_db(store)
        sprint_id = pg.create_sprint(store, "Authority terminal", "", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "recovery")
        item_id = pg.create_work_item(store, sprint_id, track_id, "release")
        claim = pg.create_claim(store, item_id, "coordinator", ttl_seconds=300)
        command = contracts.AuthorityCommand(
            event_id=str(uuid4()), record_type="claim.release", schema_version="1",
            actor="coordinator", authored_at="2026-07-29T00:00:00Z",
            refs={"repo_id": repo_id, "aggregate_type": "claim", "claim_id": claim["id"]},
            payload={"claim_id": claim["id"], "credential_ref": authority.credential_ref(claim["claim_token"])},
            basis_revision=authority.claim_revision(claim), correlation_id=str(uuid4()),
        )
        record = outbox.append_authority_command(producer, command)
        result = authority.arbitrate_command(
            store, record, credentials={command.payload["credential_ref"]: claim["claim_token"]},
        )
        assert result.accepted
        with conn.cursor() as cur:
            cur.execute("SELECT terminal_request_id, claim_id FROM terminal_recovery_ledger WHERE repo_id = %s", (repo_id,))
            assert dict(cur.fetchone()) == {"terminal_request_id": command.event_id, "claim_id": claim["id"]}

        # Helpers do not commit: an enclosing failure rolls back both original
        # settlement and audit evidence together, leaving no partial row.
        aborted = request(repo_id, claim_id=claim["id"], expected_lease_epoch=claim["lease_epoch"])
        with pytest.raises(RuntimeError):
            with conn.transaction():
                append_terminal_settlement(store, aborted, decision_id=str(uuid4()), terminal_event_id=str(uuid4()), resulting_item_state="done")
                append_recovery_audit(store, aborted, coordinator_subject="coordinator:terminal-recovery")
                raise RuntimeError("fault after recovery evidence")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM terminal_recovery_ledger WHERE repo_id = %s", (repo_id,))
            assert cur.fetchone()["n"] == 1
            cur.execute("SELECT count(*) AS n FROM terminal_recovery_audit WHERE repo_id = %s", (repo_id,))
            assert cur.fetchone()["n"] == 0
    finally:
        producer.close()
        cleanup_test_repositories(conn, [repo_id])
        conn.close()


@pytest.mark.parametrize(
    ("record_type", "payload_extra", "expected_disposition"),
    [
        ("claim.release", {}, "claim.release"),
        ("item.done-from-claim", {"keep_claim": False}, "item.done-from-claim"),
        ("item.done", {}, "item.transition.done"),
        ("item.transition", {"to_status": "blocked"}, "item.transition.blocked"),
    ],
)
def test_authority_terminal_disposition_matrix_populates_immutable_ledger(
    tmp_path, record_type, payload_extra, expected_disposition,
):
    from psycopg import connect
    from psycopg.rows import dict_row
    conn = connect(PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = new_test_repo_uuid()
    store = pg.PgStore(conn=conn, repo_id=repo_id, authority_repo_uuid=repo_id)
    producer = outbox.open_outbox(tmp_path / f"terminal-{record_type}.db")
    try:
        pg.init_db(store)
        sprint_id = pg.create_sprint(store, "Matrix", "", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "recovery")
        item_id = pg.create_work_item(store, sprint_id, track_id, record_type)
        # done is only terminal from active in the normal lifecycle graph.
        if record_type == "item.done":
            with conn.cursor() as cur:
                cur.execute("UPDATE work_item SET status = 'active' WHERE repo_id = %s AND id = %s", (repo_id, item_id))
            conn.commit()
        claim = pg.create_claim(store, item_id, "coordinator", ttl_seconds=300)
        item = pg.get_work_item(store, item_id)
        credential_ref = authority.credential_ref(claim["claim_token"])
        if record_type == "claim.release":
            refs = {"repo_id": repo_id, "aggregate_type": "claim", "claim_id": claim["id"]}
            basis = authority.claim_revision(claim)
        else:
            refs = {"repo_id": repo_id, "aggregate_type": "item", "aggregate_uuid": item["aggregate_uuid"]}
            basis = authority.item_revision(item)
        payload = {"claim_id": claim["id"], "credential_ref": credential_ref, **payload_extra}
        command = contracts.AuthorityCommand(
            event_id=str(uuid4()), record_type=record_type, schema_version="1", actor="coordinator",
            authored_at="2026-07-29T00:00:00Z", refs=refs, payload=payload,
            basis_revision=basis, correlation_id=str(uuid4()),
        )
        decision = authority.arbitrate_command(
            store, outbox.append_authority_command(producer, command),
            credentials={credential_ref: claim["claim_token"]},
        )
        assert decision.accepted
        with conn.cursor() as cur:
            cur.execute("SELECT terminal_disposition, lease_epoch FROM terminal_recovery_ledger WHERE repo_id = %s AND terminal_request_id = %s", (repo_id, command.event_id))
            assert dict(cur.fetchone()) == {"terminal_disposition": expected_disposition, "lease_epoch": claim["lease_epoch"]}
    finally:
        producer.close()
        cleanup_test_repositories(conn, [repo_id])
        conn.close()


def test_authority_ledger_failure_rolls_back_terminal_settlement(tmp_path, monkeypatch):
    """An injected ledger fault rolls back the normal authority decision too."""
    from psycopg import connect
    from psycopg.rows import dict_row
    conn = connect(PG_URL, row_factory=dict_row)
    assert_disposable_connection(conn)
    repo_id = new_test_repo_uuid()
    store = pg.PgStore(conn=conn, repo_id=repo_id, authority_repo_uuid=repo_id)
    producer = outbox.open_outbox(tmp_path / "terminal-fault.db")
    try:
        pg.init_db(store)
        sprint_id = pg.create_sprint(store, "Fault", "", status="active")
        track_id = pg.get_or_create_track(store, sprint_id, "recovery")
        item_id = pg.create_work_item(store, sprint_id, track_id, "release")
        claim = pg.create_claim(store, item_id, "coordinator", ttl_seconds=300)
        ref = authority.credential_ref(claim["claim_token"])
        command = contracts.AuthorityCommand(event_id=str(uuid4()), record_type="claim.release", schema_version="1", actor="coordinator", authored_at="2026-07-29T00:00:00Z", refs={"repo_id": repo_id, "aggregate_type": "claim", "claim_id": claim["id"]}, payload={"claim_id": claim["id"], "credential_ref": ref}, basis_revision=authority.claim_revision(claim), correlation_id=str(uuid4()))
        monkeypatch.setattr(authority, "append_terminal_settlement_from_authority", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger fault")))
        with pytest.raises(RuntimeError, match="ledger fault"):
            authority.arbitrate_command(store, outbox.append_authority_command(producer, command), credentials={ref: claim["claim_token"]})
        assert pg.get_claim(store, claim["id"]) is not None
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM terminal_recovery_ledger WHERE repo_id = %s", (repo_id,))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM terminal_recovery_audit WHERE repo_id = %s", (repo_id,))
            assert cur.fetchone()["n"] == 0
    finally:
        producer.close()
        cleanup_test_repositories(conn, [repo_id])
        conn.close()
