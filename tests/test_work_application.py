from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import SimpleNamespace

import pytest

from sprintctl import application, authority, contracts, db, outbox
from sprintctl.application import (
    ApplicationRejection,
    ProjectMemberApplication,
    ProjectWorkApplication,
    WorkApplication,
    batch_idempotency_key,
    project_batch_idempotency_key,
    record_to_dict,
)
from sprintctl.cli import cli
from sprintctl.vuoro_adapter import (
    LEGACY_REMOTE_COMMAND_PARITY,
    SCHEMA_DIALECT,
    WORK_OPERATION_CONTRACTS,
)
from tests.test_maintenance_capability import AT, CAPABILITY_ID, envelope


def _context(
    *,
    actor: str = "served-test",
    basis_revision: str | None = None,
    idempotency_key: str | None = None,
    repo_ids: frozenset[str] | None = None,
    request_id: str = "request-1",
):
    identity = SimpleNamespace(
        actor=actor,
        environment="vuoro-dev",
        authorities=frozenset(),
    )
    if repo_ids is not None:
        identity.authorizes_repo = lambda repo_id: repo_id in repo_ids
    return SimpleNamespace(
        identity=identity,
        request_id=request_id,
        basis_revision=basis_revision,
        catalog_revision="catalog-1",
        idempotency_requirement="required" if idempotency_key else "not-allowed",
        idempotency_key=idempotency_key,
    )


def _record(
    event_type: str,
    *,
    sequence: int,
    record_class: str,
    actor: str = "served-test",
    basis_revision: str | None = None,
) -> outbox.OutboxRecord:
    event_id = f"00000000-0000-4000-8000-{sequence:012d}"
    if record_class == contracts.RecordClass.AUTHORITY_COMMAND.value:
        assert event_type == "item.transition"
        command = contracts.AuthorityCommand(
            event_id=event_id,
            record_type=event_type,
            schema_version="1",
            actor=actor,
            authored_at="2026-07-21T12:00:00Z",
            refs={
                "repo_id": "10000000-0000-4000-8000-000000000001",
                "aggregate_type": "item",
                "aggregate_uuid": "20000000-0000-4000-8000-000000000001",
            },
            payload={"to_status": "active"},
            basis_revision=basis_revision,
        )
        payload = command.to_dict()
    else:
        payload = {"event_id": event_id, "record_type": event_type}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return outbox.OutboxRecord(
        origin_stream_id="00000000-0000-4000-8000-000000000001",
        origin_seq=sequence,
        event_id=event_id,
        schema_version=1,
        record_class=record_class,
        event_type=event_type,
        actor=actor,
        runtime_session_id="session-1",
        occurred_at="2026-07-21T12:00:00Z",
        basis_revision=basis_revision,
        correlation_id=None,
        causation_id=None,
        payload=payload,
        payload_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        created_at="2026-07-21T12:00:00Z",
    )


def _claim_record(
    *,
    sequence: int,
    command_actor: str,
    claim_agent: str,
    outer_actor: str | None = None,
) -> outbox.OutboxRecord:
    event_id = f"00000000-0000-4000-8000-{sequence:012d}"
    command = contracts.AuthorityCommand(
        event_id=event_id,
        record_type="claim.acquire",
        schema_version="1",
        actor=command_actor,
        authored_at="2026-07-21T12:00:00Z",
        refs={
            "repo_id": "10000000-0000-4000-8000-000000000001",
            "aggregate_type": "item",
            "aggregate_uuid": "20000000-0000-4000-8000-000000000001",
        },
        payload={
            "agent": claim_agent,
            "claim_type": "execute",
            "exclusive": True,
            "ttl_seconds": 300,
            "credential_ref": "sha256:" + "1" * 64,
            "metadata": {},
        },
        basis_revision="item:1:pending",
    )
    payload = command.to_dict()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return outbox.OutboxRecord(
        origin_stream_id="00000000-0000-4000-8000-000000000001",
        origin_seq=sequence,
        event_id=event_id,
        schema_version=1,
        record_class=contracts.RecordClass.AUTHORITY_COMMAND.value,
        event_type="claim.acquire",
        actor=outer_actor or command_actor,
        runtime_session_id="session-1",
        occurred_at="2026-07-21T12:00:00Z",
        basis_revision="item:1:pending",
        correlation_id=None,
        causation_id=None,
        payload=payload,
        payload_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
        created_at="2026-07-21T12:00:00Z",
    )


class _AdminShutdown(RuntimeError):
    sqlstate = "57P01"


class _RuntimeConnection:
    def __init__(self, name: str):
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RuntimeBackend:
    def __init__(self, stale_connection: _RuntimeConnection):
        self.stale_connection = stale_connection
        self.read_calls: list[_RuntimeConnection] = []
        self.write_calls = 0

    def list_sprints(self, store):
        self.read_calls.append(store.conn)
        if store.conn is self.stale_connection:
            raise _AdminShutdown("administrative shutdown")
        return [{"id": 1, "kind": "active_sprint"}]

    def create_sprint(self, *_args, **_kwargs):
        self.write_calls += 1
        raise _AdminShutdown("administrative shutdown")


def _runtime_application(backend: _RuntimeBackend, factory):
    from sprintctl.pg import PgStore

    stale = backend.stale_connection
    store = PgStore(conn=stale, repo_id="runtime-test", connection_factory=factory)
    return WorkApplication(
        repo_id="runtime-test",
        store=store,
        backend=backend,
        ingest_records=lambda _records: [],
        arbitrate_command=lambda _record, _credentials, _actor=None: None,
        list_records=lambda _after, _limit: [],
        list_decisions=lambda _after, _limit: [],
    )


def test_admin_shutdown_reconnects_a_read_once_and_reuses_the_fresh_connection():
    stale = _RuntimeConnection("stale")
    fresh = _RuntimeConnection("fresh")
    backend = _RuntimeBackend(stale)
    factory_calls = []
    app = _runtime_application(backend, lambda: factory_calls.append(True) or fresh)

    result = app.invoke("work.read.sprints", {}, _context())

    assert result == {"repo_id": "runtime-test", "sprints": [{"id": 1, "kind": "active_sprint"}]}
    assert factory_calls == [True]
    assert backend.read_calls == [stale, fresh]
    assert app.store.conn is fresh
    assert stale.closed is True


def test_admin_shutdown_never_retries_a_non_idempotent_mutation_but_a_later_read_recovers():
    stale = _RuntimeConnection("stale")
    fresh = _RuntimeConnection("fresh")
    backend = _RuntimeBackend(stale)
    factory_calls = []
    app = _runtime_application(
        backend,
        lambda: factory_calls.append(True) or fresh,
    )

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke("work.sprint.create", {"name": "No replay"}, _context())

    assert rejected.value.code == "postgres-runtime-unavailable"
    assert rejected.value.http_status == 503
    assert backend.write_calls == 1
    assert factory_calls == []
    assert stale.closed is True
    assert app.store.conn is None
    assert app.served_runtime_ready() is False

    recovered = app.invoke("work.read.sprints", {}, _context())

    assert recovered["sprints"] == [{"id": 1, "kind": "active_sprint"}]
    assert backend.write_calls == 1
    assert backend.read_calls == [fresh]
    assert factory_calls == [True]
    assert app.served_runtime_ready() is True


