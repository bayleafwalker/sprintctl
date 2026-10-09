"""Typed owner admission and legacy compatibility, without authority transport."""

from types import SimpleNamespace

import jsonschema
import pytest

from sprintctl.application import ApplicationRejection
from sprintctl.vuoro_adapter import WORK_OPERATION_CONTRACTS
from tests.test_work_application import _application


def context(*, bound=True, authority=True, key="native-key-1"):
    return SimpleNamespace(
        identity=SimpleNamespace(
            actor="native-test",
            authorities={"work:write"} if authority else set(),
            principal_id="github:1:0" if bound else None,
            workspace_id="ws-test" if bound else None,
        ),
        idempotency_key=key,
        repo_id=None,
    )


@pytest.mark.parametrize(
    "bound,authority,code",
    [
        (False, False, "authority-required"),
        (False, True, "identity-unbound"),
        (True, True, "reservation-replay-unavailable"),
    ],
)
def test_native_reserve_refuses_without_bound_owner_backend(bound, authority, code):
    with pytest.raises(ApplicationRejection) as exc:
        _application().invoke(
            "work.reservation.reserve-v1", {}, context(bound=bound, authority=authority)
        )
    assert exc.value.code == code


def test_native_reserve_contract_is_separate_and_closed():
    contracts = {entry.name: entry for entry in WORK_OPERATION_CONTRACTS}
    native = contracts["work.reservation.reserve-v1"]
    legacy = contracts["work.reservation.reserve"]
    assert native.input_schema == legacy.input_schema
    assert native.result_schema == legacy.result_schema
    assert native.required_authority == "work:write"
    assert native.execution_semantics == "write" and native.idempotency == "required"
    args = {"item_id": 1, "actor": "native-test", "session_id": "session"}
    jsonschema.validate(args, native.input_schema)
    for extra in (
        {"role": None},
        {"idempotency_key": "native-key-1"},
        {"acceptance_contract": None},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**args, **extra}, native.input_schema)
    jsonschema.validate(
        {
            "repo_id": "test",
            "reservation": {"admission_snapshot": {}, "replayed": False},
        },
        native.result_schema,
    )


def test_native_reserve_is_only_catalog_addition_with_all_legacy_bytes_preserved():
    import hashlib
    import json
    from pathlib import Path
    from sprintctl.vuoro_adapter import catalog_operation_specs

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/reserve-v1-base-catalog.json").read_text()
    )
    actual = {
        entry["name"]: hashlib.sha256(
            json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for entry in catalog_operation_specs(resource_schema_available=True)
    }
    expected = fixture["operation_sha256"]
    from sprintctl.effect_attempt import OPERATION_AUTHORITIES
    assert set(actual) - set(expected) == {"work.reservation.reserve-v1", "work.effect.propose-bound-v1", "work.evidence.evaluate-v1"} | set(OPERATION_AUTHORITIES)
    assert not set(expected) - set(actual)
    assert {name: actual[name] for name in expected} == expected
