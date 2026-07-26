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


def _context(
    *,
    actor: str = "served-test",
    basis_revision: str | None = None,
    idempotency_key: str | None = None,
):
    return SimpleNamespace(
        identity=SimpleNamespace(
            actor=actor,
            environment="vuoro-dev",
            authorities=frozenset(),
        ),
        request_id="request-1",
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


class _FakeTransientCredentialCarrier:
    """Minimal ``TransientCredentialCarrier`` double for resolver unit tests.

    Matches the ``reveal(key) -> str | None`` duck type documented on
    :class:`application.TransientCredentialCarrier` without depending on
    ``vuoro_service`` at all.
    """

    def __init__(self, bindings: dict[str, str]):
        self._bindings = dict(bindings)

    def reveal(self, key: str) -> str | None:
        return self._bindings.get(key)


def _fake_authority_command_record(inner_payload: dict) -> outbox.OutboxRecord:
    """An ``authority-command``-classed record shaped only well enough for
    :func:`application.make_transient_credential_resolver` -- it never
    round-trips through ``record_from_dict``/canonical validation."""

    return outbox.OutboxRecord(
        origin_stream_id="00000000-0000-4000-8000-000000000001",
        origin_seq=1,
        event_id="00000000-0000-4000-8000-000000000099",
        schema_version=1,
        record_class=contracts.RecordClass.AUTHORITY_COMMAND.value,
        event_type="claim.handoff",
        actor="resolver-test",
        runtime_session_id=None,
        occurred_at="2026-07-23T00:00:00Z",
        basis_revision="claim:1@sha256:" + "0" * 64,
        correlation_id=None,
        causation_id=None,
        payload={"payload": inner_payload},
        payload_sha256="0" * 64,
        created_at="2026-07-23T00:00:00Z",
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

    def arbitrate(record, credentials):
        calls.append((repo_id, "arbitrate", record.event_id, dict(credentials)))
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
        "work.read.next-work",
        "work.read.events",
        "work.read.sprint",
        "work.event.add",
        "work.item.create",
        "work.claim.start",
        "work.claim.arbitrate",
        "work.lifecycle.arbitrate",
        "work.evidence.ingest",
        "work.batch.apply",
        "work.project.next-work",
        "work.project.batch",
        "work.pilot.cutover-evidence",
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
        "work.claim.arbitrate",
        "work.lifecycle.arbitrate",
        "work.evidence.ingest",
        "work.batch.apply",
        "work.project.batch",
    }
    claim_start = next(
        contract
        for contract in WORK_OPERATION_CONTRACTS
        if contract.name == "work.claim.start"
    )
    assert claim_start.idempotency == "not-allowed"


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
        "vuoro", (ProjectMemberApplication("test-repo", app),)
    )
    project_payload = project.invoke("work.project.next-work", {}, _context())
    assert [item["title"] for item in project_payload["ready_items"]] == [
        "Not direct next-work"
    ]


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


@pytest.mark.parametrize("operation", ["work.claim.arbitrate", "work.batch.apply"])
@pytest.mark.parametrize(
    ("record", "expected_code"),
    [
        (
            _claim_record(
                sequence=10,
                command_actor="nested-actor",
                claim_agent="nested-actor",
                outer_actor="served-test",
            ),
            "actor-mismatch",
        ),
        (
            _claim_record(
                sequence=11,
                command_actor="served-test",
                claim_agent="different-agent",
            ),
            "claim-agent-mismatch",
        ),
    ],
)
def test_authority_actor_binding_rejects_single_and_batch_before_backend(
    operation, record, expected_code
):
    calls = []
    app = _application(calls=calls)
    if operation == "work.claim.arbitrate":
        arguments = {"record": record_to_dict(record)}
        key = record.event_id
    else:
        arguments = {"records": [record_to_dict(record)]}
        key = batch_idempotency_key([record])

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke(
            operation,
            arguments,
            _context(basis_revision=record.basis_revision, idempotency_key=key),
        )

    assert rejected.value.code == expected_code
    assert calls == []