def test_admin_shutdown_factory_failure_is_not_ready_until_a_later_read_recovers():
    stale = _RuntimeConnection("stale")
    fresh = _RuntimeConnection("fresh")
    backend = _RuntimeBackend(stale)
    factory_results = [RuntimeError("postgres is still restarting"), fresh]

    def factory():
        result = factory_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    app = _runtime_application(backend, factory)

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke("work.read.sprints", {}, _context())

    assert rejected.value.code == "postgres-runtime-unavailable"
    assert rejected.value.http_status == 503
    assert backend.read_calls == [stale]
    assert stale.closed is True
    assert app.store.conn is None
    assert app.served_runtime_ready() is False

    recovered = app.invoke("work.read.sprints", {}, _context())

    assert recovered["sprints"] == [{"id": 1, "kind": "active_sprint"}]
    assert backend.read_calls == [stale, fresh]
    assert app.store.conn is fresh
    assert app.served_runtime_ready() is True


def test_admin_shutdown_retry_requires_an_explicit_idempotency_key_for_writes():
    assert WorkApplication._can_retry_after_admin_shutdown(
        "work.lifecycle.arbitrate", _context(idempotency_key="event-1")
    )
    assert not WorkApplication._can_retry_after_admin_shutdown(
        "work.lifecycle.arbitrate", _context()
    )
    assert not WorkApplication._can_retry_after_admin_shutdown(
        "work.item.edit", _context(idempotency_key="revision-1")
    )


@dataclass
class _IngestResult:
    record: outbox.OutboxRecord
    ingest_offset: int
    duplicate: bool


@dataclass
class _Decision:
    record: outbox.OutboxRecord
    duplicate: bool
    outcome: str = "accepted"

    def to_dict(self):
        return {
            "request_event_id": self.record.event_id,
            "decision_event_id": "10000000-0000-4000-8000-000000000001",
            "decision_ingest_offset": 10 + self.record.origin_seq,
            "decision_type": (
                "item.transitioned"
                if self.outcome == "accepted"
                else "command.rejected"
            ),
            "outcome": self.outcome,
            "reason_code": None if self.outcome == "accepted" else "stale-basis",
            "reason_detail": None,
            "effect": {"status": "active"} if self.outcome == "accepted" else {},
            "duplicate": self.duplicate,
        }


def _application(
    *,
    repo_id="test-repo",
    store=None,
    backend=None,
    calls=None,
) -> WorkApplication:
    calls = calls if calls is not None else []
    ingested: dict[str, int] = {}
    decided: set[str] = set()

    def ingest(records):
        calls.append((repo_id, "ingest", [record.event_id for record in records]))
        results = []
        for record in records:
            duplicate = record.event_id in ingested
            offset = ingested.setdefault(record.event_id, len(ingested) + 1)
            results.append(_IngestResult(record, offset, duplicate))
        return results

    def arbitrate(record, authenticated_actor=None):
        calls.append((repo_id, "arbitrate", record.event_id, authenticated_actor))
        duplicate = record.event_id in decided
        decided.add(record.event_id)
        return _Decision(record, duplicate)

    return WorkApplication(
        repo_id=repo_id,
        store=store,
        backend=backend or SimpleNamespace(),
        ingest_records=ingest,
        arbitrate_command=arbitrate,
        list_records=lambda after, limit: [],
        list_decisions=lambda after, limit: [],
    )


def test_catalog_covers_served_work_surfaces_and_legacy_inventory():
    names = [contract.name for contract in WORK_OPERATION_CONTRACTS]
    assert len(names) == len(set(names))
    assert {
        "work.identity.current",
        "work.read.context",
        "work.read.context-candidates",
        "work.maintain.check",
        "work.read.handoff",
        "work.read.next-work",
        "work.read.next-work-explain",
        "work.read.events",
        "work.read.sprint",
        "work.read.sprint-detail",
        "work.event.add",
        "work.handoff.record",
        "work.item.create",
        "work.item.edit",
        "work.reservation.reserve",
        "work.reservation.touch",
        "work.reservation.reassign",
        "work.reservation.release",
        "work.lifecycle.arbitrate",
        "work.evidence.ingest",
        "work.batch.apply",
        "work.project.context",
        "work.project.sprints",
        "work.project.next-work",
        "work.project.batch",
        "work.read.maintenance-capability",
        "work.maintenance.prepare",
        "work.maintenance.transition",
        "work.maintenance.recovery-record",
        "work.maintenance.resource.prepare",
    } <= set(names)
    assert {row["operation"] for row in LEGACY_REMOTE_COMMAND_PARITY} <= set(names)
    for contract in WORK_OPERATION_CONTRACTS:
        assert contract.name.startswith("work.")
        assert contract.input_schema["$schema"] == SCHEMA_DIALECT
        assert contract.result_schema["$schema"] == SCHEMA_DIALECT
        assert contract.input_schema["additionalProperties"] is False
    required_idempotency = {
        contract.name
        for contract in WORK_OPERATION_CONTRACTS
        if contract.idempotency == "required"
    }
    assert required_idempotency == {
        "work.lifecycle.arbitrate",
        "work.evidence.ingest",
        "work.batch.apply",
        "work.project.batch",
        "work.maintenance.prepare",
        "work.maintenance.transition",
        "work.maintenance.recovery-record",
            "work.maintenance.resource.prepare",
            "work.reservation.reserve",
            "work.reservation.touch",
            "work.reservation.reassign",
            "work.reservation.release",
        }


