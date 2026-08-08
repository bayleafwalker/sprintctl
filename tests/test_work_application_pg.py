"""Served-work protocol checks against a disposable PostgreSQL authority."""

# ruff: noqa: E402 - sprintctl imports follow the disposable-PG availability gate.

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import replace

import pytest


_PG_URL = os.environ.get("SPRINTCTL_TEST_PG_URL")
try:
    import psycopg
    from psycopg.rows import dict_row

    _PSYCOPG_AVAILABLE = True
except ImportError:
    _PSYCOPG_AVAILABLE = False

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        not _PG_URL or not _PSYCOPG_AVAILABLE,
        reason="disposable SPRINTCTL_TEST_PG_URL and psycopg are required",
    ),
]

from sprintctl import authority, contracts, outbox, pg
from sprintctl.application import (
    ApplicationRejection,
    WorkApplication,
    batch_idempotency_key,
    record_to_dict,
)
from sprintctl.pg_testing import (
    assert_disposable_connection,
    cleanup_test_repositories,
    new_test_repo_id,
    write_cleanup_report,
)
from tests.test_maintenance_capability import AT, CAPABILITY_ID, envelope


@pytest.fixture(scope="module")
def store_factory():
    if not _PG_URL or not _PSYCOPG_AVAILABLE:
        pytest.skip("disposable SPRINTCTL_TEST_PG_URL and psycopg are required")
    administrative = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(administrative)
    repo_ids = set()

    def create(label):
        connection = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(connection)
        repo_id = new_test_repo_id(label)
        repo_ids.add(repo_id)
        store = pg.PgStore(
            conn=connection,
            repo_id=repo_id,
            authority_repo_uuid=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"sprintctl-repo:{repo_id}")
            ),
            connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row),
        )
        pg.init_db(store)
        return store

    try:
        yield create
    finally:
        report = cleanup_test_repositories(administrative, repo_ids)
        if report_path := os.environ.get("SPRINTCTL_TEST_PG_CLEANUP_REPORT"):
            write_cleanup_report(report_path, report)
        administrative.close()


def _context(actor, basis_revision, event_id, *, repo_id=None):
    return type(
        "Context",
        (),
        {
            "identity": type(
                "Identity",
                (),
                {
                    "actor": actor,
                    "environment": "vuoro-dev",
                    "authorities": frozenset({"work:claim", "work:lifecycle"}),
                },
            )(),
            "repo_id": repo_id,
            "request_id": f"request:{event_id}",
            "basis_revision": basis_revision,
            "catalog_revision": "catalog-1",
            "idempotency_requirement": "required",
            "idempotency_key": event_id,
        },
    )()


def _maintenance_context(repo_id: str, request_id: str):
    return type(
        "MaintenanceContext",
        (),
        {
            "identity": type(
                "Identity",
                (),
                {
                    "actor": "operator",
                    "environment": "vuoro-dev",
                    "authorities": frozenset({"work:maintenance"}),
                },
            )(),
            "repo_id": repo_id,
            "request_id": request_id,
            "basis_revision": None,
            "catalog_revision": "catalog-maintenance",
            "idempotency_requirement": "required",
            "idempotency_key": request_id,
            "transient_credentials": None,
        },
    )()


def _command_record(path, command):
    producer = outbox.open_outbox(path)
    try:
        return outbox.append_authority_command(producer, command)
    finally:
        producer.close()


def _application(store, credentials):
    return WorkApplication.postgres(
        store,
        credential_resolver=lambda _context, _record: credentials,
    )


def _claim_command(store, item, actor, token, event_id, *, claim_agent=None):
    reference = authority.credential_ref(token)
    command = contracts.AuthorityCommand(
        event_id=event_id,
        record_type="claim.acquire",
        schema_version="1",
        actor=actor,
        authored_at="2026-07-21T12:00:00Z",
        refs={
            "repo_id": store.authority_repo_uuid,
            "aggregate_type": "item",
            "aggregate_uuid": item["aggregate_uuid"],
        },
        payload={
            "agent": claim_agent or actor,
            "claim_type": "execute",
            "exclusive": True,
            "ttl_seconds": 300,
            "credential_ref": reference,
            "metadata": {},
        },
        basis_revision=authority.item_revision(item),
    )
    return command, {reference: token}


def test_maintenance_application_postgres_replay_and_repo_isolation(
    store_factory, monkeypatch
):
    store_a = store_factory("maintenance-app-a")
    store_b = store_factory("maintenance-app-b")
    app = WorkApplication.postgres(store_a)
    monkeypatch.setattr(WorkApplication, "_maintenance_now", staticmethod(lambda: AT))
    request_id = "77777777-7777-4777-8777-777777777777"
    context_a = _maintenance_context(store_a.repo_id, request_id)
    prepared = app.invoke(
        "work.maintenance.prepare",
        {"capability_id": CAPABILITY_ID, "envelope": envelope()},
        context_a,
    )
    monkeypatch.setattr(
        WorkApplication,
        "_maintenance_now",
        staticmethod(lambda: "2026-08-03T00:00:01Z"),
    )
    replay = app.invoke(
        "work.maintenance.prepare",
        {"capability_id": CAPABILITY_ID, "envelope": envelope()},
        context_a,
    )
    assert prepared["duplicate"] is False
    assert replay["duplicate"] is True

    with pytest.raises(ApplicationRejection) as other_repo:
        app.invoke(
            "work.read.maintenance-capability",
            {"capability_id": CAPABILITY_ID},
            _maintenance_context(
                store_b.repo_id, "88888888-8888-4888-8888-888888888888"
            ),
        )
    assert other_repo.value.code == "maintenance-capability-not-found"

    status_a = app.invoke(
        "work.read.maintenance-capability",
        {"capability_id": CAPABILITY_ID},
        _maintenance_context(
            store_a.repo_id, "99999999-9999-4999-8999-999999999999"
        ),
    )
    assert status_a["repo_id"] == store_a.repo_id
    assert status_a["capability"]["state"] == "prepared"
    store_a.conn.close()
    store_b.conn.close()


