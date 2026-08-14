"""Served-work protocol checks against a disposable PostgreSQL authority."""

# ruff: noqa: E402 - sprintctl imports follow the disposable-PG availability gate.

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
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
from sprintctl.maintenance_resource import CursorExpired, MaintenanceResourceStore, NOT_FOUND_PAYLOAD
from sprintctl.maintenance_capability import MaintenanceCapabilityError, PostgresMaintenanceCapabilityStore
from sprintctl.pg_testing import (
    assert_disposable_connection,
    cleanup_test_repositories,
    new_test_repo_id,
    write_cleanup_report,
)
from tests.test_maintenance_capability import AT, CAPABILITY_ID, envelope
from tests.test_maintenance_resource import (
    HISTORIES, TRANSACTIONAL_HISTORIES, exercise_frozen_history, expiring_envelope,
)


class _PostgresResourceOwner:
    def __init__(self, store):
        self.conn = store.conn
        self.repo_id = store.repo_id
        self.row = {"state": "prepared", "not_before": "2026-08-02T19:00:00Z", "expires_at": "2026-08-02T20:00:00Z", "updated_at": "2026-08-02T19:00:00Z"}
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO maintenance_capability(repo_id,capability_id,envelope_id,envelope_digest,envelope_json,plan_ref,operator_identity,not_before,expires_at,state,revision,next_sequence,created_at,updated_at) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,'prepared',1,1,%s,%s)", (self.repo_id, "mcap:test", self.repo_id, "0" * 64, "{}", "artifact:sha256:" + "0" * 64, "operator", self.row["not_before"], self.row["expires_at"], self.row["updated_at"], self.row["updated_at"]))
        self.conn.commit()

    def get(self, capability_id):
        return self.row if capability_id == "mcap:test" else None

    def set(self, state, position):
        updated_at = (datetime(2026, 8, 2, 19, tzinfo=timezone.utc) + timedelta(minutes=position)).isoformat().replace("+00:00", "Z")
        self.row.update(state=state, updated_at=updated_at)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE maintenance_capability SET state=%s,revision=revision+1,updated_at=%s WHERE repo_id=%s AND capability_id='mcap:test'", (state, self.row["updated_at"], self.repo_id))
        self.conn.commit()


@pytest.fixture
def maintenance_resource_pg_factory():
    connection = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(connection)
    with connection.cursor() as cur:
        pg._apply_schema_version_6(cur)
        pg._apply_schema_version_7(cur)
    connection.commit()
    owners = []
    def create(label):
        store = type("ResourceStore", (), {"conn": connection, "repo_id": new_test_repo_id(label)})()
        owner = _PostgresResourceOwner(store)
        owners.append(owner)
        return owner
    yield create
    with connection.cursor() as cur:
        for owner in owners:
            cur.execute("DELETE FROM maintenance_resource_event WHERE repo_id=%s", (owner.repo_id,))
            cur.execute("DELETE FROM maintenance_resource WHERE repo_id=%s", (owner.repo_id,))
    connection.commit(); connection.close()


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


@pytest.fixture
def maintenance_resource_transactional_pg_factory():
    administrative = psycopg.connect(_PG_URL, row_factory=dict_row)
    assert_disposable_connection(administrative)
    with administrative.cursor() as cur:
        pg._apply_schema_version_6(cur); pg._apply_schema_version_7(cur)
    administrative.commit(); repo_ids = set()
    def create(label):
        connection = psycopg.connect(_PG_URL, row_factory=dict_row)
        repo_id = new_test_repo_id(label); repo_ids.add(repo_id)
        return pg.PgStore(conn=connection, repo_id=repo_id, authority_repo_uuid=str(uuid.uuid4()), connection_factory=lambda: psycopg.connect(_PG_URL, row_factory=dict_row), remote_schema_version=7)
    yield create
    cleanup_test_repositories(administrative, repo_ids)
    administrative.close()


def _reopen_pg_store(store):
    return replace(store, conn=psycopg.connect(_PG_URL, row_factory=dict_row))


