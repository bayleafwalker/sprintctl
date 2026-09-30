"""End-to-end oracle for scripts/pg-disposable-tests.sh (agentops#2564, A4 and A8).

Needs PostgreSQL 16 binaries on PATH or nix (the script provides them from the
pinned nixpkgs rev). It is not collected by pytest; run it explicitly:

    timeout --foreground -k 30s 1800s python3 verification/validate_pg_disposable_harness.py

A4: ``timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh`` exits 0,
prints a PostgreSQL 16 server version and one checker count line for each of
pg_test_scope, store_factory and maintenance_resource_transactional_pg_factory,
and leaves no sprintctl-pg.* dir behind.

A8: the script is started with a short TMPDIR and SIGKILLed once
``<dir>/data/postmaster.pid`` names a live postmaster. The postmaster must
outlive the script (it is daemonized). ``scripts/pg-disposable-sweep.sh`` with the
same TMPDIR must then stop that postmaster and remove the dir. The owner-file
contract (``<dir>/owner`` = ``<pid> <starttime>``) is the one fixed by
tests/test_pg_disposable_sweep.py.

Exit 0 when both pass; any failure raises. This validator cleans up only the
dirs it created under its own short TMPDIR, and kills only a postmaster whose
cmdline names one of those data dirs.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_COMMAND = ["timeout", "--foreground", "-k", "30s", "900s", "scripts/pg-disposable-tests.sh"]
SWEEP = ROOT / "scripts" / "pg-disposable-sweep.sh"
FIXTURES = ("pg_test_scope", "store_factory", "maintenance_resource_transactional_pg_factory")
VERSION_LINE = re.compile(r"(?i)(postgres|server[ _]version).*\b16(\.\d+)?\b")


def _short_base(prefix: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    if len(str(base)) > 60:
        raise AssertionError(f"tmp base {base} is too long for the harness")
    return base


def _env(base: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env["TMPDIR"] = str(base)
    return env


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _tmp_listing() -> set[str]:
    return {path.name for path in Path("/tmp").glob("sprintctl-pg.*")}


def _emergency_cleanup(base: Path) -> None:
    for work in base.glob("sprintctl-pg.*"):
        pid_file = work / "data" / "postmaster.pid"
        try:
            pid = int(pid_file.read_text().splitlines()[0])
        except (OSError, ValueError, IndexError):
            pid = 0
        if pid and str(work / "data") in _cmdline(pid):
            os.kill(pid, signal.SIGKILL)
    shutil.rmtree(base, ignore_errors=True)


def check_a4() -> None:
    base = _short_base("spga4-")
    before = _tmp_listing()
    try:
        result = subprocess.run(
            SCRIPT_COMMAND,
            cwd=ROOT,
            env=_env(base),
            capture_output=True,
            text=True,
            timeout=1000,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            raise AssertionError(f"A4: script exited {result.returncode}:\n{output[-6000:]}")
        lines = output.splitlines()
        if not any(VERSION_LINE.search(line) for line in lines):
            raise AssertionError(f"A4: no PostgreSQL 16 server version in output:\n{output[-6000:]}")
        for fixture in FIXTURES:
            if not any(fixture in line and re.search(r"\b\d+\b", line) for line in lines):
                raise AssertionError(f"A4: no checker count line naming {fixture}:\n{output[-6000:]}")
        left = sorted(str(path) for path in base.glob("sprintctl-pg.*"))
        left += sorted(f"/tmp/{name}" for name in _tmp_listing() - before)
        if left:
            raise AssertionError(f"A4: run left dirs behind: {left}")
        print("A4 ok: script exited 0 with PostgreSQL 16, per-fixture counts, no residue")
    finally:
        _emergency_cleanup(base)


def _wait_for_postmaster(base: Path, proc: subprocess.Popen, deadline: float) -> tuple[Path, int]:
    saw_unowned = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            if saw_unowned:
                raise AssertionError("A8: the run's dir never got an owner file (<dir>/owner)")
            raise AssertionError(f"A8: script exited ({proc.returncode}) before the server started")
        for work in base.glob("sprintctl-pg.*"):
            pid_file = work / "data" / "postmaster.pid"
            try:
                pid = int(pid_file.read_text().splitlines()[0])
            except (OSError, ValueError, IndexError):
                continue
            if _alive(pid) and (work / "owner").is_file():
                return work, pid
            saw_unowned = saw_unowned or _alive(pid)
        time.sleep(0.2)
    raise AssertionError("A8: postmaster.pid with a live postmaster never appeared")


def check_a8() -> None:
    if not SWEEP.is_file():
        raise AssertionError(f"A8: missing {SWEEP.relative_to(ROOT)}")
    base = _short_base("spga8-")
    proc = subprocess.Popen(
        ["scripts/pg-disposable-tests.sh"],
        cwd=ROOT,
        env=_env(base),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        work, postmaster = _wait_for_postmaster(base, proc, time.monotonic() + 600)
        owner_pid = int((work / "owner").read_text().split()[0])
        # SIGKILL the script itself (and the process that launched it, when
        # nix shell sits in between); traps cannot run.
        for pid in {owner_pid, proc.pid}:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=30)
        time.sleep(2)
        if not _alive(postmaster):
            raise AssertionError("A8: postmaster died with the script; the scenario did not orphan it")
        if not work.exists():
            raise AssertionError("A8: dir vanished although the script was SIGKILLed")

        sweep = subprocess.run(
            [str(SWEEP)], cwd=ROOT, env=_env(base), capture_output=True, text=True, timeout=120
        )
        output = sweep.stdout + sweep.stderr
        if sweep.returncode != 0:
            raise AssertionError(f"A8: sweep exited {sweep.returncode}:\n{output}")
        deadline = time.monotonic() + 30
        while _alive(postmaster) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _alive(postmaster):
            raise AssertionError(f"A8: postmaster {postmaster} still alive after sweep:\n{output}")
        left = sorted(str(path) for path in base.glob("sprintctl-pg.*"))
        if left:
            raise AssertionError(f"A8: sweep left dirs behind: {left}\n{output}")
        print("A8 ok: orphaned postmaster survived SIGKILL of the script and the sweep removed it")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        _emergency_cleanup(base)


def main() -> int:
    failures = []
    for check in (check_a4, check_a8):
        try:
            check()
        except AssertionError as exc:
            failures.append(str(exc))
            print(f"FAIL {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
