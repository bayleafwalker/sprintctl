# Local SQLite schema migration and rollback

This guide governs the local SQLite authority. Shared PostgreSQL deployment
migrations follow [served authority alignment](../plans/vuoro-served-authority-alignment.md)
and their deployment owner. Use an explicit local database path for backup and
restore; a served project continues through its configured authority.

## Existing runner and version source

`db.init_db()` registers numbered migration functions and calls
`_run_migration` in order. Normal commands that open a local store use that
path. Help and file-only commands do not need a database. There is no separate
migration runner to install. Call it before application writes, with no active
caller transaction; the runner owns commit and rollback.

For each migration, `_run_migration`:

1. Opens `BEGIN IMMEDIATE` before checking the stored version.
2. Skips a version already committed.
3. Runs its callback and updates `schema_version` in the same transaction.
4. Commits success, or rolls back on an exception.
5. Restores foreign-key checking when its `foreign_keys_off` option was used.

The source's `db.CURRENT_SCHEMA_VERSION` and `init_db()` registration are the
current target and complete migration chain. Query them directly:

```sh
python -c 'from sprintctl import db; print(db.CURRENT_SCHEMA_VERSION)'
sqlite3 -readonly /absolute/path/to/local/sprintctl.db 'SELECT version FROM schema_version'
```

This is version gating: successfully committed versions do not rerun. It does
not prove that an older binary understands a newer schema, and it does not
repair arbitrary manually modified schemas.

## Selected historical milestones

| Version | Change |
|---|---|
|1–4|Initial work tables, sprint kinds, dates and item metadata|
|5–6|Historical exclusive claims and claim credentials|
|7–10|Refs, dependencies, takeup index and portable aggregate UUIDs|
|11–13|Item priority and historical claim status/epoch|
|14–15|File/glob/manifest and command refs|
|16–17|Maintenance capability and observable-resource ledgers|
|18–20|Advisory reservations, credential-free claim history, removal of the live claim relation|
|21–22|Repository recovery records and advisory reservation roles|
|23|Terminal work Decisions and legacy evidence handling|
|24|Immutable work Releases and reservation Release bindings|
|25|One-shot legacy done-item re-marking|

The claim fields are historical. Current work uses advisory reservations and
terminal Decisions. This table is an overview; inspect the source registration
for subsequent versions.

## Add a migration within the existing pattern

Choose the next unused explicit version after reconciling concurrent branches.
Add `_migration_N`, register that exact version in `init_db()`, and update
`CURRENT_SCHEMA_VERSION` in the same change. Keep earlier registrations bound
to their original numbers.

Prefer additive changes and compatible readers before enabling new writers.
Backfill existing rows in the same transaction and check their invariants.
SQLite permits an added column without an explicit default; required columns
on populated tables need a suitable constant default or a table rebuild.
A computed value generally requires a separate backfill statement.

Callbacks must use the existing transaction. They must not commit, open a nested
transaction, or call `executescript`, which can change transaction boundaries.
A failed callback must leave both schema/data and stored version unchanged.
After fixing the cause, rerun through the runner; do not manually stamp a newer
schema version. `IF NOT EXISTS` alone cannot validate an existing table's shape
or make an arbitrary partial migration safe.

When rebuilding referenced tables, use the runner's `foreign_keys_off=True`
option, as existing rebuild migrations do. Verify referential integrity after
the migration. Database atomicity does not roll back external files, network
operations or protected effects; keep those outside migration callbacks.

Removing a table or changing constraints requires an explicit compatibility,
history-preservation and restore plan. Existing migrations have removed live
claims after preserving their credential-free history. The schema is therefore
not universally additive, and opening it with an older binary is not a general
rollback mechanism. Release/version decisions follow the governing rollout
plan; this guide does not invent a major-version-only removal policy.

## Failure rollback and committed-upgrade rollback

SQLite DDL such as `CREATE TABLE` and `DROP TABLE` participates in an explicit
transaction. A callback exception inside `_run_migration` rolls those statements
back together with data and the version update. A completed migration has
already committed; rolling it back requires its declared recovery procedure.
There are no automatic down-migrations.

Use SQLite's backup API or `.backup` for a consistent snapshot, including
committed WAL data. Copying only a live `.db` file can omit its WAL. This example
requires an existing source and a fresh, private backup path:

