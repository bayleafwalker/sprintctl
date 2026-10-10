"""Wire calls cannot weaken Release binding or supply owner authority."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from sprintctl import effect_attempt as contract

REVISION = "item:11111111-1111-1111-1111-111111111111@description:v1@sha256:" + "a" * 64 + "@revise:0"
OPEN = {"intent_id": "intent_" + "1" * 26, "revision": 1,
        "canonical_intent_digest": "a" * 64, "expected_revision": REVISION,
        "release_digest": "b" * 64, "target": {"operation": "push_branch"},
        "idempotency_key": "attempt-key-1"}
REDEEM = {"attempt_id": "attempt_" + "2" * 26, "authorization_digest": "c" * 64,
          "idempotency_key": "consume-key-1"}
CALLS = [
    (contract.OPERATION_OPEN, OPEN),
    (contract.OPERATION_REDEEM, REDEEM),
    (contract.OPERATION_SEAL_UNUSED, REDEEM),
    (contract.OPERATION_REPORT, {**REDEEM, "commit_sha": "d" * 40,
                               "pr_url": "https://forge.example/fixture/repository/pulls/1"}),
    (contract.OPERATION_GET, {"attempt_id": REDEEM["attempt_id"]}),
]


@pytest.mark.parametrize("operation,args", CALLS)
def test_valid_exact_calls_do_not_mutate_caller_arguments(operation, args):
    original = copy.deepcopy(args)
    assert contract.validate_arguments(operation, args) == original
    assert args == original


@pytest.mark.parametrize("field", ["principal_id", "workspace_id", "client_id", "grant_id",
    "trusted", "dispatch_permitted", "delivery", "state", "created_at", "acceptor_principal"])
@pytest.mark.parametrize("operation,args", CALLS)
def test_identity_state_and_dispatch_authority_cannot_be_supplied(operation, args, field):
    with pytest.raises(ValueError):
        contract.validate_arguments(operation, {**args, field: "injected"})


@pytest.mark.parametrize("operation,args", CALLS)
def test_every_declared_argument_is_required(operation, args):
    for omitted in args:
        with pytest.raises(ValueError):
            contract.validate_arguments(operation, {k: v for k, v in args.items() if k != omitted})


@pytest.mark.parametrize("change", [
    {"revision": True}, {"revision": 0}, {"revision": "1"},
    {"intent_id": "intent_" + "I" * 26},
    {"canonical_intent_digest": "A" * 64}, {"release_digest": "sha256:" + "b" * 64},
    {"expected_revision": REVISION.rsplit("@revise:", 1)[0]},
    {"expected_revision": REVISION + "\n"}, {"target": []},
    {"idempotency_key": "short"}, {"idempotency_key": "key\nnewline"},
])
def test_open_cannot_weaken_basis_or_malformed_identifiers(change):
    with pytest.raises(ValueError):
        contract.validate_arguments(contract.OPERATION_OPEN, {**OPEN, **change})


@pytest.mark.parametrize("change", [{"attempt_id": "attempt_" + "2" * 26 + "\n"},
    {"authorization_digest": True}, {"authorization_digest": "c" * 63},
    {"idempotency_key": []}])
def test_redemption_requires_exact_authorization_identity(change):
    with pytest.raises(ValueError):
        contract.validate_arguments(contract.OPERATION_REDEEM, {**REDEEM, **change})


@pytest.mark.parametrize("commit,url", [("D" * 40, "https://forge.example/pulls/1"),
    (True, "https://forge.example/pulls/1"), ("d" * 40, ""),
    ("d" * 40, "https://forge.example/pulls/1\n"), ("d" * 40, "\ud800"),
    ("d" * 40, "not-a-url"), ("d" * 40, "javascript:report")])
def test_report_rejects_malformed_claims_without_claiming_independent_success(commit, url):
    with pytest.raises(ValueError):
        contract.validate_arguments(contract.OPERATION_REPORT, {**REDEEM, "commit_sha": commit, "pr_url": url})


def test_only_attempt_and_declared_preview_descriptors_are_added_to_released_catalog():
    from sprintctl.vuoro_adapter import catalog_operation_specs
    baseline = json.loads((Path(__file__).parent / "fixtures/attempt-v0170-base-catalog.json").read_text())
    actual = {r["name"]: hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
              for r in catalog_operation_specs(resource_schema_available=True)}
    from sprintctl.effect_intent import OPERATION_PREVIEW
    assert set(actual) - set(baseline["operation_sha256"]) == set(contract.OPERATION_AUTHORITIES) | {OPERATION_PREVIEW}
    assert {k: actual[k] for k in baseline["operation_sha256"]} == baseline["operation_sha256"]


def test_catalog_attempt_capability_and_outer_idempotency_are_exact():
    from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
    operations = {c.name: c for c in WORK_OPERATION_CONTRACTS}
    for name in contract.OPERATION_AUTHORITIES:
        descriptor = operations[name]
        assert descriptor.required_authority == "work.effect.mark-applied"
        assert descriptor.idempotency == "not-allowed"
        assert descriptor.execution_semantics == ("read" if name == contract.OPERATION_GET else "write")
