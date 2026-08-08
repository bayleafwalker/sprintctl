---
doc_id: sprintctl-postgres-schema-compatibility
status: draft
---

# PostgreSQL schema compatibility and migration roles

The shared PostgreSQL work schema is deployment-owned. Normal sprintctl and
Vuoro work-service startup must only read its compatibility ledger; it must not
create, alter, or repair schema objects.

## Published compatibility contract

`sprintctl.pg.compatibility_handshake()` and `sprintctl remote-schema check
--json` publish `sprintctl-work-compatibility/v1`. The handshake identifies the
work API as `sprintctl-work/v1`, reports the actual remote schema version, and
reports the minimum and maximum versions this runtime supports.

Schema versions 5 and 6 are supported only when the complete maintenance
storage capability is present. During the pre-migration window, version 5 may
carry that additive capability while retaining its primary ledger version.
The read-only probe hashes a schema-qualified PostgreSQL catalog description
covering every required column, type, nullability/default, primary/unique/
foreign/check constraint, and each immutable trigger's exact table, function,
events, and enabled state. It also requires the exact `maintenance-storage`
capability marker. Same-named objects in another schema do not participate.
Missing or partial storage, a missing trigger or marker, a missing/ambiguous
ledger, versions below 5, and versions above 6 fail closed before a runtime
command is served. The check executes only `SELECT` probes and never attempts
repair. Package version
strings are not protocol compatibility evidence.

A pre-cutover client that has not upgraded reports this fail-closed state as
`schema-version-mismatch` on `sprintctl doctor` (remote schema newer than the
client's compiled expectation) and is denied writes on every remote entry
point; see [#1220 evidence](../plans/1164-gate-evidence-ledger.md) for the
recorded stale-install verification. The upgrade path is to reinstall the
`sprintctl` uv tool from the current `sprintctl` repository with the
`remote` and `served` extras: `uv tool install --force --reinstall
--from /path/to/sprintctl sprintctl[remote,served]` (or the published
package once released). An install missing those extras cannot use
`SPRINTCTL_BACKEND=served` or `remote` at all and fails with
`invalid SPRINTCTL_BACKEND=...` before any schema check runs.

The handshake also publishes
`sprintctl-repository-ingest-cursor/v1` with `scope=repository` and
`contiguous=true`. Numeric `ingest_offset` values are meaningful only together
with their repository identity; different repositories may validly expose the
same offset. The internal identity-backed `ingest_id` remains globally unique
inside the shared schema and is not a public paging cursor.

## Deployment migration job

For the bounded schema-5 coexistence window, first pre-provision the complete
additive maintenance store with the migration-role credential:

```bash
sprintctl remote-schema stage-maintenance-bridge --json
```

The command requires exact ledger version 5, takes the same global transaction
lock as canonical migrations, and leaves the ledger at 5. It writes the exact
capability marker only in the same transaction as the relations and immutable
triggers. It refuses partial state rather than repairing it. Existing version-5
runtime code does not reference these additive objects and can continue using
an already-open connection; normal startup of the new runtime accepts the
bridge only after the full structural fingerprint passes. Runtime credentials
cannot invoke this DDL path.

The later canonical migration remains:

Run migrations with the migration-role credential from the appservice-owned
deployment job:

```bash
sprintctl remote-schema migrate --json
```

`SPRINTCTL_URL` supplies that job's PostgreSQL URL. The migrator takes the
stable global PostgreSQL transaction advisory lock before reading the ledger,
applies each migration transactionally, and advances the single ledger row
only after its DDL succeeds. Re-running a completed migration is a no-op. A
schema newer than the migration package is never downgraded.

Migration version 2 normalizes every known legacy version-1 deployment before
advancing the ledger. This is necessary because the old client bootstrap
accumulated idempotent DDL while leaving the ledger at version 1.

Migration version 3 retains the old global identity as `ingest_id`, backfills
`ingest_offset` with `row_number()` per repository in internal-ingest order,
translates authority-decision offsets, and installs repository-bound uniqueness
and foreign keys. It seeds one locked `ingest_repo_cursor` row per repository.
The advisory lock, table lock, DDL, data translation, cursor seed, and ledger
advance share one transaction; a fault cannot publish version 3 early. Runtime
append transactions lock the repository cursor before any producer-stream row,
and retries or rollbacks do not consume public offsets.

Migration version 4 widens the `ref` table's `ref_type` CHECK constraint to
add `command` (validation-command refs, mirroring SQLite migration 15) and
does not touch `ingest_record` or the repository cursor.

Projection cache schema version 2 records its owning repository. A version-1
cache is never interpreted as current: synchronization captures the repository
high-water, builds contiguous pages from offset zero into a sibling SQLite
file, and atomically replaces the live cache only after reaching that exact
high-water. A retained suffix offered to an empty rebuild remains a gap error.

## Role contract

The concrete role and schema names are deployment inputs owned by appservice.
Their privileges must implement these two roles:

| Role | Required privileges | Forbidden in normal operation |
|---|---|---|
| migration | connect, migration-schema usage/create, object ownership, advisory lock, DDL and ledger update | application traffic |
| runtime | connect, schema usage, table DML, sequence usage/select, and execution of domain functions | schema create, object ownership, and all DDL |

For a migration role named `sprintctl_migration`, a runtime role named
`sprintctl_runtime`, and a schema named `public`, the migration job (or its
administrator) applies grants equivalent to:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM sprintctl_runtime;
GRANT USAGE ON SCHEMA public TO sprintctl_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO sprintctl_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sprintctl_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO sprintctl_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sprintctl_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sprintctl_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sprintctl_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO sprintctl_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sprintctl_migration IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO sprintctl_runtime;
```

The runtime credential must be tested server-side: its read-only compatibility
probe succeeds, while representative `CREATE TABLE` and other DDL are denied.
Do not grant DDL merely to support an old workstation client. After runtime DDL
is removed, old direct clients are expected to fail explicitly when their
startup bootstrap encounters the restricted role.

## Served runtime connection recovery

The long-lived served work application classifies PostgreSQL administrative
shutdown only by SQLSTATE `57P01`. It reconnects and retries the request once
only for read operations, or for the small set of domain operations that
require a non-empty durable idempotency key. A direct mutation whose outcome is
unknown is never replayed; it returns the stable
`postgres-runtime-unavailable` response instead. If replacement connection
creation fails, eligible operations receive that same unavailable response.
The next eligible request may establish a new connection after PostgreSQL
readiness returns.

## Rollout compatibility mode

Normal remote startup behaves as if
`SPRINTCTL_REMOTE_SCHEMA_MODE=read-only`. During the bounded vuoro-dev rollback
window, an operator using the migration-role credential may explicitly set:

```bash
SPRINTCTL_REMOTE_SCHEMA_MODE=operator-migrate
```

That mode runs the same deployment migration package before the read-only
handshake. Any other value fails before querying the database. Remove the mode
from runtime environments after rollout evidence passes; it is not authority
for a runtime role to acquire DDL.

Local SQLite remains self-migrating through `sprintctl.db.init_db()` for local
and recovery authority. This PostgreSQL role split does not change SQLite
transition semantics or migration behavior.
