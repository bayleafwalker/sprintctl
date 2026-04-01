import json

from sprintctl import contracts


class TestContextContractModel:
    def test_to_dict_keeps_frozen_key_order(self):
        payload = contracts.ContextContract(
            sprint={"id": 4, "name": "S4"},
            summary={"total": 1},
            active_claims=[],
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
