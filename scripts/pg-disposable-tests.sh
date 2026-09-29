#!/usr/bin/env bash
# Run the disposable-PostgreSQL integration suite (tests/pg/ and
# tests/test_work_application_pg.py) against a throwaway local server.
#
# Safety contract: docs/guides/postgres-integration-tests.md. This script
# creates its own cluster in a mktemp dir, listens on a unix socket in that dir
# plus 127.0.0.1 on a free port, and never connects to any other database. The
# role password is generated per run and never written to the repository.
# Postgres binaries come from `nix shell nixpkgs#postgresql_16` when absent.
set -euo pipefail

if ! command -v initdb >/dev/null 2>&1 || ! command -v pg_ctl >/dev/null 2>&1; then
  command -v nix >/dev/null 2>&1 || { echo "pg-disposable-tests: need postgres binaries or nix" >&2; exit 2; }
  exec nix shell nixpkgs#postgresql_16 -c "$0" "$@"
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/sprintctl-pg.XXXXXX")"
data="$work/data"
sock="$work/sock"
mkdir -p "$sock"
started=0

cleanup() {
  local rc=$?
  set +e
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

admin >/dev/null <<SQL
CREATE ROLE sprintctl_test_local LOGIN PASSWORD '$test_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE sprintctl_production_probe LOGIN PASSWORD '$probe_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE sprintctl_test_local OWNER sprintctl_test_local;
COMMENT ON DATABASE sprintctl_test_local IS 'sprintctl:disposable-integration-test';
SQL

report="$work/pg-cleanup-report.json"
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

if grep -Eq '[0-9]+ skipped' "$work/pytest.out"; then
  echo "pg-disposable-tests: FAIL: pg tests were skipped" >&2
  exit 1
fi
grep -Eq '[0-9]+ passed' "$work/pytest.out" || { echo "pg-disposable-tests: FAIL: no passing tests" >&2; exit 1; }

python3 - "$report" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except (OSError, ValueError) as exc:
    sys.exit(f"pg-disposable-tests: FAIL: cleanup report unreadable: {exc}")
rows = data.get("remaining_rows")
if data.get("cleanup_completed") is not True or not isinstance(rows, dict) or not rows:
    sys.exit("pg-disposable-tests: FAIL: cleanup report incomplete")
left = {k: v for k, v in rows.items() if v != 0}
if left:
    sys.exit(f"pg-disposable-tests: FAIL: cleanup residue {left}")
print(f"pg-disposable-tests: cleanup report shows zero residue across {len(rows)} tables")
PY
