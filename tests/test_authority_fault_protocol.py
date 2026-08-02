"""Executable authority-fault histories governed by the outbox synchronization ADR.

These tests distinguish current backend behavior from the bounded authority
model, including regressions that previously demonstrated known gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools

import pytest

from sprintctl import contracts, db


def _sqlite_authority(db_path):
    owner = db.get_connection(db_path)
    observer = db.get_connection(db_path)
    db.init_db(owner)
    db.init_db(observer)
    sprint_id = db.create_sprint(owner, "Authority faults", status="active")
    track_id = db.get_or_create_track(owner, sprint_id, "protocol")
    item_id = db.create_work_item(owner, sprint_id, track_id, "Partitioned work")
    return owner, observer, sprint_id, item_id


def _missing_receipt_payload(project: str = "sprintctl") -> dict[str, str]:
    receipt_id = f"{project}.2026-07-14.missing"
    return {
        "project": project,
        "receipt_id": receipt_id,
        "receipt_path": (
            f"/projects/dev/_artifacts/{project}/capability/receipts/"
            f"{receipt_id}.json"
        ),
        "receipt_sha256": hashlib.sha256(b"unavailable").hexdigest(),
    }


def test_sqlite_partition_expiry_reassignment_rejects_stale_heartbeat(db_path):
    owner, replacement, _sprint_id, item_id = _sqlite_authority(db_path)
    history: list[tuple[str, str]] = []
    try:
        old_claim_id = db.create_claim(owner, item_id, "partitioned-owner")
        old_claim = db.get_claim(owner, old_claim_id, include_secret=True)
        history.append(("old-claim", "accepted"))

        replacement.execute(
            "UPDATE claim SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (old_claim_id,),
        )
        replacement.commit()
        assert db.list_claims(owner, item_id, active_only=True) == []
        history.append(("partition-expiry", "observed"))

        new_claim_id = db.create_claim(replacement, item_id, "replacement-owner")
        history.append(("replacement-claim", "accepted"))

        with pytest.raises(ValueError, match="expired"):
            db.heartbeat_claim(
                owner,
                old_claim_id,
                old_claim["claim_token"],
                actor="partitioned-owner",
            )
        history.append(("stale-heartbeat", "rejected"))

        active_ids = {
            claim["claim_id"] for claim in db.list_claims(replacement, item_id, active_only=True)
        }
        assert active_ids == {new_claim_id}
        assert [
            (claim["status"], claim["lease_epoch"])
            for claim in db.list_claims(replacement, item_id, active_only=False)
        ] == [("expired", 1), ("active", 2)]
        assert history == [
            ("old-claim", "accepted"),
            ("partition-expiry", "observed"),
            ("replacement-claim", "accepted"),
            ("stale-heartbeat", "rejected"),
        ]
    finally:
        owner.close()
        replacement.close()


def test_sqlite_stale_item_and_sprint_commands_reject_without_second_mutation(db_path):
    actor_a, actor_b, sprint_id, item_id = _sqlite_authority(db_path)
    try:
        db.set_work_item_status(actor_a, item_id, "active")
        assert db.get_work_item(actor_a, item_id)["status"] == "active"
        assert db.get_work_item(actor_b, item_id)["status"] == "active"

        db.set_work_item_status(actor_a, item_id, "done")
        with pytest.raises(db.InvalidTransition, match="done -> done"):
            db.set_work_item_status(actor_b, item_id, "done")
        assert db.get_work_item(actor_b, item_id)["status"] == "done"

        assert db.get_sprint(actor_a, sprint_id)["status"] == "active"
        assert db.get_sprint(actor_b, sprint_id)["status"] == "active"
        first_boundary_id = db.close_sprint_with_boundary_event(actor_a, sprint_id, "actor-a")
        with pytest.raises(db.InvalidTransition, match="sprint closed -> closed"):
            db.close_sprint_with_boundary_event(actor_b, sprint_id, "actor-b")

        boundaries = [
            event
            for event in db.list_events(actor_b, sprint_id)
            if event["event_type"] == contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE
        ]
        assert [event["id"] for event in boundaries] == [first_boundary_id]
        assert db.get_sprint(actor_b, sprint_id)["status"] == "closed"
    finally:
        actor_a.close()
        actor_b.close()


def test_sqlite_unavailable_capability_artifact_rejects_pointer_without_event(
    db_path,
    monkeypatch,
):
    operator, drafting_agent, sprint_id, _item_id = _sqlite_authority(db_path)
    try:
        db.close_sprint_with_boundary_event(operator, sprint_id, "operator")

        def missing(receipt_path: str) -> bytes:
            raise ValueError(f"capability receipt file does not exist: {receipt_path}")

        monkeypatch.setattr(contracts, "_read_capability_receipt_bytes", missing)
        with pytest.raises(ValueError, match="file does not exist"):
            db.create_event(
                drafting_agent,
                sprint_id,
                "drafting-agent",
                contracts.CAPABILITY_RECEIPT_DRAFTED_EVENT_TYPE,
                payload=_missing_receipt_payload(),
            )

        event_types = [event["event_type"] for event in db.list_events(operator, sprint_id)]
        assert event_types == [contracts.SPRINT_CLOSE_BOUNDARY_EVENT_TYPE]
    finally:
        operator.close()
        drafting_agent.close()


@dataclass(frozen=True)
class _ProposalDecision:
    outcome: str
    decision_id: str


class _ProposalAcceptanceReferenceModel:
    """Bounded intended behavior; there is no product refinement mapping yet."""

    def __init__(self) -> None:
        self._accepted: dict[str, tuple[str, _ProposalDecision]] = {}
        self._decisions: dict[tuple[str, str, bool], _ProposalDecision] = {}
        self.effects: list[tuple[str, str]] = []

    def process(self, proposal_id: str, payload_digest: str, *, valid: bool) -> _ProposalDecision:
        key = (proposal_id, payload_digest, valid)
        if key in self._decisions:
            return self._decisions[key]

        accepted = self._accepted.get(proposal_id)
        if accepted is not None:
            accepted_digest, accepted_decision = accepted
            if valid and payload_digest == accepted_digest:
                self._decisions[key] = accepted_decision
                return accepted_decision
            decision = _ProposalDecision(
                "rejected-conflict",
                f"reject:{proposal_id}:{payload_digest}:{int(valid)}",
            )
        elif not valid:
            decision = _ProposalDecision(
                "rejected-invalid",
                f"reject:{proposal_id}:{payload_digest}:0",
            )
        else:
            decision = _ProposalDecision("accepted", f"accept:{proposal_id}:{payload_digest}")
            self._accepted[proposal_id] = (payload_digest, decision)
            self.effects.append((proposal_id, payload_digest))

        self._decisions[key] = decision
        return decision


def test_bounded_proposal_model_is_idempotent_and_rejects_conflicting_payloads():
    histories = list(itertools.product(("digest-a", "digest-b"), repeat=3))
    for history in histories:
        model = _ProposalAcceptanceReferenceModel()
        decisions = [model.process("proposal-1", digest, valid=True) for digest in history]

        assert len(model.effects) == 1
        accepted_digest = model.effects[0][1]
        for digest, decision in zip(history, decisions, strict=True):
            expected = "accepted" if digest == accepted_digest else "rejected-conflict"
            assert decision.outcome == expected
            assert model.process("proposal-1", digest, valid=True) == decision

    model = _ProposalAcceptanceReferenceModel()
    rejected = model.process("proposal-invalid", "digest-x", valid=False)
    assert rejected.outcome == "rejected-invalid"
    assert model.process("proposal-invalid", "digest-x", valid=False) == rejected
    assert model.effects == []


def test_authority_command_and_rejection_taxonomy_is_explicit():
    assert contracts.record_class_for_type("item.done") is contracts.RecordClass.AUTHORITY_COMMAND
    assert contracts.record_class_for_type("sprint.close") is contracts.RecordClass.AUTHORITY_COMMAND
    assert contracts.record_class_for_type("command.rejected") is contracts.RecordClass.REMOTE_DECISION
