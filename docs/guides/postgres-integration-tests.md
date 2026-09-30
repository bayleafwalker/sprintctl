# Disposable PostgreSQL integration tests

`tests/pg/` (split by domain from the former `tests/test_pg_integration.py` in
P4.2 -- `tests/pg/_shared.py` holds the shared fixtures, skip machinery, and
helpers every file in the package imports) is destructive by design: it
creates and deletes repository-scoped sprint data. It must never run against
the shared sprintctl authority or any other persistent database.

## Safety contract

The suite connects only when `SPRINTCTL_TEST_PG_URL` identifies a server-side
identity with all of these properties:

- the database and login role names are `sprintctl_test` or start with
  `sprintctl_test_`;
- the login role owns the database;
- the database comment is exactly
  `sprintctl:disposable-integration-test`;
- the role is not a superuser and has no `CREATEDB`, `CREATEROLE`,
  `REPLICATION`, or `BYPASSRLS` attribute.

The preflight reads these facts from PostgreSQL before `init_db` or any test
data write. URL text alone is not trusted. The schema also installs triggers
on every `repo_id` table. Those triggers reject `itest-*` inserts and moves
unless the current server-side role and database satisfy the same contract.
This means an accidentally supplied production URL fails closed even if a
caller bypasses the Python preflight.

Do not weaken the contract to make an existing shared database convenient for
tests. Create a disposable PostgreSQL instance instead.

## Local disposable setup

Use a throwaway PostgreSQL container or VM. As its temporary administrator,
create a dedicated login and database:

```sql
CREATE ROLE sprintctl_test_local LOGIN PASSWORD '<temporary-password>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE sprintctl_test_local OWNER sprintctl_test_local;
COMMENT ON DATABASE sprintctl_test_local IS
  'sprintctl:disposable-integration-test';
```

Then run only against that disposable instance:

```bash
export SPRINTCTL_TEST_PG_URL='postgresql://sprintctl_test_local:<temporary-password>@127.0.0.1/sprintctl_test_local'
export SPRINTCTL_TEST_PG_CLEANUP_REPORT="$PWD/pg-cleanup-report.json"
rm -f "$SPRINTCTL_TEST_PG_CLEANUP_REPORT"   # the report appends; start from an empty file
uv run --extra remote pytest -m pg tests/pg/ -v
```

The password is temporary test infrastructure state. Never commit it or reuse
a production credential.

## One-command disposable run

`scripts/pg-disposable-tests.sh` builds the disposable instance above for you
and is the suite dispatch verification runs (manifest check
`sprintctl.pg.disposable`, also listed in `verification.commands`). Run it as:

```bash
timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh
```

On a timeout `timeout` sends TERM, which runs the script's cleanup trap; `-k 30s`
follows with KILL only if that cleanup hangs. The script:

- runs under `nix shell github:NixOS/nixpkgs/b4fd65b198c599cbe814fcb9f42d25d021595ec9#postgresql_16`
  (PostgreSQL 16.15, the rev `gitops-nixos/flake.lock` pins; set once in
  `PG_NIX_REF` at the top of the script) when `initdb` is not on `PATH`. After
  the server starts it reads `server_version_num`, fails unless the server is
  major 16 (whether the binaries came from `PATH` or nix), and prints the
  server version;
- runs `initdb` in a `mktemp` directory and starts PostgreSQL on a unix socket
  in that directory plus `127.0.0.1` on a free port; it never connects to any
  other database. A `TMPDIR` longer than 60 characters falls back to `/tmp`,
  because the socket path must stay under the unix-socket length limit;
- creates `sprintctl_test_local` (owning the `sprintctl_test_local` database,
  comment `sprintctl:disposable-integration-test`) and the
  `sprintctl_production_probe` role behind
  `SPRINTCTL_TEST_PG_PRODUCTION_GUARD_URL`, both `NOSUPERUSER NOCREATEDB
  NOCREATEROLE NOREPLICATION NOBYPASSRLS`, with per-run generated passwords
  that are never written to the repository;
