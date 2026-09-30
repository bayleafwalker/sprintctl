#!/usr/bin/env python3
"""Check disposable-PostgreSQL cleanup evidence and pytest output.

Usage: pg-cleanup-report-check.py REPORT PYTEST_OUTPUT

REPORT is the SPRINTCTL_TEST_PG_CLEANUP_REPORT file: JSON Lines, schema
sprintctl-pg-cleanup/v2, with a ``started`` record at fixture setup and a
``finished`` record at teardown (see sprintctl/pg_testing.py). PYTEST_OUTPUT is
the captured pytest console output.

Exit 0 only when every line parses, at least one fixture reported, every started
fixture has exactly one finished record with cleanup_completed true and every
remaining_rows value zero, and the pytest output shows at least one passed test
and no skipped test. On success it prints one count line per fixture name.
Shared by scripts/pg-disposable-tests.sh and the CI postgres-integration job.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

SCHEMA = "sprintctl-pg-cleanup/v2"
PREFIX = "pg-cleanup-report-check"


def check_report(path: Path, problems: list[str]) -> Counter:
    counts: Counter = Counter()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"cleanup report unreadable ({path}): {exc}")
        return counts
    if not lines:
        problems.append(f"cleanup report is empty ({path}): no fixture reported")
        return counts

    started: Counter = Counter()
    finished: dict[tuple[str, str], list[dict]] = {}
    for number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except ValueError as exc:
            problems.append(f"report line {number} is not valid JSON: {exc}")
            continue
        if not isinstance(record, dict):
            problems.append(f"report line {number} is not a JSON object")
            continue
        if record.get("schema_version") != SCHEMA:
            problems.append(
                f"report line {number}: schema_version {record.get('schema_version')!r}, expected {SCHEMA!r}"
            )
            continue
        event, nodeid, fixture = record.get("event"), record.get("nodeid"), record.get("fixture")
        if event not in ("started", "finished") or not isinstance(nodeid, str) or not isinstance(fixture, str) \
                or not nodeid or not fixture:
            problems.append(f"report line {number}: needs event started|finished plus nodeid and fixture")
            continue
        key = (nodeid, fixture)
        if event == "started":
            started[key] += 1
        else:
            finished.setdefault(key, []).append(record)

    for key, number in sorted(started.items()):
        nodeid, fixture = key
        label = f"fixture {fixture} ({nodeid})"
        if number != 1:
            problems.append(f"{label}: {number} started records (remove the report before a manual run)")
        records = finished.get(key, [])
        if len(records) != 1:
            problems.append(f"{label}: {len(records)} finished records, expected exactly 1")
            continue
        record = records[0]
        if record.get("cleanup_completed") is not True:
            problems.append(f"{label}: cleanup_completed is not true (error_type {record.get('error_type')!r})")
            continue
        rows = record.get("remaining_rows")
        if not isinstance(rows, dict) or not rows:
            problems.append(f"{label}: remaining_rows missing")
            continue
        left = {table: value for table, value in rows.items() if value != 0}
        if left:
            problems.append(f"{label}: cleanup residue {left}")
            continue
        counts[fixture] += 1
    for key in sorted(set(finished) - set(started)):
        problems.append(f"fixture {key[1]} ({key[0]}): finished record without a started record")
    if not started and not problems:
        problems.append("cleanup report has no started fixture: no fixture reported")
    return counts


def check_pytest_output(path: Path, problems: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        problems.append(f"pytest output unreadable ({path}): {exc}")
        return
    skipped = re.findall(r"\b(\d+) skipped\b", text)
    if skipped or re.search(r"^SKIPPED\b", text, re.MULTILINE):
        problems.append(f"pytest output shows skipped tests ({', '.join(skipped) or 'SKIPPED lines'} skipped)")
    if not re.search(r"\b[1-9]\d* passed\b", text):
        problems.append("pytest output shows no passed tests")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} REPORT PYTEST_OUTPUT", file=sys.stderr)
        return 2
    problems: list[str] = []
    counts = check_report(Path(argv[1]), problems)
    check_pytest_output(Path(argv[2]), problems)
    if problems:
        for problem in problems:
            print(f"{PREFIX}: FAIL: {problem}", file=sys.stderr)
        return 1
    for fixture, number in sorted(counts.items()):
        print(f"{PREFIX}: {fixture}: {number} fixture cleanup(s) completed with zero remaining rows")
    print(f"{PREFIX}: ok: {sum(counts.values())} fixture cleanup(s) across {len(counts)} fixture name(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
