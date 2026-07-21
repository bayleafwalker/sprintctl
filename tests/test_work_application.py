from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import SimpleNamespace

import pytest

from sprintctl import application, contracts, db, outbox
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
    payload = {
        "event_id": f"00000000-0000-4000-8000-{sequence:012d}",
        "record_type": event_type,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return outbox.OutboxRecord(
        origin_stream_id="00000000-0000-4000-8000-000000000001",
        origin_seq=sequence,
        event_id=f"00000000-0000-4000-8000-{sequence:012d}",
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