def _renew_command(store, claim, token, event_id, *, metadata=None):
    reference = authority.credential_ref(token)
    payload = {
        "claim_id": claim["id"],
        "ttl_seconds": 900,
        "credential_ref": reference,
    }
    if metadata is not None:
        payload["metadata"] = metadata
    command = contracts.AuthorityCommand(
        event_id=event_id,
        record_type="claim.renew",
        schema_version="1",
        actor=claim["agent"],
        authored_at="2026-07-21T12:00:00Z",
        refs={
            "repo_id": store.authority_repo_uuid,
            "aggregate_type": "claim",
            "claim_id": claim["id"],
        },
        payload=payload,
        basis_revision=authority.claim_revision(claim),
    )
    return command, {reference: token}


def _release_command(store, claim, token, event_id):
    reference = authority.credential_ref(token)
    command = contracts.AuthorityCommand(
        event_id=event_id,
        record_type="claim.release",
        schema_version="1",
        actor=claim["agent"],
        authored_at="2026-07-21T12:00:00Z",
        refs={
            "repo_id": store.authority_repo_uuid,
            "aggregate_type": "claim",
            "claim_id": claim["id"],
        },
        payload={
            "claim_id": claim["id"],
            "credential_ref": reference,
        },
        basis_revision=authority.claim_revision(claim),
    )
    return command, {reference: token}


def _handoff_command(
    store,
    claim,
    *,
    actor,
    to_actor,
    token,
    event_id,
    mode="rotate",
    proposed_token=None,
    metadata=None,
    note=None,
):
    reference = authority.credential_ref(token)
    payload = {
        "claim_id": claim["id"],
        "to_actor": to_actor,
        "mode": mode,
        "ttl_seconds": 900,
        "credential_ref": reference,
        "metadata": metadata or {},
    }
    credentials = {reference: token}
    if mode == "rotate":
        proposed_reference = authority.credential_ref(proposed_token)
        payload["proposed_credential_ref"] = proposed_reference
        credentials[proposed_reference] = proposed_token
    if note is not None:
        payload["note"] = note
    command = contracts.AuthorityCommand(
        event_id=event_id,
        record_type="claim.handoff",
        schema_version="1",
        actor=actor,
        authored_at="2026-07-21T12:00:00Z",
        refs={
            "repo_id": store.authority_repo_uuid,
            "aggregate_type": "claim",
            "claim_id": claim["id"],
        },
        payload=payload,
        basis_revision=authority.claim_revision(claim),
    )
    return command, credentials


def test_item_note_records_an_event_bound_to_the_authenticated_actor(store_factory):
    store = store_factory("item-note")
    sprint_id = pg.create_sprint(store, "Notes", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Note target")
    app = _application(store, {})

    context = _context("authenticated-actor", None, "note-1")
    result = app.invoke(
        "work.item.note",
        {
            "item_id": item_id,
            "note_type": "decision",
            "summary": "Chose the served path",
            "tags": ["served"],
        },
        context,
    )

    assert result["item_id"] == item_id
    assert result["note_type"] == "decision"
    events = pg.list_events(store, sprint_id)
    recorded = next(e for e in events if e["id"] == result["event_id"])
    assert recorded["event_type"] == "decision"
    assert recorded["actor"] == "authenticated-actor"
    assert recorded["work_item_id"] == item_id
    store.conn.close()


def test_invoke_scopes_to_the_identitys_repo_id_not_the_application_constructor(
    store_factory,
):
    """One long-lived WorkApplication must be able to serve either tenant.

    ``WorkApplication.postgres(store_a)`` is bound to ``store_a`` at
    construction (matching real composition, which builds one application
    per process from ``VUORO_WORK_REPOSITORY_ID``). An identity whose
    ``repo_id`` names a *different* repository must still be served from
    that repository -- this is the sprintctl #1245 contract that unblocks
    vuoro-shared from being pinned to a single repo tenant.
    """

    store_a = store_factory("cross-tenant-a")
    store_b = store_factory("cross-tenant-b")
    sprint_a = pg.create_sprint(store_a, "Repo A sprint", status="active")
    sprint_b = pg.create_sprint(store_b, "Repo B sprint", status="active")

    app = _application(store_a, {})

    # No repo_id on the identity falls back to the application's own
    # construction-time repo_id -- today's single-tenant behavior.
    default_context = _context("reader", None, "read-default")
    default_result = app.invoke("work.read.sprints", {}, default_context)
    assert default_result["repo_id"] == store_a.repo_id
    assert {row["id"] for row in default_result["sprints"]} == {sprint_a}

    # An identity bound to the other repo must be served from that repo,
    # even though ``app`` was constructed against store_a.
    other_context = _context(
        "reader", None, "read-other", repo_id=store_b.repo_id
    )
    other_result = app.invoke("work.read.sprints", {}, other_context)
    assert other_result["repo_id"] == store_b.repo_id
    assert {row["id"] for row in other_result["sprints"]} == {sprint_b}

    # The original store's data is untouched and still reachable through
    # the same application instance on a subsequent call.
    again = app.invoke("work.read.sprints", {}, default_context)
    assert again["repo_id"] == store_a.repo_id
    assert {row["id"] for row in again["sprints"]} == {sprint_a}

    store_a.conn.close()
    store_b.conn.close()


def _terminate_backend(connection_factory, target_connection):
    """Terminate one disposable PostgreSQL backend through a sibling session."""
    with target_connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid() AS pid")
        target_pid = cursor.fetchone()["pid"]
    terminator = connection_factory()
    try:
        with terminator.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(%s) AS terminated", (target_pid,)
            )
            assert cursor.fetchone()["terminated"] is True
        terminator.commit()
    finally:
        terminator.close()