def test_preexisting_maintenance_descriptors_remain_byte_identical():
    expected = {
        "work.read.maintenance-capability": "9810bf641ed177dd815a2768f665b7594a097b51f1418182bd998f41ddc61a3c",
        "work.maintenance.prepare": "918e7ab7c7add60816a9ffa5d3e326918f186360a67f2e84266cdc1d72fe3f81",
    }
    for contract in WORK_OPERATION_CONTRACTS:
        if contract.name not in expected:
            continue
        value = {
            key: getattr(contract, key)
            for key in (
                "name", "input_schema", "result_schema", "required_authority",
                "execution_semantics", "idempotency", "required_client_schema_features",
            )
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(encoded).hexdigest() == expected[contract.name]


def test_work_identity_current_returns_authenticated_actor(conn):
    app = _application(store=conn, backend=db)

    result = app.invoke("work.identity.current", {}, _context(actor="served-actor"))

    assert result == {"repo_id": "test-repo", "actor": "served-actor"}


def test_served_handoff_uses_shared_bundle_and_authenticated_append_only_record(conn, active_sprint):
    """The server builds the local contract; each successful client emission records once."""
    app = _application(store=conn, backend=db)
    context = _context(actor="authenticated-agent")

    bundle = app.invoke(
        "work.read.handoff",
        {"sprint_id": active_sprint["id"], "events_limit": 50,
         "git_context": {"branch": "agent/handoff", "sha": "abc", "worktree": "/client", "dirty_files": ["x.py"]}},
        context,
    )

    assert bundle["bundle_type"] == "handoff"
    assert bundle["git_context"]["worktree"] == "/client"
    first = app.invoke("work.handoff.record", {"sprint_id": active_sprint["id"], "bundle": bundle}, context)
    second = app.invoke("work.handoff.record", {"sprint_id": active_sprint["id"], "bundle": bundle}, context)

    assert first["actor"] == "authenticated-agent"
    assert first["event_id"] != second["event_id"]
    events = [event for event in db.list_events(conn, active_sprint["id"]) if event["event_type"] == "handoff-generated"]
    assert [event["actor"] for event in events] == ["authenticated-agent", "authenticated-agent"]
    reservation_reserve = next(
        contract
        for contract in WORK_OPERATION_CONTRACTS
        if contract.name == "work.reservation.reserve"
    )
    assert reservation_reserve.idempotency == "required"


def test_work_read_context_returns_the_exact_frozen_v1_contract(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    db.create_work_item(conn, active_sprint["id"], track, "Ready")
    app = _application(store=conn, backend=db)
    result = app.invoke("work.read.context", {"sprint_id": active_sprint["id"]}, _context())
    assert list(result) == [
        "contract_version", "sprint", "summary", "active_reservations",
        "active_unreserved_items", "conflicts", "ready_items", "blocked_items",
        "stale_items", "recent_decisions", "next_action",
    ]
    assert result["contract_version"] == "1"
    assert result["sprint"]["id"] == active_sprint["id"]
    assert result["summary"]["ready"] == 1
    assert result["next_action"]["kind"] == "start-ready-item"


def test_work_read_context_candidates_returns_reservation_admissible_explicit_target(
    conn, active_sprint
):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    target = db.create_work_item(conn, active_sprint["id"], track, "Exact target")
    db.create_work_item(conn, active_sprint["id"], track, "Advisory candidate")
    app = _application(store=conn, backend=db)

    result = app.invoke(
        "work.read.context-candidates",
        {"sprint_id": active_sprint["id"], "item_id": target, "target_paths": [], "query": None, "limit": 5},
        _context(),
    )

    assert result["sprint"]["id"] == active_sprint["id"]
    assert result["explicit_target"] == {"item_id": target, "found": True}
    candidate = next(row for row in result["candidates"] if row["item_id"] == target)
    assert candidate["rank"] == 1
    assert candidate["reservation_admissible"] is True
    assert result["projection"]["fallback_reason"] == "served-authority"


def test_served_maintain_check_uses_the_owning_readonly_diagnostic(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Unclaimed active")
    db.set_work_item_status(conn, item_id, "active")
    app = _application(store=conn, backend=db)

    result = app.invoke("work.maintain.check", {"sprint_id": active_sprint["id"]}, _context())

    assert result["repo_id"] == "test-repo"
    assert result["sprint"]["id"] == active_sprint["id"]
    assert result["threshold_hours"] > 0
    assert result["pending_threshold_hours"] is None
    assert "active-item-without-reservation" in {
        finding["reason_code"] for finding in result["findings"]
    }


def test_work_read_events_contract_shape():
    """sprintctl#1247: ``work.read.events`` follows the ``work.read.records``/
    ``work.read.decisions`` read-contract template, but with ``sprint_id``
    required and an optional server-side ``work_item_id`` filter (matching
    ``work.read.item``'s pattern), not the plain pagination-only shape those
    two share."""

    contract = next(
        c for c in WORK_OPERATION_CONTRACTS if c.name == "work.read.events"
    )
    assert contract.required_authority == "work:read"
    assert contract.execution_semantics == "read"
    assert contract.idempotency == "not-allowed"
    assert contract.input_schema["required"] == ["sprint_id"]
    assert set(contract.input_schema["properties"]) == {
        "sprint_id",
        "work_item_id",
        "after_offset",
        "limit",
    }
    assert contract.input_schema["additionalProperties"] is False
    assert contract.result_schema["required"] == ["repo_id", "events"]
    assert contract.result_schema["properties"]["events"] == {
        "type": "array",
        "items": {"type": "object"},
    }


def test_work_item_edit_contract_requires_revision_and_is_repo_scoped_write():
    contract = next(
        c for c in WORK_OPERATION_CONTRACTS if c.name == "work.item.edit"
    )
    assert contract.required_authority == "work:lifecycle"
    assert contract.execution_semantics == "write"
    assert contract.idempotency == "not-allowed"
    assert contract.input_schema["required"] == [
        "item_id",
        "description",
        "expected_revision",
    ]
    assert set(contract.input_schema["properties"]) == {
        "item_id",
        "description",
        "expected_revision",
        # Optional and never authorizing: it only lets the reservation ledger
        # attribute a successful edit to the caller's own session.
        "session_id",
    }


def test_maintenance_catalog_is_closed_and_least_authority():
    contracts_by_name = {contract.name: contract for contract in WORK_OPERATION_CONTRACTS}
    assert {
        "work.read.maintenance-capability",
        "work.maintenance.prepare",
        "work.maintenance.transition",
        "work.maintenance.recovery-record",
    } <= set(contracts_by_name)
    assert contracts_by_name["work.read.maintenance-capability"].required_authority == "work:maintenance"
    assert contracts_by_name["work.read.maintenance-capability"].idempotency == "not-allowed"
    assert contracts_by_name["work.maintenance.prepare"].execution_semantics == "admin"
    assert contracts_by_name["work.maintenance.prepare"].idempotency == "required"
    transition = contracts_by_name["work.maintenance.transition"]
    assert transition.input_schema["properties"]["action"]["enum"] == [
        "attest", "activate", "observe", "reconcile", "abort", "revoke"
    ]
    recovery = contracts_by_name["work.maintenance.recovery-record"]
    assert recovery.required_authority == "work:maintenance-audit"
    assert recovery.result_schema["properties"]["authority"] == {"const": "none"}
    for name in (
        "work.read.maintenance-capability",
        "work.maintenance.prepare",
        "work.maintenance.transition",
        "work.maintenance.recovery-record",
    ):
        assert contracts_by_name[name].input_schema["additionalProperties"] is False


def test_maintenance_application_delegates_lifecycle_and_recovery_audit_only(
    conn, monkeypatch
):
    monkeypatch.setattr(WorkApplication, "_maintenance_now", staticmethod(lambda: AT))
    app = _application(store=conn, backend=db)
    prepare_id = "11111111-1111-4111-8111-111111111111"
    prepare_context = _context(
        actor="operator", request_id=prepare_id, idempotency_key=prepare_id
    )
    prepared = app.invoke(
        "work.maintenance.prepare",
        {"capability_id": CAPABILITY_ID, "envelope": envelope()},
        prepare_context,
    )
    assert prepared["state"] == "prepared"
    assert prepared["duplicate"] is False

    # A later authority-clock observation does not change semantic replay.
    monkeypatch.setattr(
        WorkApplication,
        "_maintenance_now",
        staticmethod(lambda: "2026-08-02T20:01:00Z"),
    )
    replay = app.invoke(
        "work.maintenance.prepare",
        {"capability_id": CAPABILITY_ID, "envelope": envelope()},
        prepare_context,
    )
    assert replay["duplicate"] is True
    assert replay["revision"] == prepared["revision"]

    status = app.invoke(
        "work.read.maintenance-capability",
        {"capability_id": CAPABILITY_ID},
        _context(actor="operator"),
    )
    assert status["capability"]["state"] == "prepared"
    assert "envelope_json" not in status["capability"]

    stale_id = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(ApplicationRejection) as stale:
        app.invoke(
            "work.maintenance.transition",
            {
                "capability_id": CAPABILITY_ID,
                "action": "attest",
                "expected_revision": "mcap:stale:prepared:1",
                "effect_ref": "sha256:" + "0" * 64,
            },
            _context(actor="operator", request_id=stale_id, idempotency_key=stale_id),
        )
    assert stale.value.code == "maintenance-revision-conflict"
    assert stale.value.http_status == 409
    assert app.invoke(
        "work.read.maintenance-capability",
        {"capability_id": CAPABILITY_ID},
        _context(actor="operator"),
    )["capability"]["state"] == "prepared"

    recovery_id = "22222222-2222-4222-8222-222222222222"
    recovery = app.invoke(
        "work.maintenance.recovery-record",
        {
            "capability_id": CAPABILITY_ID,
            "kind": "requested-command",
            "payload_ref": "artifact:sha256:" + "7" * 64,
        },
        _context(actor="observer", request_id=recovery_id, idempotency_key=recovery_id),
    )
    assert recovery["authority"] == "none"
    assert app.invoke(
        "work.read.maintenance-capability",
        {"capability_id": CAPABILITY_ID},
        _context(actor="operator"),
    )["capability"]["state"] == "prepared"


def test_maintenance_application_rejects_actor_and_idempotency_substitution(
    conn, monkeypatch
):
    monkeypatch.setattr(WorkApplication, "_maintenance_now", staticmethod(lambda: AT))
    app = _application(store=conn, backend=db)
    request_id = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(ApplicationRejection) as actor_rejected:
        app.invoke(
            "work.maintenance.prepare",
            {"capability_id": CAPABILITY_ID, "envelope": envelope()},
            _context(actor="not-operator", request_id=request_id, idempotency_key=request_id),
        )
    assert actor_rejected.value.code == "maintenance-actor-mismatch"
    assert conn.execute("SELECT count(*) FROM maintenance_capability").fetchone()[0] == 0

    with pytest.raises(ApplicationRejection) as replay_rejected:
        app.invoke(
            "work.maintenance.prepare",
            {"capability_id": CAPABILITY_ID, "envelope": envelope()},
            _context(actor="operator", request_id=request_id, idempotency_key="different"),
        )
    assert replay_rejected.value.code == "idempotency-mismatch"
    assert conn.execute("SELECT count(*) FROM maintenance_capability").fetchone()[0] == 0


def test_served_item_links_and_claim_reads_use_backend_contracts(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    blocker = db.create_work_item(conn, active_sprint["id"], track, "Blocker")
    waiting = db.create_work_item(conn, active_sprint["id"], track, "Waiting")
    app = _application(store=conn, backend=db)

    ref = app.invoke("work.item.ref.add", {"item_id": blocker, "ref_type": "doc", "url": "docs/plan.md", "label": "plan"}, _context())
    dep = app.invoke("work.item.dep.add", {"item_id": blocker, "blocked_item_id": waiting}, _context())
    item = app.invoke("work.read.item", {"item_id": blocker}, _context())
    assert item["refs"][0]["id"] == ref["ref_id"]
    assert item["deps"]["blocks"][0]["id"] == dep["dep_id"]
    assert app.invoke("work.read.items", {"sprint_id": active_sprint["id"], "track_name": None, "status": None}, _context())["items"]
    app.invoke("work.item.ref.remove", {"item_id": blocker, "ref_id": ref["ref_id"]}, _context())
    app.invoke("work.item.dep.remove", {"item_id": blocker, "dep_id": dep["dep_id"]}, _context())


def test_served_reservation_show_is_credential_free(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Inspect")
    reservation = db.reserve(conn, item_id, actor="agent", session_id="session-a")
    result = _application(store=conn, backend=db).invoke("work.read.reservation", {"reservation_id": reservation["id"]}, _context())
    assert result["reservation"]["id"] == reservation["id"]
    assert "claim_token" not in result["reservation"]


def test_read_events_returns_sprint_events_in_order(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Item")
    e1 = db.create_event(
        conn, active_sprint["id"], "agent", event_type="decision",
        work_item_id=item_id, payload={"summary": "first"},
    )
    e2 = db.create_event(
        conn, active_sprint["id"], "agent", event_type="pattern-noted",
        payload={"summary": "second"},
    )

    app = _application(store=conn, backend=db)
    result = app.invoke("work.read.events", {"sprint_id": active_sprint["id"]}, _context())

    assert result["repo_id"] == "test-repo"
    assert [e["id"] for e in result["events"]] == [e1, e2]


def test_read_events_filters_by_work_item_id_server_side(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Item")
    other_id = db.create_work_item(conn, active_sprint["id"], track, "Other")
    matching = db.create_event(
        conn, active_sprint["id"], "agent", event_type="decision",
        work_item_id=item_id, payload={"summary": "matches"},
    )
    db.create_event(
        conn, active_sprint["id"], "agent", event_type="decision",
        work_item_id=other_id, payload={"summary": "does not match"},
    )

    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.read.events",
        {"sprint_id": active_sprint["id"], "work_item_id": item_id},
        _context(),
    )

    assert [e["id"] for e in result["events"]] == [matching]


def test_read_events_applies_after_offset_and_limit(conn, active_sprint):
    ids = [
        db.create_event(
            conn, active_sprint["id"], "agent", event_type="pattern-noted",
            payload={"summary": f"event-{i}"},
        )
        for i in range(5)
    ]

    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.read.events",
        {"sprint_id": active_sprint["id"], "after_offset": 1, "limit": 2},
        _context(),
    )

    assert [e["id"] for e in result["events"]] == ids[1:3]


def test_read_events_rejects_missing_sprint_before_backend(conn):
    app = _application(store=conn, backend=db)
    with pytest.raises(ApplicationRejection) as excinfo:
        app.invoke("work.read.events", {"sprint_id": 999999}, _context())
    assert excinfo.value.http_status == 404
    assert excinfo.value.code == "sprint-not-found"


def test_read_sprint_resolves_explicit_or_active_sprint(conn, active_sprint):
    app = _application(store=conn, backend=db)
    explicit = app.invoke("work.read.sprint", {"sprint_id": active_sprint["id"]}, _context())
    implicit = app.invoke("work.read.sprint", {"sprint_id": None}, _context())
    assert explicit["sprint"]["id"] == active_sprint["id"]
    assert implicit["sprint"]["id"] == active_sprint["id"]


def test_read_sprint_detail_builds_the_local_json_contract_server_side(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served-detail")
    db.create_work_item(conn, active_sprint["id"], track, "Ready")
    app = _application(store=conn, backend=db)

    result = app.invoke(
        "work.read.sprint-detail", {"sprint_id": active_sprint["id"]}, _context()
    )

    assert result["repo_id"] == "test-repo"
    payload = result["sprint"]
    assert payload["id"] == active_sprint["id"]
    assert payload["detail"]["stale_count"] >= 0
    assert payload["detail"]["track_health"]["served-detail"]["total"] == 1
    assert payload["detail"]["takeup"] == {"active_count": 0, "active": []}


def test_event_add_uses_authenticated_actor(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Target")
    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.event.add",
        {"sprint_id": active_sprint["id"], "event_type": "decision", "work_item_id": item_id,
         "source_type": "daemon", "payload": {"summary": "served"}},
        _context(actor="authenticated-actor"),
    )
    assert result["actor"] == "authenticated-actor"
    event = next(event for event in db.list_events(conn, active_sprint["id"]) if event["id"] == result["event_id"])
    assert event["actor"] == "authenticated-actor"
    assert event["source_type"] == "daemon"


def test_item_create_resolves_track_server_side(conn, active_sprint):
    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.item.create",
        {"sprint_id": active_sprint["id"], "track_name": "served", "title": "Created",
         "description": "Implementation scope", "priority": 2},
        _context(actor="authenticated-actor"),
    )
    assert result["track_name"] == "served"
    assert result["item"]["title"] == "Created"
    assert result["item"]["priority"] == 2
    assert db.list_tracks(conn, active_sprint["id"])[0]["name"] == "served"


def test_sprint_create_is_scoped_to_the_authenticated_repository(conn):
    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.sprint.create",
        {"name": "Dispatch", "goal": "Supervised dispatch", "status": "active"},
        _context(actor="authorized-operator"),
    )
    assert result["repo_id"] == "test-repo"
    assert result["sprint"]["name"] == "Dispatch"
    assert result["sprint"]["status"] == "active"


def test_click_next_work_and_application_handler_share_backend_semantics(
    conn, runner, active_sprint
):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    ready_id = db.create_work_item(
        conn, active_sprint["id"], track, "Ready", priority=1
    )
    blocker_id = db.create_work_item(conn, active_sprint["id"], track, "Blocker")
    waiting_id = db.create_work_item(conn, active_sprint["id"], track, "Waiting")
    db.add_dep(conn, blocker_id, waiting_id)
    backlog_id = db.create_sprint(conn, "Backlog", status="planned", kind="backlog")
    backlog_track = db.get_or_create_track(conn, backlog_id, "served")
    db.create_work_item(conn, backlog_id, backlog_track, "Not direct next-work")

    app = _application(store=conn, backend=db)
    payload = app.invoke("work.read.next-work", {}, _context())
    result = runner.invoke(cli, ["next-work", "--json"])

    assert result.exit_code == 0
    cli_ready = json.loads(result.output)
    assert payload["ready_items"] == cli_ready
    assert [item["id"] for item in cli_ready] == [ready_id, blocker_id]

    project = ProjectWorkApplication(
        "vuoro",
        (ProjectMemberApplication("test-repo", app),),
        canonical_binding={
            "project_id": "vuoro",
            "backlog_repos": ["test-repo"],
        },
    )
    project_payload = project.invoke(
        "work.project.next-work",
        {},
        _context(repo_ids=frozenset({"test-repo"})),
    )
    assert [item["title"] for item in project_payload["ready_items"]] == [
        "Not direct next-work"
    ]
    assert project_payload["graph_ready"] is True
    assert project_payload["dispatch_admissible"] == "admissible"
    assert project_payload["dispatch_reason"] == "ready-items-available"

    explained = project.invoke(
        "work.project.next-work-explain", {}, _context(repo_ids=frozenset({"test-repo"}))
    )
    assert explained["explanation"] == {
        "canonical_member_order": ["test-repo"],
        "authorization_checked_before_member_reads": True,
        "unavailable_members": [],
    }

    project_items = project.invoke(
        "work.project.items",
        {"status": "pending"},
        _context(repo_ids=frozenset({"test-repo"})),
    )
    assert {item["title"] for item in project_items["items"]} == {
        "Ready",
        "Blocker",
        "Waiting",
        "Not direct next-work",
    }


def test_next_work_explain_is_one_application_aggregate(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    ready_id = db.create_work_item(conn, active_sprint["id"], track, "Ready")
    blocker_id = db.create_work_item(conn, active_sprint["id"], track, "Blocker")
    waiting_id = db.create_work_item(conn, active_sprint["id"], track, "Waiting")
    db.add_dep(conn, blocker_id, waiting_id)

    payload = _application(store=conn, backend=db).invoke(
        "work.read.next-work-explain", {"sprint_id": active_sprint["id"]}, _context()
    )

    assert payload["contract_version"] == "2"
    assert [item["id"] for item in payload["ready_items"]] == [ready_id, blocker_id]
    assert payload["dependency_waiting_items"][0]["id"] == waiting_id
    assert payload["next_action"]["kind"] == "unblock-dependent-work"
    assert payload["recommended_command_bundle"]["bundle_version"] == "1"


def test_authority_handlers_enforce_actor_basis_and_idempotency_before_backend():
    calls = []
    app = _application(calls=calls)
    record = _record(
        "item.transition",
        sequence=1,
        record_class=contracts.RecordClass.AUTHORITY_COMMAND.value,
        basis_revision="item:1:pending",
    )
    arguments = {"record": record_to_dict(record)}

    with pytest.raises(ApplicationRejection) as wrong_actor:
        app.invoke(
            "work.lifecycle.arbitrate",
            arguments,
            _context(
                actor="someone-else",
                basis_revision=record.basis_revision,
                idempotency_key=record.event_id,
            ),
        )
    assert wrong_actor.value.code == "actor-mismatch"

    with pytest.raises(ApplicationRejection) as wrong_basis:
        app.invoke(
            "work.lifecycle.arbitrate",
            arguments,
            _context(idempotency_key=record.event_id, basis_revision="stale"),
        )
    assert wrong_basis.value.code == "basis-revision-mismatch"

    with pytest.raises(ApplicationRejection) as wrong_key:
        app.invoke(
            "work.lifecycle.arbitrate",
            arguments,
            _context(idempotency_key="other", basis_revision=record.basis_revision),
        )
    assert wrong_key.value.code == "idempotency-key-mismatch"
    assert calls == []

    accepted = app.invoke(
        "work.lifecycle.arbitrate",
        arguments,
        _context(idempotency_key=record.event_id, basis_revision=record.basis_revision),
    )
    retried = app.invoke(
        "work.lifecycle.arbitrate",
        arguments,
        _context(idempotency_key=record.event_id, basis_revision=record.basis_revision),
    )
    assert accepted["outcome"] == "accepted"
    assert accepted["duplicate"] is False
    assert retried == {**accepted, "duplicate": True}


def test_reservation_actor_binding_rejects_before_backend(conn, active_sprint):
    calls = []
    track = db.get_or_create_track(conn, active_sprint["id"], "reservation-actor")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Reserved")
    app = _application(store=conn, backend=db, calls=calls)

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke(
            "work.reservation.reserve",
            {"item_id": item_id, "actor": "different-agent", "session_id": "session-1"},
            _context(actor="served-test"),
        )

    assert rejected.value.code == "actor-mismatch"
    assert calls == []

def test_click_free_reservation_reserve_matches_cli_state_flow(conn, runner, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "reservation-reserve")
    app_item = db.create_work_item(conn, active_sprint["id"], track, "Application")
    cli_item = db.create_work_item(conn, active_sprint["id"], track, "CLI")
    app = _application(store=conn, backend=db)
    shared = {
        "actor": "worker",
        "session_id": "thread-1",
        "correlation_ref": "actionq:job-1",
    }

    served = app.invoke(
        "work.reservation.reserve", {"item_id": app_item, **shared}, _context(actor="worker")
    )
    cli_result = runner.invoke(
        cli,
        [
            "reservation",
            "reserve",
            "--item-id",
            str(cli_item),
            "--actor",
            "worker",
            "--session-id",
            "thread-1",
            "--correlation-ref",
            "actionq:job-1",
            "--json",
        ],
    )

    assert cli_result.exit_code == 0, cli_result.output
    local = json.loads(cli_result.output)
    for result in (served, local):
        reservation = result["reservation"] if "reservation" in result else result
        assert reservation["actor"] == "worker"
        assert reservation["session_id"] == "thread-1"
        assert reservation["correlation_ref"] == "actionq:job-1"

def test_second_reservation_coexists_and_is_reported_as_a_conflict(conn, active_sprint):
    """The default is coexistence: the ledger records both and says so.

    Refusing the second session would not stop it working -- it would only
    stop it being visible -- so a conflict is surfaced, not enforced.
    """
    track = db.get_or_create_track(conn, active_sprint["id"], "reservation-overlap")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Shared")
    app = _application(store=conn, backend=db)

    first = app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "first-owner", "session_id": "session-1"},
        _context(actor="first-owner"),
    )
    second = app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "second-owner", "session_id": "session-2"},
        _context(actor="second-owner"),
    )

    assert first["reservation"]["conflict"] is False
    assert second["reservation"]["conflict"] is True
    assert second["reservation"]["conflict_severity"] == "warning"
    assert [row["id"] for row in second["reservation"]["conflicting_reservations"]] == [
        first["reservation"]["id"]
    ]
    assert {row["state"] for row in db.list_reservations(conn, item_id, active_only=False)} == {"active"}


def test_verification_beside_execution_is_an_informational_overlap(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "reservation-roles")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Reviewed")
    app = _application(store=conn, backend=db)

    app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "first-owner", "session_id": "session-1"},
        _context(actor="first-owner"),
    )
    reviewer = app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "second-owner", "session_id": "session-2", "role": "verification"},
        _context(actor="second-owner"),
    )

    assert reviewer["reservation"]["conflict"] is True
    assert reviewer["reservation"]["conflict_severity"] == "informational"


