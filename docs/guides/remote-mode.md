# Remote Mode

> **Current transitional interface:** this guide documents the installed
> direct-PostgreSQL client that exists today. The ratified target removes
> shared-schema migration and database credentials from workstation clients;
> see
> [`vuoro-served-authority-alignment.md`](../plans/vuoro-served-authority-alignment.md).
> Do not extend direct-client bootstrap as the design for new shared
> capabilities.

sprintctl runs in one of two backend modes:

| Mode | Storage | Multi-session | Setup cost |
|------|---------|---------------|------------|
| `local` | SQLite file at `.sprintctl/sprintctl.db` | Single writer (file lock) | None (default) |
| `remote` | PostgreSQL via `SPRINTCTL_URL` | Real-time shared across sessions | One-time migration |

Remote mode is best when two or more agents or terminals work the same sprint
concurrently. All claim state, heartbeats, and status transitions land in
PostgreSQL instantly with no file-lock contention.

---

## Activating remote mode

### 1. Run the one-time migration

`migrate-to-remote` copies all local sqlite rows to PostgreSQL, renames the
sqlite file so it cannot be written by mistake, and writes `.sprintctl/backend.json`
to lock the repo into remote mode permanently.

```sh
# Dry run first — reports what would be imported, changes nothing
sprintctl migrate-to-remote \
  --url "postgresql://user:pass@host/db" \
  --dry-run

# Live migration — prompts for confirmation before the freeze step
sprintctl migrate-to-remote \
  --url "postgresql://user:pass@host/db"

# Skip prompt (CI / scripted)
sprintctl migrate-to-remote \
  --url "postgresql://user:pass@host/db" \
  --yes --json
```

On success the command:
1. Imports all sqlite rows into PostgreSQL under the repo's `repo_id`.
2. Renames `sprintctl.db` to `sprintctl.db.migrated-YYYYMMDD-HHMMSS` (read-only
   archive — safe to delete once you have verified the pg data).
3. Writes `.sprintctl/backend.json` with `{"backend": "remote", "repo_id": "<id>"}`.

> **Recovery**: if the import succeeds but the freeze step fails, the sqlite
> file is unchanged. Re-run with `--replace` to clear partial pg data and retry.

### 2. Export SPRINTCTL_URL in each session

```sh
export SPRINTCTL_BACKEND=remote
export SPRINTCTL_URL=postgresql://user:pass@host/db
```

Add these to your repo's `.envrc` (template in `envrc.example`) and run
`direnv allow`. From this point every `sprintctl` command in the repo uses postgres.

### 3. Install the remote extra (if not already installed)

```sh
pip install 'sprintctl[remote]'
# or
pipx install 'sprintctl[remote]'
```

The `remote` extra adds `psycopg[binary]>=3.1`. Local-only use never requires it.

---

## How the backend is resolved

At startup, sprintctl evaluates the backend in this order:

1. **`SPRINTCTL_BACKEND` env var** — `local` or `remote` (default: `local`).
2. **`.sprintctl/backend.json` marker** — if present, the repo is locked to the
   mode recorded in the file. A mismatch between the env var and the marker is
   an error.
3. **`SPRINTCTL_URL`** — required when mode is `remote`.
4. **`repo_id`** — derived from the marker's `repo_id` field, or from the
   directory containing `.sprintctl/sprintctl.db`, or from `.git`. Required for
   remote mode; used as the tenant discriminator in every PostgreSQL query.

Use the read-only doctor to inspect provenance, the resolved configuration,
whether the remote extra is enabled, and the remote schema capability:

```sh
sprintctl doctor
sprintctl doctor --json
```

The JSON contract is `sprintctl-doctor/v1`. It reports only whether
`SPRINTCTL_URL` is configured, never its value. The schema probe connects with
PostgreSQL's read-only transaction setting and never initializes or migrates
the schema. An error report includes explicit reinstall or operator guidance;
doctor itself never upgrades packages or changes backend state.

---

## Session resume in remote mode

Claim mechanics are identical in both modes. The only difference is that the
**local claim recovery file** (`claim-recovery/claim-<id>.json`) is not written
in remote mode — PostgreSQL is the single source of truth.

### Resuming a claim

```sh
# Find active claims by your identity
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json

# Show a specific claim (re-displays the token if you still have it)
sprintctl claim show --id <claim-id> --json
```

### Token recovery in remote mode

There is no local recovery file in remote mode. If your claim token is lost:

```sh
# Adopt the claim — requires the claim to have no prior token (legacy/ambiguous),
# OR be willing to force-rotate with --allow-legacy-adopt
sprintctl claim handoff \
  --id <claim-id> --actor <your-name> \
  --mode rotate --allow-legacy-adopt --json
```