def test_admin_shutdown_reconnects_a_served_read_after_real_backend_termination(
    store_factory,
):
    """The served PostgreSQL entry replaces an actually terminated backend."""
    store = store_factory("admin-shutdown-read")
    old_connection = store.conn
    factory = store.connection_factory
    assert factory is not None
    _terminate_backend(factory, old_connection)

    app = WorkApplication.postgres(store)
    result = app.invoke(
        "work.read.sprints", {}, _context("reader", None, None)
    )

    assert result == {"repo_id": store.repo_id, "sprints": []}
    assert store.conn is not old_connection
    assert app.served_runtime_ready() is True
    old_connection.close()
    store.conn.close()


def test_second_admin_shutdown_keeps_served_runtime_not_ready_until_later_recovery(
    store_factory,
):
    """A second real termination returns 503 and leaves no dead connection live."""
    store = store_factory("admin-shutdown-second")
    old_connection = store.conn
    original_factory = store.connection_factory
    assert original_factory is not None
    _terminate_backend(original_factory, old_connection)

    def terminated_replacement():
        replacement = original_factory()
        _terminate_backend(original_factory, replacement)
        return replacement

    store.connection_factory = terminated_replacement
    app = WorkApplication.postgres(store)

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke("work.read.sprints", {}, _context("reader", None, None))

    assert rejected.value.code == "postgres-runtime-unavailable"
    assert rejected.value.http_status == 503
    assert store.conn is None
    assert app.served_runtime_ready() is False

    store.connection_factory = original_factory
    recovered = app.invoke("work.read.sprints", {}, _context("reader", None, None))

    assert recovered == {"repo_id": store.repo_id, "sprints": []}
    assert app.served_runtime_ready() is True
    old_connection.close()
    store.conn.close()