def test_interrupt_existing_is_a_deliberate_takeover(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "reservation-takeover")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Reacquire")
    app = _application(store=conn, backend=db)

    first = app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "first-owner", "session_id": "session-1"},
        _context(actor="first-owner"),
    )
    second = app.invoke(
        "work.reservation.reserve",
        {"item_id": item_id, "actor": "replacement-owner", "session_id": "session-2",
         "interrupt_existing": True},
        _context(actor="replacement-owner"),
    )
    history = db.list_reservations(conn, item_id, active_only=False)
    assert [reservation["state"] for reservation in history] == ["active", "interrupted"]
    assert second["reservation"]["id"] == history[0]["id"]
    assert second["reservation"]["conflict"] is False
    assert db.get_reservation(conn, first["reservation"]["id"])["interruption_reason"] == (
        "interrupted by replacement-owner (session-2)"
    )


def test_item_note_records_an_event_bound_to_the_authenticated_actor_not_arguments(
    conn, active_sprint
):
    track = db.get_or_create_track(conn, active_sprint["id"], "notes")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Note target")
    app = _application(store=conn, backend=db)

    result = app.invoke(
        "work.item.note",
        {
            "item_id": item_id,
            "note_type": "decision",
            "summary": "Chose the served path",
            "detail": "See #1220 evidence.",
            "tags": ["served", "note"],
        },
        _context(actor="authenticated-actor"),
    )

    assert result["item_id"] == item_id
    assert result["note_type"] == "decision"
    assert result["summary"] == "Chose the served path"
    events = db.list_events(conn, active_sprint["id"])
    recorded = next(e for e in events if e["id"] == result["event_id"])
    assert recorded["event_type"] == "decision"
    assert recorded["work_item_id"] == item_id
    assert recorded["actor"] == "authenticated-actor"
    payload = json.loads(recorded["payload"])
    assert payload["summary"] == "Chose the served path"
    assert payload["detail"] == "See #1220 evidence."
    assert payload["tags"] == ["served", "note"]


