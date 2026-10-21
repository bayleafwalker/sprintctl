"""Oracle for the disposable sweep (#2564 A2, amended by agentops#2834).

No PostgreSQL needed. Contract:

* ``scripts/pg-disposable-tests.sh`` writes an owner file ``<dir>/owner`` right
  after ``mktemp``. It holds ``<pid> <starttime> <pid-namespace>``: the script's
  pid and start time, field 22 of ``/proc/<pid>/stat`` (clock ticks
  since boot), plus its ``pid:[inode]`` namespace identity.
* ``scripts/pg-disposable-sweep.sh`` is executable, runs standalone, and scans
  uid-owned ``sprintctl-pg.*`` dirs under ``/tmp`` and under the tmp base
  (``$TMPDIR``, falling back to ``/tmp`` when longer than 60 chars). The
  recorded namespace must match the current known namespace before any
  liveness check. Missing/legacy/malformed/foreign namespaces are preserved.
  An owner pid that is dead, or alive with a different start time, is stale:
  the sweep stops its cluster (``pg_ctl -D <dir>/data -m immediate stop``; if that fails,
  ``kill -9`` the ``postmaster.pid`` pid only when that process's cmdline names
  ``<dir>/data``), removes the dir and prints a line naming it. A dir whose
  owner is alive is never touched. A dir with no owner file is reported and
  left alone. The sweep exits 0.

Every fixture supplies ``--scan-base`` to replace all default roots; no test
scans the real ``/tmp``. ``pg_ctl`` is replaced by a failing stub so the kill -9
guard is exercised the same way on every host, with or without PostgreSQL installed.
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
        (work / "owner").write_text(owner + " " + os.readlink("/proc/self/ns/pid") + "\n")
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
        [str(SWEEP), "--scan-base", env["TMPDIR"]],
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


@pytest.mark.parametrize("owner_kind", ["dead", "reused"])
@pytest.mark.parametrize("namespace_kind", ["foreign", "unknown", "malformed", "legacy"])
def test_ambiguous_namespace_preserves_directory_and_postmaster(
    sweep_env, owner_kind, namespace_kind
):
    base, env, procs = sweep_env
    pid = _dead_pid() if owner_kind == "dead" else os.getpid()
    owner = f"{pid} {_start_ticks(os.getpid()) + 100000}"
    work = _make_dir(base, "sprintctl-pg.nsSAFE", owner=None)
    namespace = os.readlink("/proc/self/ns/pid")
    inode = int(namespace.removeprefix("pid:[").removesuffix("]"))
    suffix = {
        "foreign": f" pid:[{inode + 1}]",
        "unknown": " unknown",
        "malformed": " pid:[bad]",
        "legacy": "",
    }[namespace_kind]
    (work / "owner").write_text(owner + suffix + "\n")
    postmaster = _fake_postmaster(work / "data")
    procs.append(postmaster)
    _write_postmaster_pid(work, postmaster.pid)

    result = _run_sweep(env)
    assert result.returncode == 0, result.stderr
    assert work.exists(), result.stdout
    assert _alive(postmaster), result.stdout
    assert "PID namespace" in result.stdout


def test_unknown_sweeper_namespace_preserves_same_namespace_owner(sweep_env):
    base, env, _procs = sweep_env
    work = _make_dir(base, "sprintctl-pg.unknown", owner=f"{_dead_pid()} 12345")
    stub = base / "stub-bin" / "readlink"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    stub.chmod(0o755)
    result = _run_sweep(env)
    assert result.returncode == 0 and work.exists(), result.stdout + result.stderr
    assert "PID namespace" in result.stdout


def test_explicit_scan_base_replaces_ambient_tmpdir(sweep_env):
    base, env, _procs = sweep_env
    included = base / "included"
    excluded = base / "excluded"
    included.mkdir()
    excluded.mkdir()
    swept = _make_dir(included, "sprintctl-pg.included", owner=f"{_dead_pid()} 12345")
    kept = _make_dir(excluded, "sprintctl-pg.excluded", owner=f"{_dead_pid()} 12345")
    env["TMPDIR"] = str(excluded)
    result = subprocess.run(
        [str(SWEEP), "--scan-base", str(included)],
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert not swept.exists(), result.stdout
    assert kept.exists(), result.stdout
    assert str(excluded) not in result.stdout


def test_producer_records_namespace_identity_before_starting_postgres(tmp_path):
    source = (ROOT / "scripts" / "pg-disposable-tests.sh").read_text()
    owner_code = source.split("# Owner file for the sweep:", 1)[1].split('data="$work/data"', 1)[0]
    # Execute only the owner-file section, without invoking the startup sweep
    # or PostgreSQL; the comment's first line is intentionally omitted.
    owner_code = owner_code.split("\n", 1)[1]
    subprocess.run(
        ["bash", "-eu", "-c", 'work="$1"\n' + owner_code, "owner-test", str(tmp_path)],
        check=True, timeout=5,
    )
    pid, start, namespace = (tmp_path / "owner").read_text().split()
    assert int(pid) > 0 and int(start) > 0
    assert namespace == os.readlink("/proc/self/ns/pid")


@pytest.mark.parametrize("owner_kind", ["dead", "live"])
def test_unknown_owner_start_time_is_preserved(sweep_env, owner_kind):
    base, env, _procs = sweep_env
    pid = _dead_pid() if owner_kind == "dead" else os.getpid()
    work = _make_dir(base, "sprintctl-pg.unknownStart", owner=f"{pid} unknown")
    result = _run_sweep(env)
    assert result.returncode == 0 and work.exists(), result.stdout + result.stderr
    assert "unknown owner start time" in result.stdout


@pytest.mark.parametrize("arguments", [["--scan-base"], ["--scan-base", ""], ["--unknown"]])
def test_invalid_scan_arguments_refuse_before_scanning(sweep_env, arguments):
    base, env, _procs = sweep_env
    work = _make_dir(base, "sprintctl-pg.invalidArgs", owner=f"{_dead_pid()} 12345")
    result = subprocess.run(
        [str(SWEEP), "--scan-base", str(base), *arguments],
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert result.returncode == 2
    assert work.exists()
    assert result.stdout == ""
    assert "usage:" in result.stderr