def test_context_aggregate_uses_a_fresh_snapshot_after_a_reused_connection_read(
    store_factory,
):
    """A prior implicit transaction on the service connection is not ours to
    reconfigure or close; the aggregate opens a sibling repeatable-read read."""
    store = store_factory("context-reused-connection")
    sprint_id = pg.create_sprint(store, "Context", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    pg.create_work_item(store, sprint_id, track_id, "Ready")
    store.conn.commit()

    # This is the ordinary non-autocommit reuse case: an earlier read leaves
    # the shared connection in a transaction before the next invocation.
    assert pg.list_sprints(store)
    shared_transaction_status = store.conn.info.transaction_status

    # Exercise the snapshot helper through the database, rather than trusting
    # its SQL string: PostgreSQL must report both required properties.
    with pg.repeatable_read_snapshot(store) as snapshot_store:
        with snapshot_store.conn.cursor() as cursor:
            cursor.execute("SHOW transaction_isolation")
            isolation = cursor.fetchone()["transaction_isolation"]
            cursor.execute("SHOW transaction_read_only")
            read_only = cursor.fetchone()["transaction_read_only"]
    assert isolation == "repeatable read"
    assert read_only == "on"

    result = _application(store, {}).invoke(
        "work.read.context", {"sprint_id": sprint_id}, _context("reader", None, None)
    )

    assert result["contract_version"] == "1"
    assert result["summary"]["ready"] == 1
    assert result["next_action"]["kind"] == "start-ready-item"
    assert store.conn.info.transaction_status == shared_transaction_status
    store.conn.rollback()
    store.conn.close()


def test_sprint_detail_aggregate_uses_a_fresh_snapshot_after_a_reused_connection_read(
    store_factory,
):
    store = store_factory("sprint-detail-reused-connection")
    sprint_id = pg.create_sprint(store, "Detail", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    pg.create_work_item(store, sprint_id, track_id, "Ready")
    store.conn.commit()

    # A shared service connection may already have an implicit transaction;
    # the aggregate must leave it untouched while using its own snapshot.
    assert pg.list_sprints(store)
    shared_transaction_status = store.conn.info.transaction_status

    result = _application(store, {}).invoke(
        "work.read.sprint-detail", {"sprint_id": sprint_id},
        _context("reader", None, None),
    )

    assert result["sprint"]["id"] == sprint_id
    assert result["sprint"]["detail"]["track_health"]["work"]["total"] == 1
    assert store.conn.info.transaction_status == shared_transaction_status
    store.conn.rollback()
    store.conn.close()


def test_snapshot_uses_store_connection_factory_instead_of_redacted_dsn(monkeypatch):
    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def execute(self, statement):
            assert statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

    class Connection:
        def __init__(self):
            self.closed = False

        def transaction(self):
            return Transaction()

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    sibling = Connection()
    store = pg.PgStore(
        conn=object(),
        repo_id="agentops",
        connection_factory=lambda: sibling,
    )
    monkeypatch.setattr(
        pg.psycopg,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("redacted connection DSN must not be reused"),
    )

    with pg.repeatable_read_snapshot(store) as snapshot:
        assert snapshot.conn is sibling
        assert snapshot.connection_factory is store.connection_factory

    assert sibling.closed is True


@pytest.mark.parametrize(
    ("operation", "mismatch", "expected_code"),
    [
        ("work.claim.arbitrate", "nested-actor", "actor-mismatch"),
        ("work.claim.arbitrate", "claim-agent", "claim-agent-mismatch"),
        ("work.batch.apply", "nested-actor", "actor-mismatch"),
        ("work.batch.apply", "claim-agent", "claim-agent-mismatch"),
    ],
)
def test_authenticated_actor_binding_rejects_before_pg_mutation(
    store_factory, tmp_path, operation, mismatch, expected_code
):
    store = store_factory(f"actor-binding-{operation}-{mismatch}")
    sprint_id = pg.create_sprint(store, "Actor binding", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Do not claim")
    item = pg.get_work_item(store, item_id)
    authenticated_actor = "authenticated-worker"
    command_actor = (
        "nested-impersonator" if mismatch == "nested-actor" else authenticated_actor
    )
    claim_agent = "claim-impersonator" if mismatch == "claim-agent" else command_actor
    command, credentials = _claim_command(
        store,
        item,
        command_actor,
        "actor-binding-proof",
        str(uuid.uuid4()),
        claim_agent=claim_agent,
    )
    record = _command_record(tmp_path / f"{operation}-{mismatch}-producer.db", command)
    if mismatch == "nested-actor":
        record = replace(record, actor=authenticated_actor)

    if operation == "work.claim.arbitrate":
        arguments = {"record": record_to_dict(record)}
        key = record.event_id
    else:
        arguments = {"records": [record_to_dict(record)]}
        key = batch_idempotency_key([record])
    context = _context(authenticated_actor, record.basis_revision, key)

    if operation == "work.claim.arbitrate":
        with pytest.raises(ApplicationRejection) as rejected:
            _application(store, credentials).invoke(operation, arguments, context)
        assert rejected.value.code == expected_code
    else:
        # Batch application deliberately lets authority commands reach
        # arbitration so the producer stream receives a durable rejection.
        result = _application(store, credentials).invoke(operation, arguments, context)
        decision = result["results"][0]
        # Authority binds every actor-bearing claim field as one durable
        # actor-mismatch decision; the direct route preserves its more
        # granular pre-backend claim-agent rejection.
        assert decision["reason_code"] == "actor-mismatch"
        assert decision["outcome"] == "rejected"
    assert pg.list_claims(store, item_id, active_only=False) == []
    decisions = authority.list_authority_decisions(store, after_offset=0, limit=None)
    assert len(decisions) == (0 if operation == "work.claim.arbitrate" else 1)
    store.conn.close()


def test_served_claim_start_activates_or_releases_on_dependency_failure(
    store_factory,
):
    store = store_factory("served-claim-start")
    sprint_id = pg.create_sprint(store, "Served claim start", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    ready_id = pg.create_work_item(store, sprint_id, track_id, "Ready")
    blocker_id = pg.create_work_item(store, sprint_id, track_id, "Blocker")
    blocked_id = pg.create_work_item(store, sprint_id, track_id, "Blocked")
    pg.add_dep(store, blocker_id, blocked_id)
    app = _application(store, {})

    started = app.invoke(
        "work.claim.start",
        {
            "item_id": ready_id,
            "ttl_seconds": 900,
            "runtime_session_id": "pg-thread",
            "instance_id": "pg-process",
            "hostname": "pg-host",
            "pid": 4242,
        },
        _context("served-starter", None, None),
    )

    assert started["item_status_before"] == "pending"
    assert started["item_status_after"] == "active"
    assert started["status_transition_applied"] is True
    assert started["claim"]["agent"] == "served-starter"
    assert started["claim_token"] == started["claim"]["claim_token"]
    assert len(pg.list_claims(store, ready_id, active_only=True)) == 1

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke(
            "work.claim.start",
            {"item_id": blocked_id},
            _context("served-starter", None, None),
        )
    assert rejected.value.code == "claim-start-transition-failed"
    assert pg.get_work_item(store, blocked_id)["status"] == "pending"
    assert pg.list_claims(store, blocked_id, active_only=False) == []
    store.conn.close()


def test_served_claim_start_and_arbitrate_reacquire_expired_claims_exactly_once(
    store_factory, tmp_path
):
    store = store_factory("served-expiry-reacquisition")
    sprint_id = pg.create_sprint(store, "Served expiry", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    start_item_id = pg.create_work_item(store, sprint_id, track_id, "Start path")
    app = _application(store, {})

    first_start = app.invoke(
        "work.claim.start",
        {"item_id": start_item_id, "ttl_seconds": 300},
        _context("start-owner", None, str(uuid.uuid4())),
    )
    with store.conn.cursor() as cur:
        cur.execute(
            "UPDATE claim SET expires_at = now() - interval '1 second' "
            "WHERE repo_id = %s AND id = %s",
            (store.repo_id, first_start["claim"]["claim_id"]),
        )
    store.conn.commit()
    assert app.invoke(
        "work.read.item",
        {"item_id": start_item_id},
        _context("reader", None, str(uuid.uuid4())),
    )["active_claims"] == []
    assert pg.list_claims(store, start_item_id) == []

    second_start = app.invoke(
        "work.claim.start",
        {"item_id": start_item_id, "ttl_seconds": 300},
        _context("replacement-owner", None, str(uuid.uuid4())),
    )
    start_history = pg.list_claims(store, start_item_id, active_only=False)
    assert [claim["status"] for claim in start_history] == ["expired", "active"]
    assert [claim["lease_epoch"] for claim in start_history] == [1, 2]
    assert second_start["status_transition_applied"] is False

    arbitrate_item_id = pg.create_work_item(
        store, sprint_id, track_id, "Arbitrate path"
    )
    arbitrate_item = pg.get_work_item(store, arbitrate_item_id)
    old_command, old_credentials = _claim_command(
        store, arbitrate_item, "old-owner", "old-proof", str(uuid.uuid4())
    )
    old_record = _command_record(tmp_path / "old-acquire.db", old_command)
    old_result = _application(store, old_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(old_record)},
        _context("old-owner", old_record.basis_revision, old_record.event_id),
    )
    old_claim_id = old_result["effect"]["claim_id"]
    with store.conn.cursor() as cur:
        cur.execute(
            "UPDATE claim SET expires_at = now() - interval '1 second' "
            "WHERE repo_id = %s AND id = %s",
            (store.repo_id, old_claim_id),
        )
    store.conn.commit()

    current_item = pg.get_work_item(store, arbitrate_item_id)
    new_command, new_credentials = _claim_command(
        store, current_item, "new-owner", "new-proof", str(uuid.uuid4())
    )
    new_record = _command_record(tmp_path / "new-acquire.db", new_command)
    new_result = _application(store, new_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(new_record)},
        _context("new-owner", new_record.basis_revision, new_record.event_id),
    )
    retried = _application(store, new_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(new_record)},
        _context("new-owner", new_record.basis_revision, new_record.event_id),
    )
    history = pg.list_claims(store, arbitrate_item_id, active_only=False)
    assert new_result["outcome"] == "accepted"
    assert retried == {**new_result, "duplicate": True}
    assert [claim["status"] for claim in history] == ["expired", "active"]
    assert [claim["lease_epoch"] for claim in history] == [1, 2]
    assert [claim["claim_id"] for claim in pg.list_claims(store, arbitrate_item_id)] == [
        new_result["effect"]["claim_id"]
    ]

    expired = pg.get_claim(store, old_claim_id, include_secret=True)
    stale_renew, stale_credentials = _renew_command(
        store, expired, "old-proof", str(uuid.uuid4())
    )
    stale_record = _command_record(tmp_path / "stale-renew.db", stale_renew)
    stale_result = _application(store, stale_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(stale_record)},
        _context("old-owner", stale_record.basis_revision, stale_record.event_id),
    )
    assert stale_result["outcome"] == "rejected"
    assert stale_result["reason_code"] == "expired-grant"
    store.conn.close()


def test_concurrent_served_claims_have_one_durable_acceptance(store_factory, tmp_path):
    primary = store_factory("served-claim")
    sprint_id = pg.create_sprint(primary, "Served claims", status="active")
    track_id = pg.get_or_create_track(primary, sprint_id, "work")
    item_id = pg.create_work_item(primary, sprint_id, track_id, "Claim once")
    item = pg.get_work_item(primary, item_id)

    commands = []
    for index, actor in enumerate(("served-a", "served-b"), start=1):
        command, credentials = _claim_command(
            primary, item, actor, f"proof-{actor}", str(uuid.uuid4())
        )
        record = _command_record(tmp_path / f"producer-{index}.db", command)
        commands.append((actor, record, credentials))

    barrier = threading.Barrier(3)
    outcomes = []
    failures = []

    def worker(actor, record, credentials):
        connection = psycopg.connect(_PG_URL, row_factory=dict_row)
        assert_disposable_connection(connection)
        store = pg.PgStore(
            conn=connection,
            repo_id=primary.repo_id,
            authority_repo_uuid=primary.authority_repo_uuid,
        )
        try:
            barrier.wait(timeout=15)
            result = _application(store, credentials).invoke(
                "work.claim.arbitrate",
                {"record": record_to_dict(record)},
                _context(actor, record.basis_revision, record.event_id),
            )
            outcomes.append(result)
        except BaseException as exc:
            failures.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=command, name=command[0])
        for command in commands
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=15)
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert not failures
    assert sorted(result["outcome"] for result in outcomes) == ["accepted", "rejected"]
    assert sorted(result["reason_code"] or "accepted" for result in outcomes) == [
        "accepted",
        "claim-conflict",
    ]
    assert len(pg.list_claims(primary, item_id)) == 1

    accepted = next(result for result in outcomes if result["outcome"] == "accepted")
    accepted_actor, accepted_record, accepted_credentials = next(
        command
        for command in commands
        if command[1].event_id == accepted["request_event_id"]
    )
    retried = _application(primary, accepted_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(accepted_record)},
        _context(
            accepted_actor, accepted_record.basis_revision, accepted_record.event_id
        ),
    )
    assert retried == {**accepted, "duplicate": True}

    primary.conn.close()