def test_item_note_rejects_a_missing_item_before_any_write(conn):
    app = _application(store=conn, backend=db)
    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke(
            "work.item.note",
            {"item_id": 999999, "note_type": "decision", "summary": "no such item"},
            _context(actor="worker"),
        )
    assert rejected.value.code == "item-not-found"


def test_item_edit_is_cas_protected_and_appends_authenticated_audit(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "edit")
    item_id = db.create_work_item(
        conn, active_sprint["id"], track, "Editable", "Original scope"
    )
    blocker_id = db.create_work_item(
        conn, active_sprint["id"], track, "Edit blocker", "Blocker scope"
    )
    db.add_ref(conn, item_id, "doc", "docs/edit-contract.md", "edit-contract")
    db.add_dep(conn, blocker_id, item_id)
    db.reserve(conn, item_id, actor="reservation-owner", session_id="session-edit", role="inspect")
    app = _application(store=conn, backend=db)
    before = app.invoke("work.read.item", {"item_id": item_id}, _context())
    revision = before["item"]["edit_revision"]
    prior_event_ids = [event["id"] for event in before["events"]]

    edited = app.invoke(
        "work.item.edit",
        {
            "item_id": item_id,
            "description": "Corrected scope",
            "expected_revision": revision,
        },
        _context(actor="authenticated-editor"),
    )

    assert edited["item"]["id"] == item_id
    assert edited["item_id"] == item_id
    assert edited["event_id"] > 0
    assert edited["actor"] == "authenticated-editor"
    assert edited["item"]["description"] == "Corrected scope"
    assert edited["previous_revision"] == revision
    assert edited["revision"] != revision
    after = app.invoke("work.read.item", {"item_id": item_id}, _context())
    assert after["item"]["aggregate_uuid"] == before["item"]["aggregate_uuid"]
    assert after["item"]["edit_revision"] == edited["revision"]
    assert after["item"]["title"] == before["item"]["title"]
    assert after["item"]["status"] == before["item"]["status"]
    assert after["active_reservations"] == before["active_reservations"]
    assert after["refs"] == before["refs"]
    assert after["deps"] == before["deps"]
    assert prior_event_ids == [
        event["id"] for event in after["events"] if event["id"] in prior_event_ids
    ]
    audit = next(event for event in after["events"] if event["id"] == edited["event_id"])
    assert audit["event_type"] == contracts.ITEM_EDITED_EVENT_TYPE
    assert audit["actor"] == "authenticated-editor"
    payload = json.loads(audit["payload"])
    assert payload["previous_revision"] == revision
    assert payload["revision"] == edited["revision"]
    assert set(payload) == {
        "summary",
        "field",
        "previous_description",
        "description",
        "previous_revision",
        "revision",
    }

    with pytest.raises(ApplicationRejection) as stale:
        app.invoke(
            "work.item.edit",
            {
                "item_id": item_id,
                "description": "Stale overwrite",
                "expected_revision": revision,
            },
            _context(actor="other-editor"),
        )
    assert stale.value.code == "item-edit-conflict"
    assert stale.value.http_status == 409
    assert db.get_work_item(conn, item_id)["description"] == "Corrected scope"
    assert len(db.list_events(conn, active_sprint["id"])) == len(after["events"])

    with pytest.raises(ApplicationRejection) as noop:
        app.invoke(
            "work.item.edit",
            {
                "item_id": item_id,
                "description": "Corrected scope",
                "expected_revision": edited["revision"],
            },
            _context(actor="authenticated-editor"),
        )
    assert noop.value.code == "item-edit-rejected"
    assert len(db.list_events(conn, active_sprint["id"])) == len(after["events"])


