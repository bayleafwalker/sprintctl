"""Oracle for scripts/pg-cleanup-report-check.py (agentops#2564, A1).

Contract fixed by this oracle (the builder conforms to it, it may not edit it):

* Invocation: ``python3 scripts/pg-cleanup-report-check.py REPORT PYTEST_OUTPUT``
  where REPORT is the ``SPRINTCTL_TEST_PG_CLEANUP_REPORT`` file and
  PYTEST_OUTPUT is the captured pytest console output. Exit 0 means the
  evidence is complete; any other exit status is a failure and must come with
  a message on stdout or stderr.
* REPORT is JSON Lines (one JSON object per line), schema
  ``sprintctl-pg-cleanup/v2``. Every record carries ``schema_version``,
  ``event`` (``"started"`` or ``"finished"``), ``nodeid`` (the module nodeid,
  or the test nodeid for a function-scoped fixture) and ``fixture`` (the
  fixture name). A ``finished`` record also carries ``cleanup_completed`` and,
  when cleanup completed, ``remaining_rows`` (table -> count); a failed
  cleanup carries ``cleanup_completed: false`` plus ``error_type``.
* Every started (nodeid, fixture) needs exactly one finished record with
  ``cleanup_completed`` true and every ``remaining_rows`` value zero. At least
  one fixture must have reported. The pytest output must show no skipped tests
  and at least one passed test. On success the checker prints one count line
  per fixture name.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "pg-cleanup-report-check.py"
SCHEMA = "sprintctl-pg-cleanup/v2"
PASSED_OUTPUT = "........................................ [100%]\n412 passed in 31.07s\n"


def _started(nodeid: str, fixture: str) -> dict:
    return {"schema_version": SCHEMA, "event": "started", "nodeid": nodeid, "fixture": fixture}


def _finished(nodeid: str, fixture: str, *, remaining: dict | None = None, completed: bool = True) -> dict:
    record = {
        "schema_version": SCHEMA,
        "event": "finished",
        "nodeid": nodeid,
        "fixture": fixture,
        "cleanup_completed": completed,
    }
    if completed:
        record["repo_ids"] = ["itest-scope-0123456789ab"]
        record["deleted_rows"] = {"work_item": 3, "event": 7, "sprint": 1}
        record["remaining_rows"] = remaining if remaining is not None else {
            "work_item": 0,
            "event": 0,
            "sprint": 0,
        }
    else:
        record["error_type"] = "RuntimeError"
        record["repo_ids"] = ["itest-scope-0123456789ab"]
    return record


def _well_formed_records() -> list[dict]:
    """Two fixture names, three fixture instances, nested/interleaved like pytest."""
    return [
        _started("tests/pg/test_event.py", "pg_test_scope"),
        _finished("tests/pg/test_event.py", "pg_test_scope"),
        _started("tests/test_work_application_pg.py", "store_factory"),
        _started("tests/pg/test_work_item.py", "pg_test_scope"),
        _finished("tests/pg/test_work_item.py", "pg_test_scope"),
        _finished("tests/test_work_application_pg.py", "store_factory"),
    ]


def _write_report(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
    return path


def _run(report: Path, pytest_output: str, tmp_path: Path) -> subprocess.CompletedProcess:
    output = tmp_path / "pytest.out"
    output.write_text(pytest_output)
    return subprocess.run(
        [sys.executable, str(CHECKER), str(report), str(output)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )


def _message(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).strip()


def test_checker_script_exists():
    assert CHECKER.is_file(), f"missing {CHECKER.relative_to(ROOT)}"


def test_accepts_well_formed_two_fixture_report_and_prints_per_fixture_counts(tmp_path):
    report = _write_report(tmp_path / "report.jsonl", _well_formed_records())

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode == 0, _message(result)
    lines = result.stdout.splitlines()
    scope_lines = [line for line in lines if "pg_test_scope" in line]
    factory_lines = [line for line in lines if "store_factory" in line]
    assert scope_lines, f"no count line for pg_test_scope in:\n{result.stdout}"
    assert factory_lines, f"no count line for store_factory in:\n{result.stdout}"
    assert any(re.search(r"\b2\b", line) for line in scope_lines), scope_lines
    assert any(re.search(r"\b1\b", line) for line in factory_lines), factory_lines


def test_rejects_started_fixture_without_finished_record(tmp_path):
    records = _well_formed_records()
    records.append(_started("tests/test_work_application_pg.py::test_x[h]", "maintenance_resource_transactional_pg_factory"))
    report = _write_report(tmp_path / "report.jsonl", records)

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert "maintenance_resource_transactional_pg_factory" in _message(result)


def test_rejects_second_finished_record_for_one_started_fixture(tmp_path):
    records = _well_formed_records()
    records.append(_finished("tests/pg/test_event.py", "pg_test_scope"))
    report = _write_report(tmp_path / "report.jsonl", records)

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


def test_rejects_cleanup_completed_false(tmp_path):
    records = _well_formed_records()
    records[1] = _finished("tests/pg/test_event.py", "pg_test_scope", completed=False)
    report = _write_report(tmp_path / "report.jsonl", records)

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


def test_rejects_nonzero_remaining_rows(tmp_path):
    records = _well_formed_records()
    records[5] = _finished(
        "tests/test_work_application_pg.py",
        "store_factory",
        remaining={"work_item": 0, "event": 2, "sprint": 0},
    )
    report = _write_report(tmp_path / "report.jsonl", records)

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


def test_rejects_empty_report(tmp_path):
    report = tmp_path / "report.jsonl"
    report.write_text("")

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


def test_rejects_missing_report(tmp_path):
    result = _run(tmp_path / "absent.jsonl", PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


def test_rejects_unparseable_line(tmp_path):
    report = _write_report(tmp_path / "report.jsonl", _well_formed_records())
    with report.open("a") as handle:
        handle.write('{"schema_version": "sprintctl-pg-cleanup/v2", "event": "finis\n')

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


def test_rejects_legacy_single_object_v1_report(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": "sprintctl-pg-cleanup/v1",
                "cleanup_completed": True,
                "remaining_rows": {"work_item": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    result = _run(report, PASSED_OUTPUT, tmp_path)

    assert result.returncode != 0
    assert _message(result)


@pytest.mark.parametrize(
    "pytest_output",
    [
        "410 passed, 2 skipped in 30.50s\n",
        "SKIPPED [1] tests/pg/_shared.py:80: disposable database required\n1 skipped in 0.20s\n",
    ],
    ids=["passed-and-skipped", "only-skipped"],
)
def test_rejects_pytest_output_with_skipped_tests(tmp_path, pytest_output):
    report = _write_report(tmp_path / "report.jsonl", _well_formed_records())

    result = _run(report, pytest_output, tmp_path)

    assert result.returncode != 0
    assert "skip" in _message(result).lower()


def test_rejects_pytest_output_without_passed_tests(tmp_path):
    report = _write_report(tmp_path / "report.jsonl", _well_formed_records())

    result = _run(report, "no tests ran in 0.01s\n", tmp_path)

    assert result.returncode != 0
    assert _message(result)