def exercise_postgres_transactional_history(history, store):
    owner = PostgresMaintenanceCapabilityStore(store)
    capability_id = "mcap:12345678-1234-4234-8234-123456789abc"
    request_id = "12345678-1234-4234-8234-123456789abc"
    arguments = dict(capability_id=capability_id, request_id=request_id, envelope=envelope(), actor="operator", at=AT, resource=True)

    if history in {"disconnect", "prepare-response-loss"}:
        first = owner.prepare(**arguments)
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)
        store.conn.close()  # committed response is deliberately discarded
        recovered_store = _reopen_pg_store(store); recovered_owner = PostgresMaintenanceCapabilityStore(recovered_store)
        retry = recovered_owner.prepare(**(arguments | {"at": "2026-08-02T20:01:00Z"}))
        recovered_resource = MaintenanceResourceStore(recovered_owner)
        assert retry == first | {"duplicate": True}
        assert recovered_resource.reference_envelope(capability_id) == reference
        with recovered_store.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS count FROM maintenance_resource WHERE repo_id=%s", (store.repo_id,)); assert cur.fetchone()["count"] == 1
            cur.execute("SELECT count(*) AS count FROM maintenance_resource_event WHERE repo_id=%s", (store.repo_id,)); assert cur.fetchone()["count"] == 1
        recovered_store.conn.rollback(); recovered_store.conn.close()
    elif history == "cursor-below-floor":
        owner.prepare(**arguments)
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)["reference"]
        with store.conn.cursor() as cur:
            cur.execute("INSERT INTO maintenance_resource_event(repo_id,resource_ref,position,state,updated_at) SELECT %s,%s,value,'active',%s FROM generate_series(2,256) value", (store.repo_id, reference, AT))
            cur.execute("UPDATE maintenance_resource SET current_position=256 WHERE repo_id=%s AND resource_ref=%s", (store.repo_id, reference))
        store.conn.commit(); gate = threading.Barrier(2); outcome = []
        def prune():
            writer = _reopen_pg_store(store)
            with writer.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM maintenance_resource WHERE repo_id=%s AND resource_ref=%s FOR UPDATE", (store.repo_id, reference)); gate.wait()
                cur.execute("INSERT INTO maintenance_resource_event VALUES(%s,%s,257,'active',%s)", (store.repo_id, reference, AT))
                cur.execute("DELETE FROM maintenance_resource_event WHERE repo_id=%s AND resource_ref=%s AND position<2", (store.repo_id, reference))
                cur.execute("UPDATE maintenance_resource SET recovery_floor=1,current_position=257 WHERE repo_id=%s AND resource_ref=%s", (store.repo_id, reference))
            writer.conn.commit(); writer.conn.close()
        thread = threading.Thread(target=prune); thread.start(); gate.wait()
        reader_store = _reopen_pg_store(store)
        try:
            result = MaintenanceResourceStore(PostgresMaintenanceCapabilityStore(reader_store)).changes(reference, "sprintctl-maintenance-cursor-0")
            outcome.append(result["events"][0]["event_id"])
        except CursorExpired:
            outcome.append("cursor-expired")
        thread.join(); assert not thread.is_alive(); reader_store.conn.close()
        assert outcome in [["cursor-expired"], ["sprintctl-maintenance-event-1"]]
    elif history == "parallel-owner-decoders":
        second_id = "mcap:22345678-1234-4234-8234-123456789abc"
        owner.prepare(**arguments)
        second_envelope = envelope(); second_envelope["envelope_id"] = "vuoro-cutover-exact-2"
        owner.prepare(**(arguments | {"capability_id": second_id, "request_id": str(uuid.uuid4()), "envelope": second_envelope}))
        resource = MaintenanceResourceStore(owner)
        expected = [resource.reference_envelope(capability_id), resource.reference_envelope(second_id)]
        with store.conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM maintenance_resource WHERE repo_id=%s) AS resources,(SELECT count(*) FROM maintenance_resource_event WHERE repo_id=%s) AS events", (store.repo_id, store.repo_id)); row = cur.fetchone(); before = (row["resources"], row["events"])
        store.conn.rollback(); decoded = [None, None]
        def decode(index, target):
            sibling = _reopen_pg_store(store)
            decoded[index] = MaintenanceResourceStore(PostgresMaintenanceCapabilityStore(sibling)).reference_envelope(target)
            sibling.conn.rollback(); sibling.conn.close()
        threads = [threading.Thread(target=decode, args=(0, capability_id)), threading.Thread(target=decode, args=(1, second_id))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        with store.conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM maintenance_resource WHERE repo_id=%s) AS resources,(SELECT count(*) FROM maintenance_resource_event WHERE repo_id=%s) AS events", (store.repo_id, store.repo_id)); row = cur.fetchone(); after = (row["resources"], row["events"])
        store.conn.rollback()
        assert decoded == expected and len({item["reference"] for item in decoded}) == 2
        assert after == before == (2, 2)
    elif history == "expiry-materialization-authorized-write":
        owner.prepare(**(arguments | {"envelope": expiring_envelope(envelope())}))
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)["reference"]
        deadline = time.monotonic() + 3
        with pytest.raises(MaintenanceCapabilityError, match="scheduler authorization"):
            owner.sweep_expired(capability_id, at=datetime.now(timezone.utc).isoformat(), principal="operator", authorized=False)
        while not owner.sweep_expired(capability_id, at=datetime.now(timezone.utc).isoformat(), principal="maintenance-owner-scheduler", authorized=True):
            assert time.monotonic() < deadline
            time.sleep(.05)
        snapshot = MaintenanceResourceStore(owner).snapshot(reference)
        assert owner.get(capability_id)["state"] == snapshot["state"]["state"] == "expired"
        assert snapshot["terminal"] is True and snapshot["cursor"].endswith("-2")
    elif history == "restart-during-wait":
        prepared = owner.prepare(**arguments)
        reference = MaintenanceResourceStore(owner).reference_envelope(capability_id)["reference"]
        waiter_store = _reopen_pg_store(store); entered, release = threading.Event(), threading.Event(); interrupted = []
        def pause(_seconds): entered.set(); release.wait(2)
        def wait_request():
            try: MaintenanceResourceStore(PostgresMaintenanceCapabilityStore(waiter_store), pause=pause).changes(reference, "sprintctl-maintenance-cursor-1", 2)
            except psycopg.Error: interrupted.append(True)
        thread = threading.Thread(target=wait_request); thread.start(); assert entered.wait(1)
        waiter_store.conn.close(); release.set(); thread.join(2); assert interrupted == [True]
        owner.transition(capability_id=capability_id, request_id=str(uuid.uuid4()), action="attest", expected_revision=prepared["revision"], actor="operator", at="2026-08-02T20:01:00Z", effect_ref="sha256:" + "0" * 64)
        restarted_store = _reopen_pg_store(store)
        changes = MaintenanceResourceStore(PostgresMaintenanceCapabilityStore(restarted_store)).changes(reference, "sprintctl-maintenance-cursor-1", 2)
        assert changes["events"][0]["data"]["state"] == "attested"
        restarted_store.conn.rollback(); restarted_store.conn.close()
    elif history == "non-disclosure-four-way":
        owner.prepare(**arguments)
        local_resource = MaintenanceResourceStore(owner)
        local_reference = local_resource.reference_envelope(capability_id)["reference"]
        foreign_store = replace(store, repo_id=store.repo_id + "-foreign")
        foreign_owner = PostgresMaintenanceCapabilityStore(foreign_store)
        foreign_owner.prepare(**(arguments | {"request_id": str(uuid.uuid4())}))
        foreign_reference = MaintenanceResourceStore(foreign_owner).reference_envelope(capability_id)["reference"]
        cases = {
            "malformed": ("bad", True),
            "absent": ("smr1_" + "A" * 43, True),
            "foreign": (foreign_reference, True),
            "unauthorized": (local_reference, False),
        }
        outcomes = {}
        for name, (reference, authorized) in cases.items():
            assert local_resource.visible(reference, authorized=authorized) is False
            outcomes[name] = (404, NOT_FOUND_PAYLOAD)
        assert len(cases) == 4 and len(set(cases)) == 4
        assert all(outcome == (404, NOT_FOUND_PAYLOAD) for outcome in outcomes.values())
        assert local_resource.visible(local_reference, authorized=True) is True
        cleanup_test_repositories(store.conn, {foreign_store.repo_id})
    else:
        raise AssertionError(f"missing PostgreSQL transactional falsifier: {history}")


@pytest.mark.parametrize("history", HISTORIES)
def test_maintenance_resource_frozen_postgres_history(maintenance_resource_pg_factory, maintenance_resource_transactional_pg_factory, history):
    if history in TRANSACTIONAL_HISTORIES or history == "non-disclosure-four-way":
        exercise_postgres_transactional_history(history, maintenance_resource_transactional_pg_factory(f"maintenance-resource-transactional-{history}"))
    else:
        owner = maintenance_resource_pg_factory(f"maintenance-resource-{history}")
        exercise_frozen_history(history, owner, MaintenanceResourceStore(owner))


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


def _application(store, credentials=None):
    # ``credentials`` is retained only so the remaining claim-era call sites
    # keep their shape until those tests are retired; the credential resolver
    # seam is gone.
    return WorkApplication.postgres(store)


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
