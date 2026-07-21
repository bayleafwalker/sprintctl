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

Schema version 3 is the only supported version in this rollout. A missing
ledger, versions 1 or 2, and any version greater than 3 fail closed before a runtime
command is served. The check executes only `SELECT to_regclass(...)` and a
`SELECT` over `schema_version`; it never attempts repair. Package version
strings are not protocol compatibility evidence.

The handshake also publishes
`sprintctl-repository-ingest-cursor/v1` with `scope=repository` and
`contiguous=true`. Numeric `ingest_offset` values are meaningful only together
with their repository identity; different repositories may validly expose the
same offset. The internal identity-backed `ingest_id` remains globally unique
inside the shared schema and is not a public paging cursor.

## Deployment migration job

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