def test_item_edit_distinguishes_missing_item_and_required_revision(
    conn, active_sprint
):
    app = _application(store=conn, backend=db)
    with pytest.raises(ApplicationRejection) as missing:
        app.invoke(
            "work.item.edit",
            {
                "item_id": 999999,
                "description": "Corrected scope",
                "expected_revision": (
                    "item:00000000-0000-4000-8000-000000000000"
                    "@description:v0@sha256:" + "0" * 64
                ),
            },
            _context(),
        )
    assert missing.value.code == "item-not-found"
    assert missing.value.http_status == 404

    track = db.get_or_create_track(conn, active_sprint["id"], "edit")
    item_id = db.create_work_item(
        conn, active_sprint["id"], track, "Editable", "Scope"
    )
    with pytest.raises(ApplicationRejection) as invalid:
        app.invoke(
            "work.item.edit",
            {"item_id": item_id, "description": "Corrected scope"},
            _context(),
        )
    assert invalid.value.code == "invalid-arguments"

    with pytest.raises(ApplicationRejection) as malformed:
        app.invoke(
            "work.item.edit",
            {
                "item_id": item_id,
                "description": "Corrected scope",
                "expected_revision": "not-a-revision",
            },
            _context(),
        )
    assert malformed.value.code == "invalid-arguments"
    assert malformed.value.http_status == 422