def test_served_lifecycle_retry_and_stale_basis_are_durable(store_factory, tmp_path):
    store = store_factory("served-lifecycle")
    sprint_id = pg.create_sprint(store, "Served lifecycle", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Transition once")
    item = pg.get_work_item(store, item_id)
    stale_basis = authority.item_revision(item)
    pg.set_work_item_status(store, item_id, "active")

    command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()),
        record_type="item.done",
        schema_version="1",
        actor="served-lifecycle",
        authored_at="2026-07-21T12:00:00Z",
        refs={
            "repo_id": store.authority_repo_uuid,
            "aggregate_type": "item",
            "aggregate_uuid": item["aggregate_uuid"],
        },
        payload={"to_status": "done"},
        basis_revision=stale_basis,
    )
    record = _command_record(tmp_path / "stale-producer.db", command)
    app = _application(store, {})
    context = _context(command.actor, record.basis_revision, record.event_id)

    rejected = app.invoke(
        "work.lifecycle.arbitrate", {"record": record_to_dict(record)}, context
    )
    retried = app.invoke(
        "work.lifecycle.arbitrate", {"record": record_to_dict(record)}, context
    )

    assert rejected["outcome"] == "rejected"
    assert rejected["reason_code"] == "stale-basis"
    assert retried == {**rejected, "duplicate": True}
    assert pg.get_work_item(store, item_id)["status"] == "active"
    store.conn.close()