- clears all `PG*` variables (libpq reads `PGHOSTADDR`, `PGSERVICE`,
  `PGPASSFILE`, `PGSSL*` and more, any of which could redirect a connection), so
  only the explicit host, port and URLs are used;
- clears every `SPRINTCTL_*` variable except `SPRINTCTL_TEST_PG_*` (for example
  a served-mode `SPRINTCTL_BACKEND`) so the suite is hermetic;
- runs `uv run --extra dev --extra remote pytest -m pg tests/pg/
  tests/test_work_application_pg.py`, then runs
  `scripts/pg-cleanup-report-check.py` on the cleanup report and the captured
  pytest output (see below);
- always stops the server and removes the directory on exit (trap).

Extra arguments are passed to pytest. The cold run takes about 30 seconds.

### Stale clusters

Right after `mktemp` the script writes `<dir>/owner`, one line holding its pid
and that pid's start time (field 22 of `/proc/<pid>/stat`). PostgreSQL
daemonizes, so a SIGKILL of the script or a host loss leaves the postmaster
running with nothing to stop it. `scripts/pg-disposable-sweep.sh` handles
that: the main script runs it before it creates its own directory, and it also
runs standalone. It scans the `sprintctl-pg.*` directories owned by the current
user under `/tmp` and under the tmp base. For a directory whose owner pid is dead,
or alive with a different start time (pid reuse), it stops the cluster with
`pg_ctl -D <dir>/data -m immediate stop`, falls back to SIGQUIT and then
`kill -9` of the `postmaster.pid` pid only when that process's command line
names `<dir>/data`, removes the directory, and prints one line per swept
directory. It never touches a directory whose owner is alive. A directory with no
owner file (older layout) is reported and left alone.

## Cleanup evidence and interruption limits

Every repository scope minted by the tests is prefixed `itest-` and registered
with a fixture finalizer. Each finalizer goes through one helper,
`FixtureCleanup` in `sprintctl/pg_testing.py`, which revalidates the server
identity, deletes all registered scopes, queries every repository-scoped table
for residue, and records the outcome. The fixtures covered are `pg_test_scope`,
`store_factory`, `maintenance_resource_transactional_pg_factory` and the
foreign-scope cleanup inside the non-disclosure history test.

When `SPRINTCTL_TEST_PG_CLEANUP_REPORT` is set, the report is JSON Lines,
schema `sprintctl-pg-cleanup/v2`, appended to by every fixture: a `started`
record at fixture setup and a `finished` record at teardown, keyed by the
module or test nodeid plus the fixture name. A `finished` record carries the
cleanup result (`cleanup_completed`, `deleted_rows`, `remaining_rows`), or
`cleanup_completed: false` plus `error_type` when cleanup raised. Records
contain no URL or password.

`scripts/pg-cleanup-report-check.py REPORT PYTEST_OUTPUT` is the checker shared
by the script and CI. It fails unless every line parses, at least one fixture
reported, every `started` fixture has exactly one `finished` record with
`cleanup_completed` true and every `remaining_rows` value zero, and the pytest
output shows at least one passed test and no skipped test. It prints a count
line per fixture name. Because records append, remove the report file before a
manual run; a leftover report from an earlier run fails the checker with
duplicate `started` records.

Normal test failures and interrupts run the finalizers. A hard process or host
termination cannot guarantee client-side cleanup, which is why CI runs the
suite in an ephemeral PostgreSQL service, and why the script's owner file and
sweep exist for the local disposable cluster. The CI service is destroyed with
the job; the CI job runs the same test selection and the same checker, and the
cleanup report is uploaded as evidence.

CI also provisions a production-like probe role on the ephemeral database.
That role receives narrowly scoped insert permission and the integration test
proves the server trigger rejects its attempted `itest-*` write. No production
database or production data cleanup is part of this workflow.