```python
from contextlib import closing
from pathlib import Path
import os
import sqlite3

source = Path('/absolute/path/to/local/sprintctl.db').resolve(strict=True)
backup = Path('/absolute/path/to/backups/before-upgrade.sqlite')
os.close(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
    with closing(sqlite3.connect(backup)) as snapshot:
        original.backup(snapshot)
        assert snapshot.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not snapshot.execute('PRAGMA foreign_key_check').fetchall()
```

Use a backup only after validation succeeds. Record the database identity,
source binary revision, stored schema version and expected work/evidence counts.
Stop local writers for the upgrade/restore boundary and account for writes made
after the snapshot; they are absent from a restored copy.

For a rollback drill, restore into a separate candidate path. Verify schema,
work IDs, data counts, `integrity_check` and `foreign_key_check`, then run the
intended previous binary against that candidate. A passing SQLite restore test
does not establish compatibility with an untested older binary. Preserve the
migrated source while deciding whether to switch to the validated candidate.
Do not merge two divergent authoritative databases.

Per-repository flags can disable a compatible new read path before a cutover;
existing projection reads are one example. A flag does not reverse a committed
schema change, restore removed data, or authorize a shared-authority migration.
Each rollout phase must name its own fallback and evidence, as required by the
[canonical ADR](../plans/adr-outbox-sync-model.md#migration-and-rollback).

## Applied pattern: reversible local cache reads

The existing `projection-reads` switch illustrates a compatible read rollout.
It selects cached event history for supported **local** reads such as `item
show`; it does not move work authority or change the database schema. Other
read surfaces may only disclose cache health. A served invocation continues
through its served owner and never falls back to local SQLite because this
switch is disabled or its cache is unavailable.

The per-repository `.sprintctl/projection-reads.json` defaults to disabled.
Writes replace that versioned configuration atomically with mode `0600`.
The configuration parser rejects malformed shapes, unknown fields and newer
versions. Use `projection-reads status --json` to diagnose invalid configuration;
ordinary reads retain the backend when configuration cannot be loaded.

For a selected local repository, inspect the effective setting and read source:

```bash
sprintctl projection-reads status --json
sprintctl projection-reads enable
sprintctl item show --id <item-id> --json
```

The `projection` field discloses the selected source, watermark and fallback
reason. Missing, never-synchronized, stale and incompatible caches select the
backend explicitly; enabling a flag does not make a cache healthy. A compatible
healthy cache can supply supported event history. Existing authority records
remain in the backend throughout.

Rollback changes read selection and retains the cache, outbox and authoritative
records:

```bash
unset SPRINTCTL_PROJECTION_READS
sprintctl projection-reads disable
sprintctl projection-reads status --json
sprintctl item show --id <item-id> --json
```

**Environment precedence matters:** `SPRINTCTL_PROJECTION_READS` overrides the
persisted setting for that process, in either direction. Persisting `disabled`
while an enabling override remains set does not disable effective cache reads.
Remove or override that environment setting in every relevant process and
verify the effective status and `projection.source`. The environment override
can affect multiple repository invocations; changing one repository's persisted
file does not change another repository's file.

`tests/test_read_rollout_pattern.py` exercises the actual local CLI with the
existing cache envelope. It verifies effective rollback with and without an
enabling override, unchanged cache bytes and authoritative records, and an
unrelated repository's retained configuration. `tests/test_projection_reads.py`
covers configuration refusals and explicit cache-health fallbacks. These are
bounded local examples, not full state equivalence, every historical binary's
compatibility, or a service migration rehearsal.

This pattern completes Sprintctl#914's documented/applied rollout-pattern scope
as reconciled in native scope note5491. Actual provider cutover, mixed-version
qualification and recovery of records created after cutover remain open under
Sprintctl#1164/#2738. The archived shadow pilot and retired capability runtime
are historical evidence, not mechanisms to re-enable. A read toggle neither
reverses schema changes nor replaces those qualification gates.

## Verification and limits

`tests/test_local_migration_safety.py` injects a real failure after table creation,
backfill and drop, reopens the file to check unchanged rows/schema/version,
retries successfully, and verifies an already committed version does not rerun.
Both foreign-key modes are exercised. A separate test restores pre-migration
rows, schema and version from a SQLite backup after a successful destructive
synthetic migration. All files and version increments are disposable fixtures.

These tests qualify the existing local runner and recovery pattern. They do not
establish every historical migration's business correctness, old-binary
compatibility, PostgreSQL deployment readiness or estate-wide recovery.

Concurrent local startup is serialized at the migration's write lock. A caller
can still hit its configured lock timeout; the runner does not promise unlimited
concurrency or automatic retries. Existing concurrency verification remains
separate from these failure and restore examples.
