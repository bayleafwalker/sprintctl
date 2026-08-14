from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import tomllib

import pytest

from sprintctl.vuoro_adapter import (
    SCHEMA_DIALECT,
    SCHEMA_FEATURES,
    WORK_OPERATION_CONTRACTS,
    catalog_operation_specs,
)
from vuoro_adapter_kit import (
    SCHEMA_DIALECT as ADAPTER_SCHEMA_DIALECT,
    SCHEMA_FEATURES as ADAPTER_SCHEMA_FEATURES,
)


ROOT = Path(__file__).parents[1]
ADAPTER_URL = (
    "https://github.com/bayleafwalker/vuoro/releases/download/"
    "vuoro-adapter-kit-v0.1.0/"
    "vuoro_adapter_kit-0.1.0-py3-none-any.whl"
)
ADAPTER_DIGEST = "0037898a4c9f01720a42302365b0172ecd203732070326ea2abdf549a44bf0c2"


def test_shared_schema_metadata_and_owner_contract_order_are_preserved() -> None:
    assert SCHEMA_DIALECT == ADAPTER_SCHEMA_DIALECT
    assert SCHEMA_FEATURES == ADAPTER_SCHEMA_FEATURES

    available = catalog_operation_specs(resource_schema_available=True)
    assert [spec["name"] for spec in available] == [
        contract.name for contract in WORK_OPERATION_CONTRACTS
    ]
    for contract, spec in zip(WORK_OPERATION_CONTRACTS, available, strict=True):
        assert spec["owning_domain"] == "work"
        assert spec["input_schema"] == contract.input_schema
        assert spec["result_schema"] == contract.result_schema
        assert spec["required_authority"] == contract.required_authority
        assert spec["execution_semantics"] == contract.execution_semantics
        assert spec["idempotency"] == contract.idempotency
        assert spec["required_client_schema_features"] == list(
            contract.required_client_schema_features
        )
        assert spec["repo_scoped"] is not contract.name.startswith("work.project.")

    by_name = {spec["name"]: spec for spec in available}
    assert by_name["work.maintenance.resource.prepare"]["result_contract"] == {
        "mode": "resource-reference",
        "resource_kind": "work.maintenance-capability",
    }
    assert all(
        "result_contract" not in spec
        for name, spec in by_name.items()
        if name != "work.maintenance.resource.prepare"
    )


def test_catalog_specs_are_deeply_isolated_from_owner_contracts_and_each_other() -> None:
    first = catalog_operation_specs(resource_schema_available=True)
    first[0]["input_schema"]["properties"]["mutation"] = {"type": "string"}
    first[0]["required_client_schema_features"].append("mutation")

    second = catalog_operation_specs(resource_schema_available=True)
    assert "mutation" not in second[0]["input_schema"]["properties"]
    assert second[0]["required_client_schema_features"] == list(SCHEMA_FEATURES)
    assert "mutation" not in WORK_OPERATION_CONTRACTS[0].input_schema["properties"]


def test_resource_schema_gate_removes_exactly_the_three_owner_operations() -> None:
    available = catalog_operation_specs(resource_schema_available=True)
    unavailable = catalog_operation_specs(resource_schema_available=False)
    resource_names = {
        "work.maintenance.resource.prepare",
        "work.maintenance.resource.get",
        "work.maintenance.resource.changes",
    }

    assert len(available) == 47
    assert len(unavailable) == 44
    assert {spec["name"] for spec in available} - {
        spec["name"] for spec in unavailable
    } == resource_names
    assert not {spec["name"] for spec in available if spec["name"].startswith("work.claim.")}


def test_runtime_dependency_and_lock_select_one_immutable_adapter_wheel() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    requirements = [
        requirement
        for requirement in project["dependencies"]
        if requirement.startswith("vuoro-adapter-kit @ ")
    ]
    assert requirements == [
        f"vuoro-adapter-kit @ {ADAPTER_URL}#sha256={ADAPTER_DIGEST}"
    ]

    with (ROOT / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    package = [
        package
        for package in lock["package"]
        if package["name"] == "vuoro-adapter-kit"
    ]
    assert len(package) == 1
    assert package[0]["source"] == {"url": ADAPTER_URL}
    assert package[0]["wheels"] == [
        {"url": ADAPTER_URL, "hash": f"sha256:{ADAPTER_DIGEST}"}
    ]


def test_installed_distribution_metadata_preserves_adapter_url_and_digest() -> None:
    try:
        requirements = distribution("sprintctl").requires or []
    except PackageNotFoundError:
        pytest.skip("sprintctl is not installed as a distribution")
    assert any(
        requirement.startswith("vuoro-adapter-kit @ ")
        and ADAPTER_URL in requirement
        and ADAPTER_DIGEST in requirement
        for requirement in requirements
    )
