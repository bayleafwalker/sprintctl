"""Static contract oracle for the disposable-PostgreSQL harness (agentops#2564).

Covers acceptance A3 (every fixture cleanup goes through the shared evidence
helper), A5 (pinned PostgreSQL), A6 (hard-kill timeout in the dispatch
manifest), A7 (CI parity) and A9 (guide). Runtime behaviour is covered by
tests/test_pg_cleanup_report_check.py, tests/test_pg_disposable_sweep.py and
verification/validate_pg_disposable_harness.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pg-disposable-tests.sh"
MANIFEST = ROOT / "sprintctl.dispatch.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
GUIDE = ROOT / "docs" / "guides" / "postgres-integration-tests.md"
PINNED_REV = "b4fd65b198c599cbe814fcb9f42d25d021595ec9"
PINNED_REF = f"github:NixOS/nixpkgs/{PINNED_REV}#postgresql_16"
HARD_KILL_COMMAND = "timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh"


# --- A3 ---------------------------------------------------------------------

def test_fixture_cleanups_do_not_call_cleanup_test_repositories_directly():
    offenders = []
    for relative in ("tests/pg/_shared.py", "tests/test_work_application_pg.py"):
        for number, line in enumerate((ROOT / relative).read_text().splitlines(), 1):
            if "cleanup_test_repositories(" in line:
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, "direct cleanup calls bypass the evidence helper:\n" + "\n".join(offenders)


# --- A5 ---------------------------------------------------------------------

def test_script_pins_postgresql_16_to_the_fixed_nixpkgs_rev():
    text = SCRIPT.read_text()
    assert text.count("nixpkgs#postgresql_16") == 0, "unpinned nixpkgs#postgresql_16 still referenced"
    assert PINNED_REV in text
    assert PINNED_REF in text


def test_script_uses_the_shared_checker_and_sweep():
    text = SCRIPT.read_text()
    assert "pg-cleanup-report-check.py" in text
    assert "pg-disposable-sweep.sh" in text
    assert "server_version_num" in text


# --- A6 ---------------------------------------------------------------------

def test_dispatch_manifest_runs_the_script_under_a_hard_kill_timeout():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["hybrid"]["commands"]["sprintctl.pg.disposable"] == HARD_KILL_COMMAND
    entries = [
        command
        for command in manifest["verification"]["commands"]
        if "pg-disposable-tests.sh" in command
    ]
    assert entries == [HARD_KILL_COMMAND]


# --- A7 ---------------------------------------------------------------------

def _job_block(text: str, job: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(f"  {job}:")
    block = []
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line) or re.match(r"^\S", line):
            break
        block.append(line)
    return block


def _steps(block: list[str]) -> list[str]:
    steps: list[list[str]] = []
    in_steps = False
    for line in block:
        if re.match(r"^    steps:\s*$", line):
            in_steps = True
            continue
        if not in_steps:
            continue
        if re.match(r"^    \S", line):
            break
        if re.match(r"^      - ", line):
            steps.append([line])
        elif steps:
            steps[-1].append(line)
    return ["\n".join(step) for step in steps]


def test_ci_yaml_parses_with_safe_load():
    # PyYAML is not a project dependency, so parse in an isolated uv env.
    uv = shutil.which("uv")
    assert uv, "uv is required to parse ci.yml with yaml.safe_load"
    result = subprocess.run(
        [
            uv, "run", "--no-project", "--with", "pyyaml", "python", "-c",
            "import sys, yaml; doc = yaml.safe_load(open(sys.argv[1])); "
            "assert 'postgres-integration' in doc['jobs'], sorted(doc['jobs'])",
            str(CI),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_postgres_job_runs_the_script_suite_then_the_shared_checker():
    block = _job_block(CI.read_text(), "postgres-integration")
    job = "\n".join(block)
    assert "image: postgres:16" in job, "CI must keep the postgres:16 service container"
    steps = _steps(block)
    assert steps, "postgres-integration job has no steps"

    pytest_steps = [
        index
        for index, step in enumerate(steps)
        if re.search(r"\bpytest\s", step) and "pg-cleanup-report-check.py" not in step
    ]
    assert len(pytest_steps) == 1, steps
    pytest_step = steps[pytest_steps[0]]
    for token in ("-m pg", "-rs", "tests/pg/", "tests/test_work_application_pg.py"):
        assert token in pytest_step, f"pytest step lacks {token!r}:\n{pytest_step}"

    checker_steps = [
        index for index, step in enumerate(steps) if "scripts/pg-cleanup-report-check.py" in step
    ]
    assert checker_steps and min(checker_steps) > pytest_steps[0], (
        "scripts/pg-cleanup-report-check.py must run in a step after the pytest step"
    )

    upload = [step for step in steps if "actions/upload-artifact" in step]
    assert len(upload) == 1, "cleanup evidence upload step is missing"
    assert "pg-cleanup-report.json" in upload[0]
    assert "if-no-files-found: error" in upload[0]
    assert "SPRINTCTL_TEST_PG_CLEANUP_REPORT: pg-cleanup-report.json" in job


# --- A9 ---------------------------------------------------------------------

def _paragraphs() -> list[str]:
    text = GUIDE.read_text().replace("\\", "")
    return [" ".join(chunk.split()) for chunk in re.split(r"\n\s*\n", text)]


def _paragraph_with(*patterns: str) -> str | None:
    for paragraph in _paragraphs():
        if all(re.search(pattern, paragraph, re.IGNORECASE) for pattern in patterns):
            return paragraph
    return None


def test_guide_states_pg_and_sprintctl_variable_clearing():
    assert _paragraph_with(r"PG\*", r"clear"), "guide must say every PG* variable is cleared"
    assert _paragraph_with(r"SPRINTCTL_\*", r"clear", r"SPRINTCTL_TEST_PG_\*"), (
        "guide must say every SPRINTCTL_* variable except SPRINTCTL_TEST_PG_* is cleared"
    )


def test_guide_states_long_tmpdir_falls_back_to_tmp():
    assert _paragraph_with(r"TMPDIR", r"\b60\b", r"/tmp\b")


def test_guide_documents_pinned_rev_and_major_16_check():
    text = GUIDE.read_text()
    assert PINNED_REV in text
    assert "nix shell nixpkgs#postgresql_16" not in text
    assert _paragraph_with(r"major[ -]?16|server_version_num"), "guide must document the major-16 check"


def test_guide_documents_hard_kill_command():
    assert HARD_KILL_COMMAND in " ".join(GUIDE.read_text().split())


def test_guide_documents_owner_file_and_sweep():
    assert _paragraph_with(r"pg-disposable-sweep\.sh", r"owner")


def test_guide_documents_json_lines_evidence_and_checker():
    text = " ".join(GUIDE.read_text().split())
    assert re.search(r"JSON Lines|JSONL", text), "guide must describe JSON Lines evidence"
    assert "sprintctl-pg-cleanup/v2" in text
    assert _paragraph_with(r"\bstarted\b", r"\bfinished\b")
    assert "pg-cleanup-report-check.py" in text


def test_guide_says_to_remove_report_before_manual_run():
    assert _paragraph_with(r"\b(remove|delete|rm)\b", r"report", r"manual|by hand|before"), (
        "guide must tell people to remove the report file before a manual run"
    )
