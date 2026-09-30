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

The current source tree admits exactly remote schema 19 (schema 18 for
sprintctl 0.9.0 through 0.10.x): `MINIMUM_SCHEMA_VERSION` and
`MAXIMUM_SCHEMA_VERSION` in `sprintctl/pg_migrations.py` are both 19, and
`remote-schema-version` in `pyproject.toml` records the same value. Any other
ledger version fails closed before a runtime command is served.

Historical (schema-5 coexistence window, superseded by the exact-version
policy above): schema versions 5 and 6 were supported only when the complete
maintenance storage capability was present. During the pre-migration window,
version 5 could carry that additive capability while retaining its primary
ledger version.
The read-only probe hashes a schema-qualified PostgreSQL catalog description
covering every required column, type, nullability/default, primary/unique/
foreign/check constraint, and each immutable trigger's exact table, function,
events, and enabled state. It also requires the exact `maintenance-storage`
capability marker. Same-named objects in another schema do not participate.
Missing or partial storage, a missing trigger or marker, a missing/ambiguous
ledger, versions below 5, and versions above 6 failed closed before a
runtime command was served in that window. The check executes only `SELECT` probes and never attempts
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

Schema 17 (sprintctl 0.8.0) is a coordinated cutover. A 0.8.0 runtime
admits only schema 17, and a 0.7.x runtime refuses it. Run the deployment
migration before, or together with, the 0.8.0 rollout. Schema 17 adds the
`run`, `evidence_item`, `session_note` and `work_idempotency_ledger` tables
behind the `work.run.*`, `work.evidence.*-v1` and `work.session-note.*`
operations. If a relation already uses one of those names without schema
17's exact columns and constraints, the migration refuses instead of keeping
it.

Schema 18 (sprintctl 0.9.0) is again a coordinated cutover: a 0.9.0
runtime admits only schema 18, and a 0.8.x runtime refuses it. Run the
deployment migration together with the 0.9.0 rollout. Schema 18 is
additive: it adds the `work_lease` and `work_outcome_report` tables behind
the `work.lease.*-v1` operations, and refuses, as schema 17 does, a relation
already holding one of those names with another shape.

sprintctl 0.10.0 changes only the adapter catalog; the remote schema stays
at 18, so a 0.9.x authority needs no migration for a 0.10.0 rollout.

Schema 19 (agentops#2541, M2-1) is a coordinated cutover again: a runtime
built with it admits only schema 19, and a 0.10.x runtime refuses it. Run the
deployment migration together with the rollout of the first release that
contains it. Schema 19 is additive: it adds the `work_effect_intent` table
behind the `work.effect.*-v1` operations, and the
`sprintctl_work_effect_intent_guard` trigger that keeps an intent's content
and an accepted intent's acceptance record immutable in storage. Like
schemas 17 and 18, the migration refuses a relation already holding one of
its names with another shape.

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

The long-lived served work application holds one shared PostgreSQL connection,
created by `sprintctl.pg.get_connection` together with a connection factory.
It is built to survive a database restart, failover, or idle-session kill
without replaying a command whose outcome it cannot know.

**Connection settings.** `get_connection` adds `connect_timeout=5` and TCP
keepalives (`keepalives=1`, `keepalives_idle=30`, `keepalives_interval=10`,
`keepalives_count=3`) and `tcp_user_timeout=10000` (bounding a command sent to
a peer that vanished mid-request, which keepalives do not cover) to both the
first connection and every replacement, each only when the DSN does not
already set it; a value in the DSN always wins.

**Connection loss.** An error counts as connection loss when its SQLSTATE is in
class `57P0x` (administrative or crash shutdown, cannot connect now, database
dropped, idle-session timeout) or class `08` (connection exception), or when
psycopg raised an `OperationalError` and the connection is afterwards closed or
broken (a socket that died with no SQLSTATE, such as "consuming input failed").
Every other error keeps its ordinary mapping.

**Before every operation.** If the shared connection is missing, closed or
broken, it is replaced through the factory before the operation is dispatched.
Nothing has been sent yet, so this is safe for every operation, mutations
included. If the replacement cannot be made, the operation returns
`postgres-runtime-unavailable` (HTTP 503) and nothing was sent.

**During an operation.** On connection loss the dead connection is closed and
dropped from the shared store, so the next request reconnects first. The
operation is then replayed once on a fresh connection only if it is
retry-eligible:

- pure reads: every `work.read.*` operation, plus `work.identity.current`,
  `work.maintain.check`, `work.maintenance.resource.get`,
  `work.maintenance.resource.changes`, `work.public.list-v1`,
  `work.public.item-v1`, `work.validate.item-status-mutation`,
  `work.run.resolve-v1`, `work.evidence.tail-v1`, `work.lease.read-v1`,
  `work.effect.get-v1` and `work.effect.list-proposed-v1`;
- commands keyed by a durable unique constraint, when the request carries a
  required, non-empty idempotency key: `work.lifecycle.arbitrate`,
  `work.decision.record`, `work.evidence.ingest`, `work.batch.apply`,
  `work.maintenance.prepare`, `work.maintenance.transition`,
  `work.maintenance.recovery-record` and `work.maintenance.resource.prepare`.

Any other operation returns `postgres-runtime-unavailable` (HTTP 503) with a
message that its outcome is unknown: the caller re-reads the current state, or
resends the same request with the same idempotency key. Reservation operations
(`work.reservation.*`) are not eligible, because `reserve` always inserts a new
row. The `work_idempotency_ledger` operations (`work.run.register-v1`,
`work.evidence.append-v1`, `work.session-note.write-v1`,
`work.lease.acquire-v1`, `work.lease.report-outcome-v1` and its alias
`work.lease.complete-v1`) are never replayed internally either: the caller's
resend with the same key is answered from the ledger, so it has one effect.

**Readiness.** `WorkApplication.served_runtime_ready()` checks the database
each time it is called: it replaces a dead connection, then runs `SELECT 1`
inside its own transaction with `SET LOCAL statement_timeout = 2000` and rolls
that transaction back. A connection that only turns out to be lost during the
probe is replaced and probed once more. It returns false on any failure and
never raises, so readiness drops while PostgreSQL is unreachable and returns
by itself once it is back, without waiting for a request. A connection that
already has a transaction open belongs to a running operation and is not
probed. The Vuoro service shell binds this method as its HTTP readiness check
(`readiness_check=work_application.served_runtime_ready` in its composition).

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
