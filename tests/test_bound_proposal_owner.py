"""Closed bound basis and preservation of the attested 0.14.2 catalog."""
import copy
import hashlib
import json
from pathlib import Path
import jsonschema
import pytest
from sprintctl.effect_causal import validate_basis
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS, catalog_operation_specs
from tests.test_effect_intent_contract import PROPOSE_ARGS

BOUND = 'work.effect.propose-bound-v1'
REVISION = 'item:00000000-0000-0000-0000-000000000001@description:v0@sha256:' + 'a' * 64 + '@revise:0'
BASIS = {'expected_revision': REVISION, 'release_digest': 'a' * 64,
         'reserve_idempotency_key': 'reserve-key-1', 'commit_sha': 'b' * 40,
         'evidence_tail': {'item_id': 'tail-1', 'chain_seq': 0, 'entry_digest': 'sha256:' + 'c' * 64}}

@pytest.mark.parametrize('field,value', [
    ('expected_revision', REVISION.rsplit('@revise:', 1)[0]),
    ('release_digest', 'sha256:' + 'a' * 64),
    ('reserve_idempotency_key', 'short'), ('commit_sha', 'b' * 64),
    ('evidence_tail', {'item_id': 'tail-1', 'chain_seq': True, 'entry_digest': 'sha256:' + 'c' * 64}),
    ('evidence_tail', {'item_id': 'tail-1', 'chain_seq': 0, 'entry_digest': 'c' * 64}),
    ('evidence_tail', {'item_id': 'tail-1', 'chain_seq': 0, 'entry_digest': 'sha256:' + 'c' * 64, 'extra': 1}),
])
def test_invalid_basis_is_refused(field, value):
    with pytest.raises(ValueError): validate_basis({**BASIS, field: value})


def test_closed_basis_and_copied_tail():
    for bad in ({}, {**BASIS, 'extra': 1}, None):
        with pytest.raises(ValueError): validate_basis(bad)
    result = validate_basis(BASIS)
    result['evidence_tail']['item_id'] = 'changed'
    assert BASIS['evidence_tail']['item_id'] == 'tail-1'


def test_bound_contract_requires_all_guards_and_proposal_authority():
    contracts = {c.name: c for c in WORK_OPERATION_CONTRACTS}
    contract = contracts[BOUND]
    assert contract.required_authority == contracts['work.effect.propose-v1'].required_authority == 'work.effect.propose'
    args = {**PROPOSE_ARGS, 'causal_basis': copy.deepcopy(BASIS)}
    jsonschema.validate(args, contract.input_schema)
    for field in BASIS:
        bad = copy.deepcopy(args); del bad['causal_basis'][field]
        with pytest.raises(jsonschema.ValidationError): jsonschema.validate(bad, contract.input_schema)
    with pytest.raises(jsonschema.ValidationError): jsonschema.validate(PROPOSE_ARGS, contract.input_schema)
    with pytest.raises(jsonschema.ValidationError): jsonschema.validate(args, contracts['work.effect.propose-v1'].input_schema)


def test_only_catalog_addition_and_every_existing_descriptor_byte_preserved():
    baseline = json.loads((Path(__file__).parent / 'fixtures/bound-proposal-v0142-base-catalog.json').read_text())
    actual = {entry['name']: hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
              for entry in catalog_operation_specs(resource_schema_available=True)}
    from sprintctl.effect_attempt import OPERATION_AUTHORITIES
    assert set(actual) - set(baseline['operation_sha256']) == {BOUND, "work.evidence.evaluate-v1"} | set(OPERATION_AUTHORITIES)
    assert {k: actual[k] for k in baseline['operation_sha256']} == baseline['operation_sha256']
