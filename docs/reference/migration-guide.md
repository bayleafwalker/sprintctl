# Schema migration guide

sprintctl uses a sequential, numbered migration system backed by a
`schema_version` table in the SQLite database. Migrations run automatically on
`init_db()` — every CLI invocation calls this before any other DB operation,
so no manual migration step is needed for normal upgrades.

---

## How migrations work

`db.py` maintains a `_MIGRATIONS` list and an `init_db()` function that applies
each migration in order, guarded by the current `schema_version`:

```python
def init_db(conn):
    _run_migration(conn, 1, _migration_1)
    _run_migration(conn, 2, _migration_2)
    # ...
    _run_migration(conn, 8, _migration_8)
```

Each `_run_migration` call:

1. Opens a `BEGIN IMMEDIATE` transaction (prevents concurrent migration races)
2. Reads the current schema version
3. Skips if the DB is already at or beyond the target version
4. Applies the migration function
5. Updates `schema_version` and commits — or rolls back on error

**The migration is idempotent for the caller**: running `init_db` on an
already-migrated database is a no-op for every version already applied.

Current schema version: **8** (ref and dep tables, claim token fields, sprint kind, git context on events).

---

## Migration history

| Version | What changed |
|---------|-------------|
| 1 | Initial schema: sprint, track, work_item, event tables |
| 2 | Added `updated_at` to work_item; added `assignee` column |
| 3 | Added `goal` to sprint; added `kind` column |
| 4 | Added `start_date`, `end_date` to sprint |
| 5 | Added `claim` table (exclusive ownership with TTL); rebuilt work_item constraints |
| 6 | Added `claim_token`, `runtime_session_id`, `instance_id`, `hostname`, `pid` to claim |
| 7 | Added `ref` table (typed external references on items) |
| 8 | Added `dep` table (item-to-item blocking dependencies) |

---

## Adding a new migration

1. Write the migration function in `db.py`:

   ```python
   def _migration_9(conn: sqlite3.Connection) -> None:
       conn.execute("""
           ALTER TABLE work_item ADD COLUMN priority INTEGER NOT NULL DEFAULT 0
       """)
   ```

2. Register it in `init_db()`:

   ```python
   def init_db(conn: sqlite3.Connection) -> None:
       # ... existing migrations ...
       _run_migration(conn, 9, _migration_9)
   ```

3. Add a test in `test_maintain.py` asserting the new schema state:

   ```python
   def test_schema_version_is_9(self, conn):
       version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
       assert version == 9
   ```

### Rules for safe migrations

- **Additive only**: add columns, add tables, add indexes. Never drop or rename
  in the same migration as existing data.
- **Default values required for new columns**: SQLite's `ALTER TABLE ADD COLUMN`
  requires a constant or `NULL` default. Computed defaults need a follow-up
  UPDATE statement inside the migration.
- **Foreign key constraints on new tables**: disable foreign keys before the
  migration if restructuring existing tables. `_run_migration` accepts
  `foreign_keys_off=True` for this case (used in migration 5).
- **No destructive changes in patch releases**: removing a column or changing a
  constraint requires a major version bump and explicit operator communication.
- **BEGIN IMMEDIATE**: `_run_migration` already wraps each migration in
  `BEGIN IMMEDIATE` — do not add your own transaction inside a migration function.

---

## Migration safety framework (design checklist)

Use this sequence when introducing a new schema version:

1. Preflight
   - take a timestamped DB backup (`cp ...sprintctl.db ...bak-YYYYMMDD`)
   - run `sprintctl maintain check --json` and save the output for comparison
   - run a full local test pass before changing migration code
2. Apply
   - implement an additive migration (`ALTER TABLE ... ADD COLUMN` or new table)
   - register it in `init_db()` as the next sequential version
   - run targeted migration tests plus full suite
3. Verify
   - reopen DB with current CLI (`init_db()` path) and confirm schema version
   - run smoke commands: `sprintctl sprint list --json`, `sprintctl usage --context --json`
   - check no data-loss regressions on `item`, `claim`, `ref`, `dep` paths
4. Rollback drill (before release)
   - restore the backup DB into a temp path
   - run previous sprintctl version against restored DB
   - verify baseline commands still succeed

This keeps migration safety explicit without introducing a separate migration
runner or distributed upgrade coordinator.

---

## Backward compatibility

### Adding a column with a default

Safe. Existing rows get the default; existing code that doesn't know about the
new column continues to work. Example: migration 6 added `claim_token` with a
`NULL` default — old claims become `legacy_ambiguous` and can be adopted.

### Adding a new table

Safe. Existing queries are unaffected. Always use `CREATE TABLE IF NOT EXISTS`
so partial migrations don't fail on re-run.

### Changing a CHECK constraint on an existing table

Requires rebuilding the table (copy-alter-drop). Use `foreign_keys_off=True`
and the copy-table pattern from migration 5 as a reference.

### Removing a column or table

Not supported in patch releases. Plan:

1. Deprecate: stop writing to the column; mark it in a comment
2. In the next major bump: migrate data away, then drop in a new migration

---

## Rollback strategy

SQLite does not support transactional DDL for all operations (e.g. `DROP TABLE`
in a `BEGIN` is committed immediately on SQLite). The practical rollback strategy
is:

**Before running a new version of sprintctl on a production database, back up
the database file:**

```bash
cp ~/.sprintctl/sprintctl.db ~/.sprintctl/sprintctl.db.bak-$(date +%Y%m%d)
```

Or with a per-project database:

```bash
cp .sprintctl/sprintctl.db .sprintctl/sprintctl.db.bak-$(date +%Y%m%d)
```

To roll back to the previous version:

1. Restore the backup: `cp sprintctl.db.bak sprintctl.db`
2. Pin to the previous version of sprintctl

Since migrations are purely additive (new columns/tables only), a rolled-back
binary will simply ignore the new schema elements — existing columns and tables
are unaffected.

---

## Checking the current schema version

```bash
sqlite3 ~/.sprintctl/sprintctl.db "SELECT version FROM schema_version"
```

Or via the Python API:

```python
from sprintctl import db
conn = db.get_connection()
version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
```

---

## Concurrent migration safety

`_run_migration` uses `BEGIN IMMEDIATE` which acquires a write lock before
reading the current version. Two processes starting simultaneously will serialize
at this point — one will apply the migration and commit, the other will read the
updated version and skip. This is safe for local-first use where at most two CLI
processes run against the same database concurrently.