def test_done_from_claim_is_atomic_and_retries_after_claim_delete(store_factory, tmp_path):
    store = store_factory("served-atomic-finish")
    sprint_id = pg.create_sprint(store, "Atomic finish", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Finish once")
    pg.set_work_item_status(store, item_id, "active")
    item = pg.get_work_item(store, item_id)
    claim_id = pg.create_claim(store, item_id, "worker")
    claim = pg.get_claim(store, claim_id, include_secret=True)
    assert claim is not None
    ref = authority.credential_ref(claim["claim_token"])
    command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()), record_type="item.done-from-claim", schema_version="1",
        actor="worker", authored_at="2026-07-26T12:00:00Z",
        refs={"repo_id": store.authority_repo_uuid, "aggregate_type": "item", "aggregate_uuid": item["aggregate_uuid"]},
        payload={"claim_id": claim_id, "credential_ref": ref, "keep_claim": False},
        basis_revision=authority.item_revision(item),
    )
    record = _command_record(tmp_path / "atomic-finish.db", command)
    app = _application(store, {ref: claim["claim_token"]})
    context = _context("worker", record.basis_revision, record.event_id)
    accepted = app.invoke("work.lifecycle.arbitrate", {"record": record_to_dict(record)}, context)
    assert accepted["outcome"] == "accepted"
    assert accepted["effect"]["claim_released"] is True
    assert pg.get_work_item(store, item_id)["status"] == "done"
    assert pg.get_claim(store, claim_id) is None
    duplicate = app.invoke("work.lifecycle.arbitrate", {"record": record_to_dict(record)}, context)
    assert duplicate == {**accepted, "duplicate": True}

    kept_id = pg.create_work_item(store, sprint_id, track_id, "Finish but retain claim")
    pg.set_work_item_status(store, kept_id, "active")
    kept_item = pg.get_work_item(store, kept_id)
    kept_claim_id = pg.create_claim(store, kept_id, "worker")
    kept_claim = pg.get_claim(store, kept_claim_id, include_secret=True)
    assert kept_claim is not None
    kept_ref = authority.credential_ref(kept_claim["claim_token"])
    kept_command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()), record_type="item.done-from-claim", schema_version="1",
        actor="worker", authored_at="2026-07-26T12:00:00Z",
        refs={"repo_id": store.authority_repo_uuid, "aggregate_type": "item", "aggregate_uuid": kept_item["aggregate_uuid"]},
        payload={"claim_id": kept_claim_id, "credential_ref": kept_ref, "keep_claim": True},
        basis_revision=authority.item_revision(kept_item),
    )
    kept_record = _command_record(tmp_path / "atomic-finish-keep.db", kept_command)
    kept = _application(store, {kept_ref: kept_claim["claim_token"]}).invoke(
        "work.lifecycle.arbitrate", {"record": record_to_dict(kept_record)},
        _context("worker", kept_record.basis_revision, kept_record.event_id),
    )
    assert kept["outcome"] == "accepted" and kept["effect"]["claim_released"] is False
    assert kept["effect"]["claim_still_present"] is True
    assert pg.get_work_item(store, kept_id)["status"] == "done"
    assert pg.get_claim(store, kept_claim_id) is not None

    other_id = pg.create_work_item(store, sprint_id, track_id, "Reject wrong proof")
    pg.set_work_item_status(store, other_id, "active")
    other = pg.get_work_item(store, other_id)
    other_claim_id = pg.create_claim(store, other_id, "worker")
    bad_ref = authority.credential_ref("wrong-proof")
    rejected_command = contracts.AuthorityCommand(
        event_id=str(uuid.uuid4()), record_type="item.done-from-claim", schema_version="1",
        actor="worker", authored_at="2026-07-26T12:00:00Z",
        refs={"repo_id": store.authority_repo_uuid, "aggregate_type": "item", "aggregate_uuid": other["aggregate_uuid"]},
        payload={"claim_id": other_claim_id, "credential_ref": bad_ref, "keep_claim": False},
        basis_revision=authority.item_revision(other),
    )
    rejected_record = _command_record(tmp_path / "atomic-finish-reject.db", rejected_command)
    rejected_context = _context("worker", rejected_record.basis_revision, rejected_record.event_id)
    rejected = _application(store, {bad_ref: "wrong-proof"}).invoke(
        "work.lifecycle.arbitrate", {"record": record_to_dict(rejected_record)}, rejected_context
    )
    assert rejected["outcome"] == "rejected" and rejected["reason_code"] == "invalid-claim-proof"
    assert pg.get_work_item(store, other_id)["status"] == "active"
    assert pg.get_claim(store, other_claim_id) is not None
    store.conn.close()


