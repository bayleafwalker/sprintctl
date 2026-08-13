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
uv run --extra remote pytest -m pg tests/pg/ -v
```

The password is temporary test infrastructure state. Never commit it or reuse
a production credential.

## Cleanup evidence and interruption limits

Every repository scope minted by the module is prefixed `itest-` and registered
with one module finalizer. The finalizer revalidates the server identity,
deletes all registered scopes, queries every repository-scoped table for
residue, and writes `sprintctl-pg-cleanup/v1` evidence when
`SPRINTCTL_TEST_PG_CLEANUP_REPORT` is set. A successful report records zero
remaining rows for every table and contains no URL or password.

Normal test failures and interrupts run the finalizer. A hard process or host
termination cannot guarantee client-side cleanup, which is why CI runs the
suite in an ephemeral PostgreSQL service. The service is destroyed with the
job; the cleanup report is still uploaded as evidence that the ordinary
finalizer completed.

CI also provisions a production-like probe role on the ephemeral database.
That role receives narrowly scoped insert permission and the integration test
proves the server trigger rejects its attempted `itest-*` write. No production
database or production data cleanup is part of this workflow.