def test_generic_event_add_cannot_forge_an_item_edit_audit(conn, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "edit")
    item_id = db.create_work_item(
        conn, active_sprint["id"], track, "Forgery target", "Original scope"
    )
    app = _application(store=conn, backend=db)

    with pytest.raises(ApplicationRejection) as forged:
        app.invoke(
            "work.event.add",
            {
                "sprint_id": active_sprint["id"],
                "work_item_id": item_id,
                "event_type": contracts.ITEM_EDITED_EVENT_TYPE,
                "source_type": "actor",
                "payload": {"summary": "forged"},
                "actor": "forged-client-actor",
            },
            _context(actor="authenticated-caller"),
        )

    assert forged.value.code == "event-rejected"
    assert "reserved; use the item edit operation" in forged.value.message
    assert db.list_events(conn, active_sprint["id"]) == []


def test_batch_is_content_bound_idempotent_and_preserves_producer_order():
    calls = []
    app = _application(calls=calls)
    records = [
        _record(
            "note.recorded",
            sequence=1,
            record_class=contracts.RecordClass.OBSERVATION.value,
        ),
        _record(
            "item.transition",
            sequence=2,
            record_class=contracts.RecordClass.AUTHORITY_COMMAND.value,
            basis_revision="item:1:pending",
        ),
        _record(
            "work.completed",
            sequence=3,
            record_class=contracts.RecordClass.OBSERVATION.value,
        ),
    ]
    arguments = {"records": [record_to_dict(record) for record in records]}
    context = _context(
        basis_revision="item:1:pending",
        idempotency_key=batch_idempotency_key(records),
    )

    first = app.invoke("work.batch.apply", arguments, context)
    second = app.invoke("work.batch.apply", arguments, context)

    expected_calls = [
        ("test-repo", "ingest", [records[0].event_id]),
        ("test-repo", "arbitrate", records[1].event_id, "served-test"),
        ("test-repo", "ingest", [records[2].event_id]),
    ]
    assert calls[:3] == expected_calls
    assert calls[3:] == expected_calls
    assert [row["event_id"] for row in first["results"]] == [
        record.event_id for record in records
    ]
    assert [row["duplicate"] for row in first["results"]] == [False, False, False]
    assert [row["duplicate"] for row in second["results"]] == [True, True, True]

    changed = records + [
        _record(
            "decision.recorded",
            sequence=4,
            record_class=contracts.RecordClass.OBSERVATION.value,
        )
    ]
    with pytest.raises(ApplicationRejection) as mismatch:
        app.invoke(
            "work.batch.apply",
            {"records": [record_to_dict(record) for record in changed]},
            context,
        )
    assert mismatch.value.code == "idempotency-key-mismatch"