def test_click_free_claim_start_matches_cli_state_flow(conn, runner, active_sprint):
    track = db.get_or_create_track(conn, active_sprint["id"], "claim-start")
    app_item = db.create_work_item(conn, active_sprint["id"], track, "Application")
    cli_item = db.create_work_item(conn, active_sprint["id"], track, "CLI")
    app = _application(store=conn, backend=db)
    shared = {
        "ttl_seconds": 900,
        "runtime_session_id": "thread-1",
        "instance_id": "process-1",
        "branch": "feat/served",
        "hostname": "test-host",
        "pid": 4242,
    }

    served = app.invoke(
        "work.claim.start", {"item_id": app_item, **shared}, _context(actor="worker")
    )
    cli_result = runner.invoke(
        cli,
        [
            "claim",
            "start",
            "--item-id",
            str(cli_item),
            "--actor",
            "worker",
            "--ttl",
            "900",
            "--runtime-session-id",
            "thread-1",
            "--instance-id",
            "process-1",
            "--branch",
            "feat/served",
            "--hostname",
            "test-host",
            "--pid",
            "4242",
            "--json",
        ],
    )

    assert cli_result.exit_code == 0, cli_result.output
    legacy = json.loads(cli_result.output)
    for result in (served, legacy):
        assert result["operation"] == "claim_start"
        assert result["item_status_before"] == "pending"
        assert result["item_status_after"] == "active"
        assert result["status_transition_applied"] is True
        assert result["claim_token"] == result["claim"]["claim_token"]
        assert result["claim"]["agent"] == "worker"
        assert result["claim"]["claim_type"] == "execute"
        assert result["claim"]["exclusive"] in (1, True)
        assert result["claim"]["runtime_session_id"] == "thread-1"
        assert result["claim"]["instance_id"] == "process-1"
        assert result["claim"]["branch"] == "feat/served"
        assert result["claim"]["hostname"] == "test-host"
        assert result["claim"]["pid"] == 4242

    failing_item = db.create_work_item(conn, active_sprint["id"], track, "Rollback")
    db.set_work_item_status(conn, failing_item, "active", actor="seed")
    db.set_work_item_status(conn, failing_item, "done", actor="seed")
    with pytest.raises(ApplicationRejection) as failed:
        app.invoke(
            "work.claim.start", {"item_id": failing_item}, _context(actor="worker")
        )
    assert failed.value.code == "claim-start-transition-failed"
    assert db.list_claims(conn, failing_item, active_only=False) == []


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
        ("test-repo", "arbitrate", records[1].event_id, {}),
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
    impersonated = _claim_record(
        sequence=21,
        command_actor="nested-actor",
        claim_agent="nested-actor",
        outer_actor="served-test",
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


def test_cutover_evidence_handler_is_the_same_domain_core(monkeypatch, tmp_path):
    expected = {
        "contract_version": "1",
        "config": {},
        "parity": None,
        "watermark": {},
        "stale_tools": {},
        "rollback_rehearsal": None,
        "promotable": False,
        "blockers": ["parity-not-evaluated"],
    }
    observed = {}

    def build(**kwargs):
        observed.update(kwargs)
        return expected

    monkeypatch.setattr(application.cutover, "build_cutover_evidence", build)
    app = _application()
    app.repo_root = tmp_path

    assert (
        app.invoke(
            "work.pilot.cutover-evidence",
            {"rehearse": False, "max_watermark_age_seconds": 90},
            _context(),
        )
        == expected
    )
    assert observed == {
        "cwd": tmp_path,
        "repo_root": tmp_path,
        "parity": None,
        "max_watermark_age_seconds": 90,
        "rehearse": False,
    }


def test_claim_context_catalog_contract_is_an_unauthenticated_read_op_shape():
    contract = next(
        contract
        for contract in WORK_OPERATION_CONTRACTS
        if contract.name == "work.claim.context"
    )
    assert contract.required_authority == "work:claim"
    assert contract.execution_semantics == "read"
    assert contract.idempotency == "not-allowed"
    assert contract.input_schema["required"] == ["claim_id"]


def test_claim_context_returns_non_secret_snapshot_and_current_revision(
    conn, active_sprint
):
    track = db.get_or_create_track(conn, active_sprint["id"], "served")
    item_id = db.create_work_item(conn, active_sprint["id"], track, "Context item")
    claim_id = db.create_claim(conn, item_id, "claim-owner")

    app = _application(store=conn, backend=db)
    result = app.invoke(
        "work.claim.context",
        {"claim_id": claim_id},
        _context(actor="context-reader"),
    )

    assert result["repo_id"] == "test-repo"
    assert result["authority_repo_uuid"] is None
    assert result["actor"] == "context-reader"
    assert result["claim"]["work_item_id"] == item_id
    assert "claim_token" not in result["claim"]

    secret_claim = db.get_claim(conn, claim_id, include_secret=True)
    assert result["claim_revision"] == authority.claim_revision(secret_claim)
    assert secret_claim["claim_token"] not in json.dumps(result)


def test_claim_context_missing_claim_rejects_without_backend_mutation(conn):
    app = _application(store=conn, backend=db)

    with pytest.raises(ApplicationRejection) as rejected:
        app.invoke("work.claim.context", {"claim_id": 999}, _context())

    assert rejected.value.code == "claim-not-found"
    assert rejected.value.http_status == 404


def test_transient_credential_resolver_reveals_only_referenced_refs():
    resolver = application.make_transient_credential_resolver()
    referenced_ref = "sha256:" + "1" * 64
    proposed_ref = "sha256:" + "2" * 64
    unrelated_ref = "sha256:" + "3" * 64
    carrier = _FakeTransientCredentialCarrier(
        {
            referenced_ref: "secret-one",
            proposed_ref: "secret-two",
            unrelated_ref: "secret-three",
        }
    )
    context = SimpleNamespace(transient_credentials=carrier)
    record = _fake_authority_command_record(
        {
            "credential_ref": referenced_ref,
            "proposed_credential_ref": proposed_ref,
        }
    )

    resolved = resolver(context, record)

    assert resolved == {referenced_ref: "secret-one", proposed_ref: "secret-two"}


def test_transient_credential_resolver_returns_none_without_a_carrier():
    resolver = application.make_transient_credential_resolver()
    record = _fake_authority_command_record({"credential_ref": "sha256:" + "1" * 64})

    assert resolver(SimpleNamespace(transient_credentials=None), record) is None
    # A context built before v2 existed (no attribute at all) behaves the same.
    assert resolver(SimpleNamespace(), record) is None


def test_transient_credential_resolver_skips_unresolvable_bindings():
    resolver = application.make_transient_credential_resolver()
    ref = "sha256:" + "4" * 64
    context = SimpleNamespace(transient_credentials=_FakeTransientCredentialCarrier({}))
    record = _fake_authority_command_record({"credential_ref": ref})

    assert resolver(context, record) == {}


def test_transient_credential_resolver_reads_the_flat_payload_for_observations():
    """Only ``authority-command``-classed records nest their domain payload
    one level down (``record.payload["payload"]``); an observation's own
    payload already sits at the top level."""

    resolver = application.make_transient_credential_resolver()
    ref = "sha256:" + "5" * 64
    carrier = _FakeTransientCredentialCarrier({ref: "secret"})
    context = SimpleNamespace(transient_credentials=carrier)
    observation = outbox.OutboxRecord(
        origin_stream_id="00000000-0000-4000-8000-000000000001",
        origin_seq=1,
        event_id="00000000-0000-4000-8000-000000000098",
        schema_version=1,
        record_class=contracts.RecordClass.OBSERVATION.value,
        event_type="event.observed",
        actor="resolver-test",
        runtime_session_id=None,
        occurred_at="2026-07-23T00:00:00Z",
        basis_revision=None,
        correlation_id=None,
        causation_id=None,
        payload={"credential_ref": ref},
        payload_sha256="0" * 64,
        created_at="2026-07-23T00:00:00Z",
    )

    assert resolver(context, observation) == {ref: "secret"}
