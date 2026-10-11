"""Synthetic contract checks for the run invocation receipt v1 packet."""

import hashlib
import json
import math
from pathlib import Path

import jsonschema
import pytest


FIXTURES = Path(__file__).parents[1] / "verification/fixtures/run-invocation-receipt-v1"
_JCS_SAFE_INTEGER = 9007199254740991


def _schema(name):
    return json.loads((FIXTURES / name).read_text())


def _example(name):
    return json.loads((FIXTURES / name).read_text())


def _jcs_number_supported(value):
    """Fixture-only assertion for the published safe-integer number profile."""
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -_JCS_SAFE_INTEGER <= value <= _JCS_SAFE_INTEGER
    if isinstance(value, float):
        return False
    return True


def test_correlated_register_keeps_legacy_arguments_and_rejects_transport_claims():
    schema = _schema("register-correlated.schema.json")
    example = _example("register-correlated.example.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(example, schema)

    for key in ("invocation_id", "attempt_id", "principal_epoch", "repo_id"):
        invalid = {**example, key: "caller-controlled"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_verified_transport_metadata_requires_valid_id_pair_and_epoch():
    schema = _schema("verified-correlation-metadata.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    valid = {
        "invocation_id": "inv_0123456789ABCDEFGHJKMNPQRS",
        "attempt_id": "att_0123456789ABCDEFGHJKMNPQRS",
        "principal_epoch": 3,
    }
    jsonschema.validate(valid, schema)
    for invalid in (
        {"attempt_id": valid["attempt_id"], "principal_epoch": 3},
        {"invocation_id": valid["invocation_id"], "principal_epoch": 3},
        {"invocation_id": valid["invocation_id"], "attempt_id": valid["attempt_id"]},
        {**valid, "invocation_id": "inv_short"},
        {**valid, "attempt_id": "att_short"},
        {**valid, "principal_epoch": -1},
        {**valid, "principal_epoch": 9007199254740992},
        {**valid, "repo_id": "caller-selected"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_receipt_get_requires_exact_run_invocation_attempt_and_no_enumeration():
    schema = _schema("receipt-get.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    valid = {
        "run_id": "run_0123456789ABCDEFGHJKMNPQRS",
        "invocation_id": "inv_0123456789ABCDEFGHJKMNPQRS",
        "attempt_id": "att_0123456789ABCDEFGHJKMNPQRS",
    }
    jsonschema.validate(valid, schema)
    for invalid in (
        {"run_id": valid["run_id"], "invocation_id": valid["invocation_id"]},
        {"run_id": valid["run_id"], "attempt_id": valid["attempt_id"]},
        {**valid, "run_id": "run_short"},
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

    for field in (
        "request_digest", "owner_request_digest", "idempotency_key",
        "grant_ids", "claim_ids", "run_arguments",
    ):
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
    assert register["verified_metadata_schema"] == "verified-correlation-metadata.schema.json"
    assert register["missing_pair"] == (
        "typed-refusal-for-correlated-operation; caller-routes-to-legacy-v1"
    )
    assert register["result_schema"] == "register-correlated.result.schema.json"
    assert register["legacy_replay_without_correlation"] == (
        "legacy-unbound-with-null-receipt-no-retroactive-link"
    )

    receipt = contract["operations"]["work.run.invocation-receipt-get-v1"]
    assert receipt["authority"] == "work:read"
    assert receipt["requires_existing_owned_run"] is True
    assert receipt["lookup"] == "exact-run-invocation-and-attempt-triple-only"
    assert receipt["enumeration"] is False
    assert receipt["foreign_or_unknown"] == "same-run-not-found-404"
    assert receipt["owned_run_without_exact_receipt"] == "correlation-absent"


def test_correlated_register_result_has_bound_and_legacy_unbound_variants():
    schema = _schema("register-correlated.result.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    bound = _example("register-correlated.bound.example.json")
    unbound = _example("register-correlated.legacy-unbound.example.json")
    jsonschema.validate(bound, schema)
    jsonschema.validate(unbound, schema)
    jsonschema.validate(bound["receipt"], _schema("receipt-result.schema.json"))
    assert bound["run"]["run_id"] == bound["receipt"]["owner_record_id"]
    assert unbound["correlation_status"] == "legacy-unbound"
    assert unbound["receipt"] is None

    for invalid in (
        {**bound, "receipt": None},
        {**unbound, "receipt": bound["receipt"]},
        {key: value for key, value in bound.items() if key != "correlation_status"},
        {**bound, "owner_request_digest": "must-not-be-public"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_owner_digest_vectors_pin_domain_jcs_bytes_and_digest_format():
    vectors = _example("owner-record-digest-vectors.json")
    prefix = vectors["domain_prefix_utf8"].encode("utf-8")
    assert prefix.endswith(b"\x00")
    assert prefix.hex() == vectors["domain_prefix_hex"]
    assert vectors["digest_format"] == "64 lowercase hexadecimal characters; no prefix"
    assert "owner_request_digest" in vectors["included_private_fields"]
    assert "owner_request_digest" in vectors["private_fields_never_emitted"]
    assert set(vectors["excluded_mutable_fields"]) == {"grant_ids", "claim_ids"}

    for vector in vectors["valid_vectors"]:
        canonical = vector["canonical_utf8"].encode("utf-8")
        assert json.loads(canonical) == vector["projection"]
        assert hashlib.sha256(prefix + canonical).hexdigest() == vector["expected_sha256"]
    unicode_vector = vectors["valid_vectors"][1]
    assert list(unicode_vector["projection"]) == ["𐀀", "\ue000"]
    assert vectors["rejected_number_examples"] == [
        {"name": "safe-integer-upper-plus-one", "value": 9007199254740992},
        {"name": "safe-integer-lower-minus-one", "value": -9007199254740992},
        {"name": "float-even-if-integral", "value": 1.0},
        {"name": "non-finite", "value": "NaN"},
    ]
    assert _jcs_number_supported(_JCS_SAFE_INTEGER)
    assert _jcs_number_supported(-_JCS_SAFE_INTEGER)
    for vector in vectors["rejected_number_examples"]:
        if vector["name"] == "non-finite":
            non_finite = float(vector["value"].lower())
            assert not math.isfinite(non_finite)
            assert not _jcs_number_supported(non_finite)
        else:
            assert not _jcs_number_supported(vector["value"])
