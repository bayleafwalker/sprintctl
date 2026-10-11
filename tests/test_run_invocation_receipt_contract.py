"""Synthetic contract checks for the run invocation receipt v1 packet."""

import json
from pathlib import Path

import jsonschema
import pytest


FIXTURES = Path(__file__).parents[1] / "verification/fixtures/run-invocation-receipt-v1"


def _schema(name):
    return json.loads((FIXTURES / name).read_text())


def _example(name):
    return json.loads((FIXTURES / name).read_text())


def test_correlated_register_keeps_legacy_arguments_and_rejects_transport_claims():
    schema = _schema("register-correlated.schema.json")
    example = _example("register-correlated.example.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(example, schema)

    for key in ("invocation_id", "attempt_id", "principal_epoch", "repo_id"):
        invalid = {**example, key: "caller-controlled"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_receipt_get_requires_both_exact_ids_and_has_no_enumeration_fields():
    schema = _schema("receipt-get.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    valid = {
        "invocation_id": "inv_0123456789ABCDEFGHJKMNPQRS",
        "attempt_id": "att_0123456789ABCDEFGHJKMNPQRS",
    }
    jsonschema.validate(valid, schema)
    for invalid in (
        {"invocation_id": valid["invocation_id"]},
        {"attempt_id": valid["attempt_id"]},
        {**valid, "repo_id": "caller-selected"},
        {**valid, "limit": 64},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_receipt_result_is_closed_and_carries_complete_current_binding():
    schema = _schema("receipt-result.schema.json")
    example = _example("receipt-result.example.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(example, schema)

    for field in ("request_digest", "idempotency_key", "grant_ids", "claim_ids", "run_arguments"):
        invalid = {**example, field: "must-not-be-public"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)
    for field in ("principal_epoch", "grant_id"):
        invalid = json.loads(json.dumps(example))
        del invalid["owner_binding"][field]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_receipt_rejects_bad_ids_digest_and_negative_epoch():
    schema = _schema("receipt-result.schema.json")
    example = _example("receipt-result.example.json")
    for path, value in (
        (("invocation_id",), "inv_short"),
        (("attempt_id",), "att_0"),
        (("owner_record_digest",), "sha256:bad"),
        (("owner_binding", "principal_epoch"), -1),
    ):
        invalid = json.loads(json.dumps(example))
        target = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_operation_contract_preserves_authority_and_legacy_boundary():
    contract = _example("operation-contract.json")
    assert contract["format"] == "sprintctl-versioned-operation-contract/v1"
    assert contract["contract"] == "sprintctl-run-invocation-receipt-contract/v1"
    assert contract["postgres_schema_version"] == 23
    assert contract["sqlite_support"] == "unsupported"
    assert contract["correlation_is_authority"] is False
    assert contract["correlation_is_acceptance_evidence"] is False

    register = contract["operations"]["work.run.register-correlated-v1"]
    assert register["authority"] == "work:evidence"
    assert register["ledger_tool"] == "register_run"
    assert register["legacy_idempotency_and_result"] == "unchanged"
    assert register["correlation_source"] == "verified-gateway-identity-metadata-only"
    assert register["missing_pair"] == "use-legacy-work.run.register-v1"

    receipt = contract["operations"]["work.run.invocation-receipt-get-v1"]
    assert receipt["authority"] == "work:read"
    assert receipt["requires_existing_owned_run"] is True
    assert receipt["lookup"] == "exact-invocation-and-attempt-pair-only"
    assert receipt["enumeration"] is False
    assert receipt["foreign_or_unknown"] == "same-run-not-found-404"