def test_item_transition_selects_delegated_execute_claim_not_older_coordinate(
    store_factory, tmp_path
):
    store = store_factory("delegated-item-transition")
    sprint_id = pg.create_sprint(store, "Delegated transition", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Activate me")
    item = pg.get_work_item(store, item_id)
    coordinate_id = pg.create_claim(
        store, item_id, "coordinator", claim_type="coordinate", ttl_seconds=600
    )
    coordinate = pg.get_claim(store, coordinate_id, include_secret=True)
    execute_id = pg.create_claim(
        store, item_id, "worker", claim_type="execute", ttl_seconds=600,
        coordinate_claim_id=coordinate_id,
        coordinate_claim_token=coordinate["claim_token"],
    )
    execute = pg.get_claim(store, execute_id, include_secret=True)

    def transition(claim, proof, label):
        ref = authority.credential_ref(proof)
        command = contracts.AuthorityCommand(
            event_id=str(uuid.uuid4()), record_type="item.transition", schema_version="1",
            actor="worker", authored_at="2026-08-02T12:00:00Z",
            refs={
                "repo_id": store.authority_repo_uuid, "aggregate_type": "item",
                "aggregate_uuid": item["aggregate_uuid"], "aggregate_id": item_id,
            },
            payload={"to_status": "active", "claim_id": claim["claim_id"], "credential_ref": ref},
            basis_revision=authority.item_revision(item),
        )
        record = _command_record(tmp_path / f"{label}.db", command)
        return _application(store, {ref: proof}).invoke(
            "work.lifecycle.arbitrate", {"record": record_to_dict(record)},
            _context("worker", record.basis_revision, record.event_id),
        )

    rejected = transition(coordinate, coordinate["claim_token"], "coordinate")
    assert rejected["outcome"] == "rejected"
    assert rejected["reason_code"] == "invalid-claim-proof"
    assert pg.get_work_item(store, item_id)["status"] == "pending"

    accepted = transition(execute, execute["claim_token"], "execute")
    assert accepted["outcome"] == "accepted"
    assert accepted["effect"]["status"] == "active"
    assert pg.get_work_item(store, item_id)["status"] == "active"
    store.conn.close()


def test_claim_context_returns_non_secret_snapshot_and_current_revision(
    store_factory, tmp_path
):
    store = store_factory("claim-context")
    sprint_id = pg.create_sprint(store, "Claim context", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Context item")
    item = pg.get_work_item(store, item_id)

    command, credentials = _claim_command(
        store, item, "context-actor", "context-proof", str(uuid.uuid4())
    )
    record = _command_record(tmp_path / "context-producer.db", command)
    app = _application(store, credentials)
    context = _context("context-actor", record.basis_revision, record.event_id)
    accepted = app.invoke(
        "work.claim.arbitrate", {"record": record_to_dict(record)}, context
    )
    assert accepted["outcome"] == "accepted"
    claim_id = accepted["effect"]["claim_id"]

    read_context = _context("context-reader", None, None)
    result = app.invoke("work.claim.context", {"claim_id": claim_id}, read_context)

    assert result["repo_id"] == store.repo_id
    assert result["authority_repo_uuid"] == store.authority_repo_uuid
    assert result["actor"] == "context-reader"
    assert result["claim"]["claim_id"] == claim_id
    assert result["claim"]["work_item_id"] == item_id
    assert result["claim_revision"] == authority.get_claim_revision(store, claim_id)

    serialized = json.dumps(result)
    assert "claim_token" not in result["claim"]
    assert "context-proof" not in serialized
    assert "dsn" not in serialized.lower()
    store.conn.close()


def test_claim_context_missing_claim_rejects_without_producer_record(store_factory):
    store = store_factory("claim-context-missing")
    app = _application(store, {})
    context = _context("context-reader", None, None)

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke("work.claim.context", {"claim_id": 999999}, context)

    assert rejected.value.code == "claim-not-found"
    assert rejected.value.http_status == 404
    assert pg.list_ingested_records(store, after_offset=0, limit=None) == []
    store.conn.close()


def test_claim_renew_applies_metadata_with_legacy_heartbeat_semantics(
    store_factory, tmp_path
):
    store = store_factory("claim-renew-metadata")
    sprint_id = pg.create_sprint(store, "Claim renew", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Renew item")
    item = pg.get_work_item(store, item_id)

    acquire, acquire_credentials = _claim_command(
        store, item, "renew-actor", "renew-proof", str(uuid.uuid4())
    )
    acquire_record = _command_record(tmp_path / "renew-acquire.db", acquire)
    app = _application(store, acquire_credentials)
    accepted = app.invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(acquire_record)},
        _context("renew-actor", acquire_record.basis_revision, acquire_record.event_id),
    )
    claim_id = accepted["effect"]["claim_id"]

    # First renew: supply full metadata.
    claim = pg.get_claim(store, claim_id, include_secret=True)
    renew_one_command, renew_one_credentials = _renew_command(
        store,
        claim,
        "renew-proof",
        str(uuid.uuid4()),
        metadata={
            "runtime_session_id": "session-one",
            "instance_id": "instance-one",
            "branch": "feature/one",
            "worktree_path": "/work/one",
            "commit_sha": "a" * 40,
            "pr_ref": "org/repo#1",
            "hostname": "host-one",
            "pid": 111,
        },
    )
    renew_one = _command_record(tmp_path / "renew-one.db", renew_one_command)
    app_one = _application(store, renew_one_credentials)
    result_one = app_one.invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(renew_one)},
        _context("renew-actor", renew_one.basis_revision, renew_one.event_id),
    )
    assert result_one["outcome"] == "accepted"
    after_one = pg.get_claim(store, claim_id, include_secret=False)
    assert after_one["runtime_session_id"] == "session-one"
    assert after_one["instance_id"] == "instance-one"
    assert after_one["branch"] == "feature/one"
    assert after_one["worktree_path"] == "/work/one"
    assert after_one["commit_sha"] == "a" * 40
    assert after_one["pr_ref"] == "org/repo#1"
    assert after_one["hostname"] == "host-one"
    assert after_one["pid"] == 111

    # Second renew: omit metadata entirely -- existing values must survive
    # (the same COALESCE / "only apply non-null values" semantics legacy
    # ``pg.heartbeat_claim`` uses).
    claim = pg.get_claim(store, claim_id, include_secret=True)
    renew_two_command, renew_two_credentials = _renew_command(
        store, claim, "renew-proof", str(uuid.uuid4()), metadata=None
    )
    renew_two = _command_record(tmp_path / "renew-two.db", renew_two_command)
    app_two = _application(store, renew_two_credentials)
    result_two = app_two.invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(renew_two)},
        _context("renew-actor", renew_two.basis_revision, renew_two.event_id),
    )
    assert result_two["outcome"] == "accepted"
    after_two = pg.get_claim(store, claim_id, include_secret=False)
    assert after_two["runtime_session_id"] == "session-one"
    assert after_two["branch"] == "feature/one"
    assert after_two["pid"] == 111

    # Third renew: a partial metadata object overrides only the named field.
    claim = pg.get_claim(store, claim_id, include_secret=True)
    renew_three_command, renew_three_credentials = _renew_command(
        store,
        claim,
        "renew-proof",
        str(uuid.uuid4()),
        metadata={"branch": "feature/two"},
    )
    renew_three = _command_record(tmp_path / "renew-three.db", renew_three_command)
    app_three = _application(store, renew_three_credentials)
    result_three = app_three.invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(renew_three)},
        _context("renew-actor", renew_three.basis_revision, renew_three.event_id),
    )
    assert result_three["outcome"] == "accepted"
    after_three = pg.get_claim(store, claim_id, include_secret=False)
    assert after_three["branch"] == "feature/two"
    assert after_three["runtime_session_id"] == "session-one"
    assert after_three["pid"] == 111
    store.conn.close()


