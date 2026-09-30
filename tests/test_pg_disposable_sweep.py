"""Oracle for scripts/pg-disposable-sweep.sh (agentops#2564, A2). No PostgreSQL needed.

Contract fixed by this oracle (the builder conforms to it, it may not edit it):

* ``scripts/pg-disposable-tests.sh`` writes an owner file ``<dir>/owner`` right
  after ``mktemp``. It holds one line ``<pid> <starttime>``: the script's pid
  and that pid's start time, field 22 of ``/proc/<pid>/stat`` (clock ticks
  since boot).
* ``scripts/pg-disposable-sweep.sh`` is executable, runs standalone, and scans
  uid-owned ``sprintctl-pg.*`` dirs under ``/tmp`` and under the tmp base
  (``$TMPDIR``, falling back to ``/tmp`` when longer than 60 chars). A dir whose
  owner pid is dead, or alive with a different start time, is stale: the sweep
  stops its cluster (``pg_ctl -D <dir>/data -m immediate stop``; if that fails,
  ``kill -9`` the ``postmaster.pid`` pid only when that process's cmdline names
  ``<dir>/data``), removes the dir and prints a line naming it. A dir whose
  owner is alive is never touched. A dir with no owner file is reported and
  left alone. The sweep exits 0.

``pg_ctl`` is replaced by a failing stub so the kill -9 guard is exercised the
same way on every host, with or without PostgreSQL installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "pg-disposable-sweep.sh"


def _start_ticks(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[19])


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=10)
    assert not Path(f"/proc/{proc.pid}").exists()
    return proc.pid


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _make_dir(base: Path, name: str, *, owner: str | None) -> Path:
    work = base / name
    (work / "data").mkdir(parents=True)
    (work / "sock").mkdir()
    if owner is not None:
        (work / "owner").write_text(owner + "\n")
    return work


def _write_postmaster_pid(work: Path, pid: int) -> None:
    data = work / "data"
    (data / "postmaster.pid").write_text(
        f"{pid}\n{data}\n{int(time.time())}\n54321\n{work / 'sock'}\n127.0.0.1\n  1234567    98304\nready   \n"
    )


def _fake_postmaster(data_dir: Path) -> subprocess.Popen:
    """A long-lived process whose cmdline names the data dir, like a postmaster."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", "-D", str(data_dir)],
    )


@pytest.fixture
def sweep_env():
    base = Path(tempfile.mkdtemp(prefix="spgsw-", dir="/tmp"))
    assert len(str(base)) <= 60
    stub_bin = base / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "pg_ctl"
    stub.write_text("#!/usr/bin/env bash\necho \"stub pg_ctl $*\" >&2\nexit 1\n")
    stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env["TMPDIR"] = str(base)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    procs: list[subprocess.Popen] = []
    try:
        yield base, env, procs
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(base, ignore_errors=True)


def _run_sweep(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SWEEP)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        env=env,
    )


def test_sweep_script_exists_and_is_executable():
    assert SWEEP.is_file(), f"missing {SWEEP.relative_to(ROOT)}"
    assert os.access(SWEEP, os.X_OK), f"{SWEEP.relative_to(ROOT)} is not executable"


def test_sweep_removes_stale_dirs_and_keeps_live_unowned_and_foreign_ones(sweep_env):
    base, env, procs = sweep_env
    me = os.getpid()
    my_start = _start_ticks(me)

    # Stale: owner pid is dead; its postmaster.pid names a live process whose
    # cmdline names this data dir, so that process must be killed.
    dead = _make_dir(base, "sprintctl-pg.deadAA", owner=f"{_dead_pid()} 12345")
    orphan = _fake_postmaster(dead / "data")
    procs.append(orphan)
    _write_postmaster_pid(dead, orphan.pid)

    # Stale: owner pid is dead; its postmaster.pid names an unrelated live
    # process (cmdline does not name the data dir), which must survive.
    decoy_dir = _make_dir(base, "sprintctl-pg.decoyB", owner=f"{_dead_pid()} 12345")
    decoy = subprocess.Popen(["sleep", "300"])
    procs.append(decoy)
    _write_postmaster_pid(decoy_dir, decoy.pid)

    # Stale: owner pid is alive but was reused (start time differs).
    reused = _make_dir(base, "sprintctl-pg.reusdC", owner=f"{me} {my_start + 100000}")

    # Live: owner is this test process with its real start time. Its
    # "postmaster" must not be touched either.
    live = _make_dir(base, "sprintctl-pg.liveDD", owner=f"{me} {my_start}")
    live_postmaster = _fake_postmaster(live / "data")
    procs.append(live_postmaster)
    _write_postmaster_pid(live, live_postmaster.pid)

    # No owner file: reported, left alone.
    unowned = _make_dir(base, "sprintctl-pg.noownE", owner=None)

    # Name does not match sprintctl-pg.*: ignored even with a dead owner.
    foreign = _make_dir(base, "sprintctl-other.FFFFFF", owner=f"{_dead_pid()} 12345")

    result = _run_sweep(env)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output

    assert not dead.exists(), output
    assert not decoy_dir.exists(), output
    assert not reused.exists(), output
    for swept in (dead, decoy_dir, reused):
        assert swept.name in output, f"no report line for {swept.name}:\n{output}"

    orphan.wait(timeout=15)
    assert orphan.returncode is not None
    assert _alive(decoy), "sweep killed a pid whose cmdline does not name the data dir"

    assert live.exists() and (live / "owner").exists(), output
    assert (live / "data" / "postmaster.pid").exists(), output
    assert _alive(live_postmaster), "sweep touched the cluster of a live owner"

    assert unowned.exists(), output
    assert unowned.name in output, f"dir without owner file not reported:\n{output}"

    assert foreign.exists(), output
    assert (foreign / "owner").exists()


def test_sweep_with_nothing_to_do_exits_zero(sweep_env):
    base, env, _procs = sweep_env
    result = _run_sweep(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(base.glob("sprintctl-pg.*")) == []
