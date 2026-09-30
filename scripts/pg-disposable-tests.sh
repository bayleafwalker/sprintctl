#!/usr/bin/env bash
# Run the disposable-PostgreSQL integration suite (tests/pg/ and
# tests/test_work_application_pg.py) against a throwaway local server.
#
# Safety contract: docs/guides/postgres-integration-tests.md. This script
# creates its own cluster in a mktemp dir, listens on a unix socket in that dir
# plus 127.0.0.1 on a free port, and never connects to any other database. The
# role password is generated per run and never written to the repository.
# Postgres binaries come from the pinned nixpkgs rev below (PostgreSQL 16.15,
# the rev gitops-nixos/flake.lock pins) when absent; the server must be major 16
# either way. Run under `timeout --foreground -k 30s 900s` so a hung run is
# killed; a hard kill leaves a stale cluster that the next run's sweep
# (scripts/pg-disposable-sweep.sh, keyed by <dir>/owner) stops and removes.
set -euo pipefail

PG_NIX_REF="github:NixOS/nixpkgs/b4fd65b198c599cbe814fcb9f42d25d021595ec9#postgresql_16"

if ! command -v initdb >/dev/null 2>&1 || ! command -v pg_ctl >/dev/null 2>&1; then
  command -v nix >/dev/null 2>&1 || { echo "pg-disposable-tests: need postgres binaries or nix" >&2; exit 2; }
  exec nix shell "$PG_NIX_REF" -c "$0" "$@"
fi

# libpq reads every PG* variable (PGHOSTADDR, PGSERVICE, PGSERVICEFILE, PGPASSFILE,
# PGSSL*, ...), and any of them could redirect a connection. Clear them all so
# only the explicit host, port and URLs below are used.
while IFS= read -r name; do unset "$name"; done < <(compgen -e | grep '^PG' || true)

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The server socket path must stay under the unix-socket length limit (~107
# bytes), so fall back to /tmp when TMPDIR is long.
tmp_base="${TMPDIR:-/tmp}"
[ "${#tmp_base}" -le 60 ] || tmp_base=/tmp
# Stop and remove clusters orphaned by an earlier hard-killed run, before this
# run's own dir exists. A failing sweep must not block the run.
"$repo_root/scripts/pg-disposable-sweep.sh" || echo "pg-disposable-tests: warning: stale-cluster sweep failed" >&2
work="$(mktemp -d "$tmp_base/sprintctl-pg.XXXXXX")"
# Owner file for the sweep: this script's pid and its start time (field 22 of
# /proc/<pid>/stat, counted after the last ")"), or "unknown" without /proc.
owner_start=unknown
if [ -r "/proc/$$/stat" ]; then
  owner_stat="$(cat "/proc/$$/stat")"
  read -ra owner_fields <<<"${owner_stat##*) }"
  owner_start="${owner_fields[19]:-unknown}"
fi
echo "$$ $owner_start" >"$work/owner.tmp"
mv "$work/owner.tmp" "$work/owner"
data="$work/data"
sock="$work/sock"
mkdir -p "$sock"
started=0

cleanup() {
  local rc=$?
  # A second signal must not abort cleanup halfway and leave the dir behind.
  trap '' INT TERM
  set +e
  if [ "$rc" != 0 ] && [ -s "$work/server.log" ]; then
    echo "pg-disposable-tests: server log (exit $rc):" >&2
    tail -n 40 "$work/server.log" >&2
  fi
  if [ "$started" = 1 ]; then
    pg_ctl -D "$data" -m immediate -w stop >/dev/null 2>&1
  fi
  # Belt and braces: a postmaster that outlived pg_ctl still owns this pid file.
  if [ -f "$data/postmaster.pid" ]; then
    kill -9 "$(head -1 "$data/postmaster.pid")" 2>/dev/null
  fi
  rm -rf "$work"
  exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT TERM

port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
test_pw="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
probe_pw="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"

initdb -D "$data" -U pgadmin_disposable --auth-local=trust --auth-host=scram-sha-256 -E UTF8 >/dev/null
pg_ctl -D "$data" -w -l "$work/server.log" \
  -o "-c listen_addresses=127.0.0.1 -c port=$port -c unix_socket_directories=$sock -c fsync=off" start >/dev/null
started=1

admin() { psql -X -v ON_ERROR_STOP=1 -h "$sock" -p "$port" -U pgadmin_disposable -d postgres "$@"; }

server_version_num="$(admin -tA -c 'SHOW server_version_num')"
server_version="$(admin -tA -c 'SHOW server_version')"
case "$server_version_num" in
  16[0-9][0-9][0-9][0-9]) ;;
  *) echo "pg-disposable-tests: FAIL: server_version_num $server_version_num is not PostgreSQL major 16" >&2; exit 1 ;;
esac
echo "pg-disposable-tests: PostgreSQL server version $server_version (server_version_num $server_version_num)"

admin >/dev/null <<SQL
CREATE ROLE sprintctl_test_local LOGIN PASSWORD '$test_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE sprintctl_production_probe LOGIN PASSWORD '$probe_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE sprintctl_test_local OWNER sprintctl_test_local;
COMMENT ON DATABASE sprintctl_test_local IS 'sprintctl:disposable-integration-test';
SQL

report="$work/pg-cleanup-report.jsonl"
export SPRINTCTL_TEST_PG_URL="postgresql://sprintctl_test_local:$test_pw@127.0.0.1:$port/sprintctl_test_local"
export SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL="postgresql://sprintctl_production_probe:$probe_pw@127.0.0.1:$port/sprintctl_test_local"
export SPRINTCTL_TEST_PG_CLEANUP_REPORT="$report"

cd "$repo_root"
# Hermetic: ambient backend/profile selection (e.g. a served-mode shell) must
# not leak into the suite. Only the disposable SPRINTCTL_TEST_PG_* values stay.
while IFS= read -r name; do
  case "$name" in SPRINTCTL_TEST_PG_*) ;; *) unset "$name" ;; esac
done < <(compgen -e | grep '^SPRINTCTL_' || true)
rc=0
uv run --extra dev --extra remote pytest -q -m pg -rs tests/pg/ tests/test_work_application_pg.py "$@" \
  >"$work/pytest.out" 2>&1 || rc=$?
cat "$work/pytest.out"
[ "$rc" = 0 ] || exit "$rc"

# Shared with CI: report lines parse, every started fixture finished clean with
# zero remaining rows, no skipped tests, at least one passed test.
python3 "$repo_root/scripts/pg-cleanup-report-check.py" "$report" "$work/pytest.out"
