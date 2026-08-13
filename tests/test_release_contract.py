from __future__ import annotations

from pathlib import Path

import pytest

from verification.validate_release_contract import (
    _adapter_requirement,
    _locked_adapter_requirement,
    _validate_adapter_pin,
    validate_wheel,
)


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-sprintctl.yaml"


def test_adapter_dependency_is_an_immutable_github_wheel_pin() -> None:
    requirement = _adapter_requirement()
    lock_url, lock_digest = _locked_adapter_requirement()
    digest = _validate_adapter_pin(requirement)

    assert requirement.split("#", 1)[0] == lock_url
    assert digest == lock_digest
    assert len(digest) == 64


def _assert_release_workflow(workflow: str) -> None:
    assert 'tags:\n      - "v*"' in workflow
    assert "workflow_dispatch" not in workflow
    assert "pypi" not in workflow.lower()
    assert "gh release upload" not in workflow
    assert "--clobber" not in workflow
    assert "uv publish" not in workflow
    assert "attestations: write" in workflow
    assert "contents: write" in workflow
    assert "id-token: write" in workflow


def _assert_release_order(workflow: str) -> None:
    assert workflow.count('"3.11"') == 1
    assert workflow.count('"3.12"') == 2
    assert workflow.count("uv build --wheel") == 1
    assert workflow.count("uses: actions/attest@v4") == 1
    assert "needs: tests" in workflow

    sync = workflow.index("uv sync --frozen --extra dev")
    full_suite = workflow.index("uv run pytest -q")
    build = workflow.index("uv build --wheel --out-dir dist")
    validation = workflow.index("verification/validate_release_contract.py")
    attestation = workflow.index("uses: actions/attest@v4")
    release_create = workflow.index('gh release create "$tag"')
    release_publish = workflow.index('gh release edit "$GITHUB_REF_NAME"')
    assert sync < full_suite < build < validation < attestation < release_create
    assert release_create < release_publish
    assert "--verify-tag" in workflow
    assert '"$wheel"' in workflow


def test_release_workflow_is_tag_only_github_only_and_attested() -> None:
    _assert_release_workflow(WORKFLOW.read_text(encoding="utf-8"))


def test_release_workflow_has_two_python_gates_and_one_gated_build() -> None:
    _assert_release_order(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "broken",
    [
        lambda workflow: workflow.replace(
            "uv sync --frozen --extra dev", "uv sync --frozen"
        ),
        lambda workflow: workflow.replace(
            "uv run pytest -q", "uv run pytest tests/test_release_contract.py"
        ),
        lambda workflow: workflow.replace(
            "          uv build --wheel --out-dir dist\n",
            "          uv build --wheel --out-dir dist\n"
            "          uv build --wheel --out-dir dist\n",
        ),
        lambda workflow: workflow.replace("            --verify-tag \\\n", ""),
        lambda workflow: workflow.replace(
            '            "$wheel"\n', '            --clobber "$wheel"\n'
        ),
        lambda workflow: workflow.replace("  contents: write", "  contents: read"),
        lambda workflow: workflow.replace("  id-token: write", "  id-token: read"),
    ],
    ids=(
        "partial-sync",
        "partial-suite",
        "second-build",
        "missing-verify-tag",
        "clobber-release-asset",
        "read-only-release-permission",
        "read-only-attestation-permission",
    ),
)
def test_release_workflow_contract_rejects_regressions(broken) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    with pytest.raises((AssertionError, ValueError)):
        mutated = broken(workflow)
        _assert_release_workflow(mutated)
        _assert_release_order(mutated)


@pytest.mark.skipif(
    not list((ROOT / "dist").glob("*.whl")),
    reason="release wheel is built by the release workflow",
)
def test_built_wheel_satisfies_release_contract() -> None:
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    assert len(wheels) == 1
    validate_wheel(wheels[0], tag=f"v{wheels[0].name.split('-')[1]}")
