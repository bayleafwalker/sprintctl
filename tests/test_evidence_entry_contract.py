"""Exact owned-chain reads are additive, read-only and identity-free on input."""
import jsonschema
import pytest

from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS

OPERATION = "work.evidence.entry-v1"
RUN = "run_" + "A" * 26


def test_entry_read_is_scoped_like_tail_without_mutation_or_identity_inputs():
    contract = next((c for c in WORK_OPERATION_CONTRACTS if c.name == OPERATION), None)
    assert contract is not None, "exact owned evidence lookup is missing"
    assert (contract.required_authority, contract.execution_semantics, contract.idempotency) == (
        "work:evidence", "read", "not-allowed")
    assert set(contract.input_schema["properties"]) == {"run_id", "chain_seq"}
    jsonschema.validate({"run_id": RUN, "chain_seq": 0}, contract.input_schema)
    for bad in ({"run_id": RUN, "chain_seq": -1}, {"run_id": RUN, "chain_seq": True},
                {"run_id": RUN, "chain_seq": 0, "principal_id": "someone-else"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, contract.input_schema)
