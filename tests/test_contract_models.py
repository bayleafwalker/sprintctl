import json
from uuid import uuid4

import pytest

from sprintctl import contracts


def _record_kwargs(record_type: str) -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "record_type": record_type,
        "schema_version": "sprintctl-record/v1",
        "actor": "codex",
        "authored_at": "2026-07-14T12:00:00Z",
        "refs": {"work_item_id": "1128"},
        "payload": {"summary": "typed record"},
        "basis_revision": "item:1128@3",
        "correlation_id": uuid4(),
        "causation_id": uuid4(),
        "payload_digest": "a" * 64,
        "artifact_digest": "b" * 64,
    }


class TestSemanticRecordContracts:
    def test_observation_round_trips_with_stable_json_order(self):
        record = contracts.Observation(**_record_kwargs("work.completed"))

        payload = record.to_dict()
        assert list(payload) == [
            "event_id",
            "record_type",
            "record_class",
            "schema_version",
            "actor",
            "authored_at",
            "refs",
            "payload",
            "basis_revision",
            "correlation_id",
            "causation_id",
            "payload_digest",
            "artifact_digest",
        ]
        restored = contracts.record_from_dict(json.loads(json.dumps(payload)))
        assert restored == record
        assert restored.offline_bufferable is True
        assert restored.requires_remote_arbitration is False
        assert restored.remote_authored is False

    def test_taxonomy_prevents_an_authority_command_from_being_bufferable(self):
        command = contracts.AuthorityCommand(**_record_kwargs("item.done"))
        assert command.offline_bufferable is False
        assert command.requires_remote_arbitration is True

        with pytest.raises(ValueError, match="authority-command"):
            contracts.Observation(**_record_kwargs("item.done"))

    def test_remote_decision_must_use_a_remote_decision_type(self):
        decision = contracts.RemoteDecision(**_record_kwargs("claim.granted"))
        assert decision.remote_authored is True

        payload = decision.to_dict()
        payload["record_class"] = "observation"
        with pytest.raises(ValueError, match="remote-decision"):
            contracts.record_from_dict(payload)

    def test_envelope_validation_rejects_unknown_type_and_invalid_digest(self):
        with pytest.raises(ValueError, match="not classified"):
            contracts.Observation(**_record_kwargs("unclassified.record"))

        invalid = _record_kwargs("work.completed")
        invalid["artifact_digest"] = "not-a-digest"
        with pytest.raises(ValueError, match="artifact_digest"):
            contracts.Observation(**invalid)

    def test_to_dict_is_defensive(self):
        record = contracts.Observation(**_record_kwargs("work.completed"))
        first = record.to_dict()
        first["payload"]["summary"] = "mutated"

        assert record.to_dict()["payload"]["summary"] == "typed record"


class TestContextContractModel:
    def test_to_dict_keeps_frozen_key_order(self):
        payload = contracts.ContextContract(
            sprint={"id": 4, "name": "S4"},
            summary={"total": 1},
            active_claims=[],
            active_unclaimed_items=[],
            conflicts=[],
            ready_items=[],
            blocked_items=[],
            stale_items=[],
            recent_decisions=[],
            next_action={"kind": "no-action"},
        ).to_dict()
        assert list(payload.keys()) == [
            "contract_version",
            "sprint",
            "summary",
            "active_claims",
            "active_unclaimed_items",
            "conflicts",
            "ready_items",
            "blocked_items",
            "stale_items",
            "recent_decisions",
            "next_action",
        ]
        assert payload["contract_version"] == contracts.CONTEXT_CONTRACT_VERSION

    def test_to_dict_is_deterministic_and_defensive(self):
        model = contracts.ContextContract(
            sprint={"id": 4, "name": "S4"},
            summary={"total": 1},
            active_claims=[{"claim_id": 7, "actor": "agent"}],
            active_unclaimed_items=[{"id": 9, "title": "Task"}],
            conflicts=[],
            ready_items=[],
            blocked_items=[],
            stale_items=[],
            recent_decisions=[],
            next_action={"kind": "start-ready-item"},
        )

        first = model.to_dict()
        first["active_claims"][0]["actor"] = "mutated"
        mutated_json = json.dumps(first)

        second = model.to_dict()
        second_json = json.dumps(second)
        assert mutated_json != second_json
        assert second["active_claims"][0]["actor"] == "agent"
        assert second["active_unclaimed_items"][0]["title"] == "Task"


class TestHandoffBundleModel:
    def test_to_dict_keeps_frozen_key_order(self):
        payload = contracts.HandoffBundle(
            sprintctl_version="0.1.0",
            generated_at="2026-04-01T00:00:00Z",
            generated_from={"command": "sprintctl handoff"},
            sprint={"id": 4},
            summary={"total": 0},
            active_claims=[],
            conflicts=[],
            work={"active_items": [], "ready_items": [], "blocked_items": [], "stale_items": []},
            recent_decisions=[],
            recent_events=[],
            next_action={"kind": "no-action"},
            delta_since_last_handoff={"event_count": 0},
            freshness={"generated_at": "2026-04-01T00:00:00Z"},
            evidence={"dirty_files": []},
            git_context=None,
            claim_identity_model={"ownership_proof": "claim_id+claim_token"},
            resume_instructions=[],
            agent_shutdown_protocol={"required_before_termination": []},
            items=[],
            events=[],
        ).to_dict()
        assert list(payload.keys()) == [
            "bundle_type",
            "bundle_version",
            "sprintctl_version",
            "generated_at",
            "generated_from",
            "sprint",
            "summary",
            "active_claims",
            "conflicts",
            "work",
            "recent_decisions",
            "recent_events",
            "next_action",
            "delta_since_last_handoff",
            "freshness",
            "evidence",
            "git_context",
            "claim_identity_model",
            "resume_instructions",
            "agent_shutdown_protocol",
            "items",
            "events",
        ]
        assert payload["bundle_type"] == contracts.HANDOFF_BUNDLE_TYPE
        assert payload["bundle_version"] == contracts.HANDOFF_BUNDLE_VERSION

    def test_to_dict_is_deterministic_and_defensive(self):
        model = contracts.HandoffBundle(
            sprintctl_version="0.1.0",
            generated_at="2026-04-01T00:00:00Z",
            generated_from={"command": "sprintctl handoff"},
            sprint={"id": 4},
            summary={"total": 0},
            active_claims=[{"claim_id": 9, "actor": "agent-a"}],
            conflicts=[],
            work={"active_items": [], "ready_items": [], "blocked_items": [], "stale_items": []},
            recent_decisions=[],
            recent_events=[],
            next_action={"kind": "no-action"},
            delta_since_last_handoff={"event_count": 0},
            freshness={"generated_at": "2026-04-01T00:00:00Z"},
            evidence={"dirty_files": []},
            git_context={"branch": "main"},
            claim_identity_model={"ownership_proof": "claim_id+claim_token"},
            resume_instructions=[],
            agent_shutdown_protocol={"required_before_termination": []},
            items=[],
            events=[],
        )

        first = model.to_dict()
        first["active_claims"][0]["actor"] = "mutated"
        mutated_json = json.dumps(first)

        second = model.to_dict()
        second_json = json.dumps(second)
        assert mutated_json != second_json
        assert second["active_claims"][0]["actor"] == "agent-a"
