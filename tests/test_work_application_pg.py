"""Served-work protocol checks against a disposable PostgreSQL authority."""

# ruff: noqa: E402 - sprintctl imports follow the disposable-PG availability gate.

from __future__ import annotations

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


def _context(actor, basis_revision, event_id):
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
            "request_id": f"request:{event_id}",
            "basis_revision": basis_revision,
            "catalog_revision": "catalog-1",
            "idempotency_requirement": "required",
            "idempotency_key": event_id,
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

    with pytest.raises(ApplicationRejection) as rejected:
        _application(store, credentials).invoke(operation, arguments, context)

    assert rejected.value.code == expected_code
    assert pg.list_claims(store, item_id, active_only=False) == []
    assert authority.list_authority_decisions(store, after_offset=0, limit=None) == []
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
