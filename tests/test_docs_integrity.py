from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_phase4_docs_files_exist():
    expected_files = [
        "docs/customization.md",
        "docs/advanced/coordinator-mode.md",
        "docs/advanced/claim-discipline.md",
        "docs/examples/repo-template.md",
    ]
    missing = [path for path in expected_files if not (REPO_ROOT / path).exists()]
    assert missing == []


def test_readme_links_phase4_docs():
    readme = _read("README.md")
    assert "[Customization Guide](docs/customization.md)" in readme
    assert "[Coordinator Mode](docs/advanced/coordinator-mode.md)" in readme
    assert "[Claim Discipline](docs/advanced/claim-discipline.md)" in readme
    assert "[repo-template.md](docs/examples/repo-template.md)" in readme


def test_start_here_links_phase4_docs():
    start_here = _read("docs/guides/start-here.md")
    assert "[Customization Guide](../customization.md)" in start_here
    assert "[Coordinator Mode](../advanced/coordinator-mode.md)" in start_here
    assert "[Claim Discipline](../advanced/claim-discipline.md)" in start_here


def test_advanced_coordination_links_phase4_docs():
    advanced = _read("docs/guides/advanced-coordination.md")
    assert "[Coordinator Mode](../advanced/coordinator-mode.md)" in advanced
    assert "[Claim Discipline](../advanced/claim-discipline.md)" in advanced