This mints a new token and emits a `claim-ownership-corrected` audit event. The
prior session's token is immediately invalidated.

> **Local mode**: `claim recover --id <claim-id>` reads the local recovery file
> and avoids the adoption step. This path is not available in remote mode.

### Full resume sequence (remote)

```sh
sprintctl usage --context --json          # sprint state, active claims, conflicts
sprintctl next-work --json --explain      # next recommended item + required commands
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json  # find your claims
```

---

## Multi-session coordination

With a PostgreSQL backend, claim expiry is enforced by wall-clock timestamps on
the server. Key operational notes:

- **TTL discipline is critical**: set `--ttl` to at least twice your expected
  task duration and heartbeat at half-TTL. A lapsed claim in postgres is
  exploitable by any agent that can write to the same database.
- **No file-lock serialization**: two concurrent claims on separate items can
  proceed in parallel without any coordination overhead.
- **Claim token uniqueness**: claim tokens are unique within `(repo_id,
  claim_token)`. Token collisions are retried automatically (up to 5 attempts).
- **`purge_expired_claims`**: the `maintain check` command purges expired claim
  rows. In local mode this happens automatically on every `maintain check` call.
  In remote mode, schedule it via `sprintctl maintain check --fix` as a periodic
  background task if long-running sessions are common.

---

## Operator checklist

### Before migration

- [ ] All agents have released or handed off their claims (`sprintctl usage --context`)
- [ ] No active sprint operations in flight
- [ ] PostgreSQL reachable from every host that will run sprintctl
- [ ] `psycopg[binary]` installed on every host (`pip install 'sprintctl[remote]'`)

### After migration

- [ ] Verify pg row counts match sqlite: `sprintctl sprint list --json`
- [ ] Confirm `SPRINTCTL_URL` and `SPRINTCTL_BACKEND=remote` in all session environments
- [ ] Validate `.sprintctl/backend.json` exists with correct `repo_id`
- [ ] Run `sprintctl usage --context --json` and confirm the sprint appears
- [ ] Archive the `.db.migrated-*` backup file once pg data is verified

---

## Reverting to local mode

Remote mode is designed to be a one-way migration per repo. To revert to a
**current-state** sqlite database, use the recovery command:

1. `SPRINTCTL_BACKEND=remote sprintctl db recover-from-remote --output recovery.db --verify`
   — reads every repo-scoped row (sprint, track, work_item, event, claim, ref,
   dep) for the resolved `repo_id` directly from postgres and writes a fresh
   sqlite database at the current local schema, with original IDs preserved.
   Read-only against postgres; refuses to overwrite an existing `--output`
   file. `--verify` reports per-table row-count parity against the postgres
   source and runs sqlite integrity/foreign-key checks. The command also
   writes one synthetic `recovery.completed` event per recovered sprint, in
   the same transaction as the recovered rows, so a recovered database is
   distinguishable from one that was never migrated.
   `ingest_stream`/`ingest_repo_cursor`/`ingest_record`/`authority_decision`
   are intentionally not carried over — they are remote-serving
   infrastructure with no sqlite equivalent.

   Active ownership is not carried over either: `claim_token` is stripped
   from every claim row and active claims are closed as `expired`. A
   recovered database is a **new authority instance** — pre-recovery claim
   credentials do not work against it, agents must reclaim work, and the
   recovered file never contains usable secrets. Claim history is preserved
   for audit.

   If the remote and local schemas have drifted (a migration landed on one
   side but not the other), the command fails closed with a schema-mismatch
   error instead of writing partial rows. If a run fails or is interrupted,
   the partial `--output` file is removed where possible; delete it manually
   before retrying if it remains.
2. Point `SPRINTCTL_DB` at the recovered file, remove `.sprintctl/backend.json`,
   and unset `SPRINTCTL_BACKEND` / `SPRINTCTL_URL`.

If you only need a postgres-to-postgres copy (not a local sqlite database),
`pg_dump` remains the right tool — `db recover-from-remote` never writes to
postgres.

This path should only be necessary in exceptional circumstances (lost postgres
access, environment teardown). Data created in postgres after recovery runs
will not be in the recovered sqlite database — it captures a single point in
time, not an ongoing sync.

---

## Related

- [Disposable PostgreSQL Integration Tests](postgres-integration-tests.md)
- [Resume Work](resume-work.md)
- [Schema Migration Guide](../reference/migration-guide.md)
- [Claim Discipline](../advanced/claim-discipline.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