def test_project_aggregates_use_canonical_binding_order_tags_and_partial_results(tmp_path):
    first_store = db.get_connection(tmp_path / "agentops.db")
    second_store = db.get_connection(tmp_path / "sprintctl.db")
    for store, name in ((first_store, "agentops"), (second_store, "sprintctl")):
        db.init_db(store)
        sprint_id = db.create_sprint(
            store, f"{name} backlog", "goal", None, None, "planned", kind="backlog"
        )
        track_id = db.get_or_create_track(store, sprint_id, "project")
        db.create_work_item(store, sprint_id, track_id, f"{name} ready")

    class SnapshotBackend:
        def __init__(self, unavailable=False):
            self.unavailable = unavailable
            self.snapshots = 0

        def repeatable_read_snapshot(self, store):
            from contextlib import nullcontext

            self.snapshots += 1
            if self.unavailable:
                raise RuntimeError("unavailable")
            return nullcontext(store)

        def __getattr__(self, name):
            return getattr(db, name)

    first_backend = SnapshotBackend()
    second_backend = SnapshotBackend(unavailable=True)
    project = ProjectWorkApplication(
        "vuoro",
        (
            ProjectMemberApplication("agentops", _application(store=first_store, backend=first_backend, repo_id="agentops")),
            ProjectMemberApplication("sprintctl", _application(store=second_store, backend=second_backend, repo_id="sprintctl")),
        ),
        canonical_binding={
            "project_id": "vuoro",
            "display_name": "Vuoro",
            "home_repo": "agentops",
            "backlog_repos": ["agentops", "sprintctl"],
        },
    )

    context = project.invoke(
        "work.project.context", {}, _context(repo_ids=frozenset({"agentops", "sprintctl"}))
    )
    assert context["project"]["display_name"] == "Vuoro"
    assert [row["origin_repo"] for row in context["repositories"]] == ["agentops", "sprintctl"]
    assert context["repositories"][1]["status"] == "unavailable"
    assert context["ready_items"][0]["origin_repo"] == "agentops"
    assert first_backend.snapshots == 1
    assert second_backend.snapshots == 1

    sprints = project.invoke(
        "work.project.sprints", {"include_backlog": True}, _context(repo_ids=frozenset({"agentops", "sprintctl"}))
    )
    assert [row["origin_repo"] for row in sprints["repositories"]] == ["agentops", "sprintctl"]
    assert [row["origin_repo"] for row in sprints["sprints"]] == ["agentops"]


def test_project_aggregates_fail_closed_without_canonical_binding():
    project = ProjectWorkApplication("vuoro", ())

    with pytest.raises(ApplicationRejection) as error:
        project.invoke("work.project.context", {}, _context())

    assert error.value.code == "canonical-project-binding-required"


def test_project_aggregates_reject_without_every_member_authorization():
    project = ProjectWorkApplication(
        "vuoro",
        (ProjectMemberApplication("agentops", _application(repo_id="agentops")),),
        canonical_binding={"project_id": "vuoro", "backlog_repos": ["agentops"]},
    )

    with pytest.raises(ApplicationRejection) as error:
        project.invoke(
            "work.project.sprints", {}, _context(repo_ids=frozenset())
        )

    assert error.value.code == "project-member-unauthorized"


def test_project_batch_requires_declared_order_and_retries_member_units():
    calls = []
    first = _application(repo_id="agentops", calls=calls)
    second = _application(repo_id="sprintctl", calls=calls)
    project = ProjectWorkApplication(
        "vuoro",
        (
            ProjectMemberApplication("agentops", first),
            ProjectMemberApplication("sprintctl", second),
        ),
    )
    first_record = _record(
        "note.recorded",
        sequence=1,
        record_class=contracts.RecordClass.OBSERVATION.value,
    )
    second_record = _record(
        "work.completed",
        sequence=2,
        record_class=contracts.RecordClass.OBSERVATION.value,
    )
    units = [("agentops", [first_record]), ("sprintctl", [second_record])]
    arguments = {
        "units": [
            {
                "origin_repo": origin_repo,
                "records": [record_to_dict(record) for record in records],
            }
            for origin_repo, records in units
        ]
    }
    context = _context(idempotency_key=project_batch_idempotency_key(units))

    result = project.invoke("work.project.batch", arguments, context)
    retried = project.invoke("work.project.batch", arguments, context)

    assert [row["origin_repo"] for row in result["results"]] == [
        "agentops",
        "sprintctl",
    ]
    assert [row["repo_id"] for row in result["results"]] == [
        "agentops",
        "sprintctl",
    ]
    assert [row["results"][0]["ingest_offset"] for row in result["results"]] == [
        1,
        1,
    ]
    assert [call[0] for call in calls] == [
        "agentops",
        "sprintctl",
        "agentops",
        "sprintctl",
    ]
    assert retried["results"][0]["results"][0]["duplicate"] is True

    reversed_arguments = {"units": list(reversed(arguments["units"]))}
    reversed_units = list(reversed(units))
    with pytest.raises(ApplicationRejection) as order:
        project.invoke(
            "work.project.batch",
            reversed_arguments,
            _context(idempotency_key=project_batch_idempotency_key(reversed_units)),
        )
    assert order.value.code == "project-order-mismatch"


def test_project_batch_validates_all_actor_bindings_before_any_member_mutation():
    calls = []
    first = _application(repo_id="agentops", calls=calls)
    second = _application(repo_id="sprintctl", calls=calls)
    project = ProjectWorkApplication(
        "vuoro",
        (
            ProjectMemberApplication("agentops", first),
            ProjectMemberApplication("sprintctl", second),
        ),
    )
    observation = _record(
        "note.recorded",
        sequence=20,
        record_class=contracts.RecordClass.OBSERVATION.value,
    )
    impersonated = _record(
        "item.transition",
        sequence=21,
        record_class=contracts.RecordClass.AUTHORITY_COMMAND.value,
        actor="nested-actor",
        basis_revision="item:1:pending",
    )
    units = [("agentops", [observation]), ("sprintctl", [impersonated])]
    arguments = {
        "units": [
            {
                "origin_repo": origin_repo,
                "records": [record_to_dict(record) for record in records],
            }
            for origin_repo, records in units
        ]
    }

    with pytest.raises(ApplicationRejection) as rejected:
        project.invoke(
            "work.project.batch",
            arguments,
            _context(idempotency_key=project_batch_idempotency_key(units)),
        )

    assert rejected.value.code == "actor-mismatch"
    assert calls == []



def test_reservation_read_catalog_contract_is_a_credential_free_read():
    contract = next(
        contract
        for contract in WORK_OPERATION_CONTRACTS
        if contract.name == "work.read.reservation"
    )
    assert contract.required_authority == "work:read"
    assert contract.execution_semantics == "read"
    assert contract.idempotency == "not-allowed"
    assert contract.input_schema["required"] == ["reservation_id"]


def test_reservation_read_returns_credential_free_snapshot(
    conn, active_sprint
):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Reserved item")
    reservation = db.reserve(conn, item_id, actor="reservation-owner", session_id="session-a")

    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.read.reservation",
        {"reservation_id": reservation["id"]},
        _context(actor="context-reader"),
    )

    assert result["repo_id"] == "test-repo"
    assert result["reservation"]["work_item_id"] == item_id
    assert result["reservation"]["actor"] == "reservation-owner"
    assert "claim_token" not in json.dumps(result)


def test_reservation_read_missing_row_rejects_without_backend_mutation(conn):
    app = _application(store=conn, backend=db)

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke("work.read.reservation", {"reservation_id": 999}, _context())

    assert rejected.value.code == "reservation-not-found"
    assert rejected.value.http_status == 404
