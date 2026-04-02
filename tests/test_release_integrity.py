import os
from pathlib import Path
import subprocess
import sys
import tomllib

from sprintctl import __version__
from sprintctl.cli import cli


ROOT = Path(__file__).resolve().parents[1]


class TestReleaseIntegrity:
    def test_cli_version_option_reports_package_version(self, runner, db_path):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0, result.output
        assert __version__ in result.output
        assert "sprintctl, version" in result.output

    def test_pyproject_console_script_points_to_cli_entrypoint(self):
        with (ROOT / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        assert pyproject["project"]["scripts"]["sprintctl"] == "sprintctl.cli:cli"

    def test_help_lists_current_resume_surface(self, runner, db_path):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0, result.output
        for command in ("usage", "handoff", "next-work", "session", "git-context", "claim", "maintain"):
            assert command in result.output

    def test_module_entrypoint_exposes_cli_help(self, db_path):
        env = os.environ.copy()
        env["SPRINTCTL_DB"] = str(db_path)
        result = subprocess.run(
            [sys.executable, "-m", "sprintctl", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "Usage: python -m sprintctl" in result.stdout
        for command in ("usage", "handoff", "next-work", "session", "git-context", "claim", "maintain"):
            assert command in result.stdout

    def test_module_entrypoint_reports_package_version(self, db_path):
        env = os.environ.copy()
        env["SPRINTCTL_DB"] = str(db_path)
        result = subprocess.run(
            [sys.executable, "-m", "sprintctl", "--version"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert __version__ in result.stdout
        assert "sprintctl, version" in result.stdout

    def test_module_entrypoint_usage_lists_next_work_explain(self, db_path):
        env = os.environ.copy()
        env["SPRINTCTL_DB"] = str(db_path)
        result = subprocess.run(
            [sys.executable, "-m", "sprintctl", "usage"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "next-work      [--sprint-id ID] [--json] [--explain]" in result.stdout
        assert "session resume [--sprint-id ID] [--json]" in result.stdout

    def test_module_entrypoint_next_work_help_includes_explain(self, db_path):
        env = os.environ.copy()
        env["SPRINTCTL_DB"] = str(db_path)
        result = subprocess.run(
            [sys.executable, "-m", "sprintctl", "next-work", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "--explain" in result.stdout

    def test_usage_reference_lists_current_contract_commands(self, runner, db_path):
        result = runner.invoke(cli, ["usage"])
        assert result.exit_code == 0, result.output
        assert f"sprintctl v{__version__}" in result.output
        for fragment in (
            "usage          [--context] [--sprint-id ID] [--json]",
            "handoff        [--sprint-id ID] [--output PATH] [--events N] [--format json|text]",
            "next-work      [--sprint-id ID] [--json] [--explain]",
            "session resume [--sprint-id ID] [--json]",
            "git-context",
            "sprint show    [--id ID] [--detail] [--watch] [--interval SECONDS] [--json]",
            "item list      [--sprint-id ID] [--track NAME] [--status STATUS] [--fzf] [--json]",
        ):
            assert fragment in result.output
