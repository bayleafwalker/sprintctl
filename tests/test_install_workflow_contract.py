from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_readme_includes_sprintctl_and_kctl_install_and_upgrade_commands():
    readme = _read("README.md")
    for fragment in (
        "pipx install git+https://github.com/bayleafwalker/sprintctl.git",
        "pipx install git+https://github.com/bayleafwalker/kctl.git",
        "pipx upgrade sprintctl",
        "pipx upgrade kctl",
    ):
        assert fragment in readme


def test_contributing_pins_tool_refresh_workflow():
    contributing = _read("CONTRIBUTING.md")
    for fragment in (
        "pipx install git+https://github.com/bayleafwalker/sprintctl.git",
        "pipx install git+https://github.com/bayleafwalker/kctl.git",
        "pipx upgrade sprintctl",
        "pipx upgrade kctl",
        ".venv/bin/python -m sprintctl next-work --help",
        "repo-local module entrypoint",
    ):
        assert fragment in contributing
