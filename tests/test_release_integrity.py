import os
from pathlib import Path
import subprocess
import sys
import tomllib

from sprintctl import CLI_CAPABILITIES, __version__
from sprintctl import doctor
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

    def test_served_extra_uses_digest_pinned_released_client_wheel(self):
        with (ROOT / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        requirement, = pyproject["project"]["optional-dependencies"]["served"]
        assert "vuoro-client-v0.1.1/vuoro_client-0.1.1-py3-none-any.whl" in requirement
        assert "sha256=b5fb6bad174abd00d67504398690bcfb8c3cc3be891e5465983827e5a1740f6d" in requirement
        assert "git+" not in requirement

    def test_remote_schema_cutover_is_a_minor_release_past_0_8(self):
        """0.8.0 is tagged against remote schema 17; a runtime whose minimum
        is 18 cannot serve a schema-17 authority, so it is 0.9.0 or later.
        0.10.0 keeps schema 18; it is a minor release because the adapter
        catalog changed (agentops#2539, agentops#2540). 0.10.1 is a patch:
        served runtime connection recovery (#2110), no catalog or schema change."""
        from sprintctl import pg_migrations

        with (ROOT / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        assert pyproject["project"]["version"] == __version__ == "0.10.1"
        assert pyproject["tool"]["sprintctl"]["remote-schema-version"] == 18
        assert pg_migrations.MINIMUM_SCHEMA_VERSION == pg_migrations.CURRENT_SCHEMA_VERSION == 18

    def test_release_contract_names_this_version(self):
        """The tag workflow validates the wheel against RELEASE_VERSION; a
        stale one is how v0.7.4 failed to release."""
        text = (ROOT / "verification" / "validate_release_contract.py").read_text()
        assert f'RELEASE_VERSION = "{__version__}"' in text

    def test_pyproject_doctor_capabilities_match_runtime(self):
        with (ROOT / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        settings = pyproject["tool"]["sprintctl"]
        assert pyproject["project"]["version"] == __version__
        assert sorted(settings["capabilities"]) == sorted(CLI_CAPABILITIES)
        assert settings["sqlite-schema-version"] == doctor.SQLITE_SCHEMA_VERSION
        assert settings["remote-schema-version"] == doctor.REMOTE_SCHEMA_VERSION

    def test_help_lists_current_resume_surface(self, runner, db_path):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0, result.output
        for command in ("doctor", "usage", "handoff", "next-work", "session", "git-context", "reservation", "maintain"):
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
        for command in ("doctor", "usage", "handoff", "next-work", "session", "git-context", "reservation", "maintain"):
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
            "doctor         [--json]",
            "usage          [--context] [--sprint-id ID] [--json]",
            "handoff        [--sprint-id ID] [--output PATH] [--events N] [--format json|text]",
            "next-work      [--sprint-id ID] [--json] [--explain]",
            "session resume [--sprint-id ID] [--json]",
            "git-context",
            "sprint show    [--id ID] [--detail] [--watch] [--interval SECONDS] [--json]",
            "item list      [--sprint-id ID] [--track NAME] [--status STATUS] [--fzf] [--json]",
            "event add      --sprint-id ID --type|--event-type TYPE --actor NAME [--item-id ID]",
            "event log      Alias for event add",
            "takeup take    --sprint-id ID --actor NAME [--instance-id ID] [--context TEXT]",
            "takeup         take|release|list|show|sweep",
        ):
            assert fragment in result.output
