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
    assert "SELECT FOR UPDATE" in packet["operations"][0]["synchronization"]
    assert packet["bounds"]["independent_connections"] == 2
    assert "old-token-cannot-mutate-after-rotated-handoff" in packet["invariants"]


def test_claim_protocol_reports_bounded_postgres_exclusivity_evidence():
    protocol = (ROOT / "docs/protocols/claim-ownership.md").read_text(encoding="utf-8")

    assert "work-item row lock is the arbitration point" in protocol
    assert "classified as `concurrency-tested`" in protocol
    assert "general cross-operation linearizability proof" in protocol


def test_remote_ingest_context_covers_retry_gap_and_cursor_protocol():
    packet = json.loads(
        (ROOT / "verification/contexts/outbox-remote-ingestion.json").read_text(encoding="utf-8")
    )

    assert packet["schema_version"] == "test-context/v1"
    assert packet["depth"] == 2
    assert packet["backends"] == ["postgres"]
    assert "same-stream-retry-after-lost-response" in [operation["name"] for operation in packet["operations"]]
    assert "sequence-gaps-are-rejected-without-partial-batch-admission" in packet["invariants"]