def test_expired_claim_release_accepts_valid_proof_and_rejects_wrong_proof(
    store_factory, tmp_path
):
    store = store_factory("expired-claim-release")
    sprint_id = pg.create_sprint(store, "Expired claim release", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Release item")
    item = pg.get_work_item(store, item_id)

    acquire, acquire_credentials = _claim_command(
        store, item, "release-owner", "release-proof", str(uuid.uuid4())
    )
    acquire_record = _command_record(tmp_path / "release-acquire.db", acquire)
    acquired = _application(store, acquire_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(acquire_record)},
        _context(
            "release-owner",
            acquire_record.basis_revision,
            acquire_record.event_id,
        ),
    )
    claim_id = acquired["effect"]["claim_id"]
    with store.conn.cursor() as cur:
        cur.execute(
            "UPDATE claim SET expires_at = now() - interval '1 second' "
            "WHERE repo_id = %s AND id = %s",
            (store.repo_id, claim_id),
        )
    store.conn.commit()

    expired = pg.get_claim(store, claim_id, include_secret=True)
    wrong_command, wrong_credentials = _release_command(
        store, expired, "wrong-proof", str(uuid.uuid4())
    )
    wrong_record = _command_record(tmp_path / "release-wrong.db", wrong_command)
    wrong = _application(store, wrong_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(wrong_record)},
        _context("release-owner", wrong_record.basis_revision, wrong_record.event_id),
    )
    assert wrong["outcome"] == "rejected"
    assert wrong["reason_code"] == "invalid-claim-proof"
    assert pg.get_claim(store, claim_id, include_secret=False) is not None

    release_command, release_credentials = _release_command(
        store, expired, "release-proof", str(uuid.uuid4())
    )
    release_record = _command_record(tmp_path / "release-valid.db", release_command)
    released = _application(store, release_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(release_record)},
        _context(
            "release-owner",
            release_record.basis_revision,
            release_record.event_id,
        ),
    )
    assert released["outcome"] == "accepted"
    assert released["effect"]["released"] is True
    assert released["effect"]["status"] == "active"
    assert pg.get_claim(store, claim_id, include_secret=False) is None
    store.conn.close()


def test_claim_handoff_atomically_emits_non_secret_coordination_event(
    store_factory, tmp_path
):
    store = store_factory("claim-handoff-event")
    sprint_id = pg.create_sprint(store, "Claim handoff", status="active")
    track_id = pg.get_or_create_track(store, sprint_id, "work")
    item_id = pg.create_work_item(store, sprint_id, track_id, "Handoff item")
    item = pg.get_work_item(store, item_id)

    acquire, acquire_credentials = _claim_command(
        store, item, "handoff-owner", "handoff-old-proof", str(uuid.uuid4())
    )
    acquire_record = _command_record(tmp_path / "handoff-acquire.db", acquire)
    app = _application(store, acquire_credentials)
    accepted = app.invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(acquire_record)},
        _context(
            "handoff-owner", acquire_record.basis_revision, acquire_record.event_id
        ),
    )
    claim_id = accepted["effect"]["claim_id"]

    claim = pg.get_claim(store, claim_id, include_secret=True)
    handoff_command, handoff_credentials = _handoff_command(
        store,
        claim,
        actor="handoff-owner",
        to_actor="handoff-recipient",
        token="handoff-old-proof",
        proposed_token="handoff-new-proof",
        event_id=str(uuid.uuid4()),
        note="Structured handoff note.",
    )
    handoff = _command_record(tmp_path / "handoff.db", handoff_command)
    handoff_app = _application(store, handoff_credentials)
    handoff_context = _context(
        "handoff-owner", handoff.basis_revision, handoff.event_id
    )
    handoff_result = handoff_app.invoke(
        "work.claim.arbitrate", {"record": record_to_dict(handoff)}, handoff_context
    )
    assert handoff_result["outcome"] == "accepted"
    assert handoff_result["effect"]["actor"] == "handoff-recipient"

    events = [
        event
        for event in pg.list_events(store, sprint_id)
        if event["event_type"] == "claim-handoff"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["actor"] == "handoff-owner"
    assert event["work_item_id"] == item_id
    payload = json.loads(event["payload"])
    assert payload["operation"] == "handoff"
    assert payload["mode"] == "rotate"
    assert payload["detail"] == "Structured handoff note."
    assert payload["token_rotated"] is True
    assert payload["from_identity"]["actor"] == "handoff-owner"
    assert payload["to_identity"]["actor"] == "handoff-recipient"
    assert payload["from_identity"]["claim_token_present"] is True
    assert payload["to_identity"]["claim_token_present"] is True

    serialized = json.dumps(payload)
    assert "handoff-old-proof" not in serialized
    assert "handoff-new-proof" not in serialized
    assert "claim_token" not in payload["from_identity"]
    assert "claim_token" not in payload["to_identity"]

    # A handoff rejected *inside* the handoff branch itself (credential
    # conflict, discovered after proof resolution but before the ownership
    # UPDATE) must leave both claim ownership and coordination evidence
    # untouched -- the ownership UPDATE and the evidence INSERT commit or
    # roll back together.
    blocker_item_id = pg.create_work_item(store, sprint_id, track_id, "Blocker item")
    blocker_item = pg.get_work_item(store, blocker_item_id)
    blocker_acquire, blocker_credentials = _claim_command(
        store, blocker_item, "handoff-recipient", "blocker-proof", str(uuid.uuid4())
    )
    blocker_record = _command_record(tmp_path / "handoff-blocker.db", blocker_acquire)
    _application(store, blocker_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(blocker_record)},
        _context(
            "handoff-recipient",
            blocker_acquire.basis_revision,
            blocker_acquire.event_id,
        ),
    )

    claim_after_handoff = pg.get_claim(store, claim_id, include_secret=True)
    conflicting_command, conflicting_credentials = _handoff_command(
        store,
        claim_after_handoff,
        actor="handoff-recipient",
        to_actor="handoff-third",
        token="handoff-new-proof",
        proposed_token="blocker-proof",
        event_id=str(uuid.uuid4()),
    )
    conflicting = _command_record(
        tmp_path / "handoff-conflicting.db", conflicting_command
    )
    conflicting_result = _application(store, conflicting_credentials).invoke(
        "work.claim.arbitrate",
        {"record": record_to_dict(conflicting)},
        _context(
            "handoff-recipient", conflicting.basis_revision, conflicting.event_id
        ),
    )
    assert conflicting_result["outcome"] == "rejected"
    assert conflicting_result["reason_code"] == "credential-conflict"

    events_after = [
        event
        for event in pg.list_events(store, sprint_id)
        if event["event_type"] == "claim-handoff"
    ]
    assert len(events_after) == 1
    assert pg.get_claim(store, claim_id, include_secret=False)["agent"] == (
        "handoff-recipient"
    )
    store.conn.close()
