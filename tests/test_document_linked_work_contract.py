import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_document_link_contract_uses_stable_identity_and_immutable_revision():
    contract = (ROOT / "docs/reference/doc-refs.md").read_text(encoding="utf-8")

    assert "doc_id: my-feature-plan" in contract
    assert "status: draft" in contract
    assert "@git:0123456789abcdef0123456789abcdef01234567" in contract
    assert "--type doc" in contract
    assert "No doc:" in contract
    assert "incomplete execution provenance" in contract


def test_claim_context_records_backend_parity_race_and_stale_proof():
    packet = json.loads(
        (ROOT / "verification/contexts/claim-handoff-backend-parity.json").read_text(encoding="utf-8")
    )

    assert packet["schema_version"] == "test-context/v1"
    assert packet["backends"] == ["sqlite", "postgres"]
    assert packet["depth"] == 2
    assert "barrier-before-conflict-check" == packet["operations"][0]["synchronization"]
    assert "old-token-cannot-mutate-after-rotated-handoff" in packet["invariants"]


def test_claim_protocol_does_not_overstate_postgres_exclusivity():
    protocol = (ROOT / "docs/protocols/claim-ownership.md").read_text(encoding="utf-8")

    assert "not yet established for concurrent PostgreSQL claim creation" in protocol
    assert "classify exclusivity parity as `unknown`" in protocol
