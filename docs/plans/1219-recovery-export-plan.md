# #1219 Recovery Rehearsal — Missing-Capability Findings and Implementation Brief

Status: planned (dispatch-plan output, 2026-07-24)
Related items: #1219 (rehearsal, blocked), #1164 (split-backend retirement gate), #1220 (fail-closed verification)

## Problem

#1219 is scoped as "export from the served remote authority, restore into recovery
SQLite, verify parity of items/events/claims" — but no remote-to-SQLite export path
exists in sprintctl. The rehearsal is blocked on a missing capability, not on
configuration.

## Verified findings (2026-07-24)

1. **Served remote is healthy and schema-matched.** `uv run --extra remote --extra
   served sprintctl doctor` reports ok, schema 3=3, repo_id `sprintctl`, sprint #407
   present. The blocker is not connectivity.
2. **`export`/`import` are local-only by construction.** Both call `_get_conn()`
   (`sprintctl/cli.py:118`), which invokes `_backend.require_local_backend()`
   (`sprintctl/backend.py:246`) and raises `Error: remote backend is not implemented
   yet.` under `SPRINTCTL_BACKEND=remote`. Reproduced against sprint #407.
3. **The read primitives already exist in `sprintctl/pg.py`** — `get_sprint`,
   `list_tracks`, `list_work_items`, `list_events`, `list_claims_by_sprint`,
   `list_refs_for_items` — but nothing wires them into any Postgres→SQLite path.
4. **The documented recovery story does not produce a recovery SQLite.**
   `docs/guides/remote-mode.md` ("Reverting to local mode") offers `pg_dump`
   (Postgres→Postgres) or restoring a pre-migration `.db.migrated-*` backup; neither
   yields a current-state SQLite with items/events/claims parity. The
   `migrate-to-remote` NDJSON export (`_pg.export_ndjson`) is sqlite→pg one-way.
5. **The existing local `export` envelope omits claims and deps**, and local
   `import` re-sequences all IDs. #1219 requires claims parity, and a recovery
   authority with re-sequenced item IDs would break claims, deps, refs, and every
   external reference to item numbers. The existing export/import pair is therefore
   the wrong vehicle for recovery even after remote enablement.

## Decision: new command `sprintctl db recover-from-remote`

A dedicated, read-only, full-repo, **ID-preserving** recovery command under the
existing `db` maintenance group:

```
SPRINTCTL_BACKEND=remote sprintctl db recover-from-remote --output recovery.db [--verify]
```

Behavior:

- Requires remote backend config (inverse guard of `require_local_backend`); refuses
  to run against local, refuses to overwrite an existing output file.
- Reads all repo-scoped rows for the resolved `repo_id` from Postgres: `sprint`,
  `track`, `work_item`, `event`, `claim`, `ref`, `dep`.
- Writes a fresh SQLite database via `db.init_db()` at the current local schema
  version, inserting rows with **original IDs preserved** (explicit-ID inserts).
- `--verify` emits a deterministic parity report: per-table row counts
  (Postgres vs recovery SQLite) plus a spot sample (e.g. newest and oldest event per
  sprint, active claims) and runs `PRAGMA integrity_check` / `foreign_key_check`.

Decisions folded in (do not re-litigate during build):

- **Full-repo, not per-sprint.** Recovery replaces the split-backend fallback; a
  partial authority is not a recovery authority. Per-sprint export stays with the
  existing `export` command.
- **ID-preserving, not re-sequenced.** Required for claims/deps/refs integrity and
  for external references to survive recovery.
- **Direct pg→sqlite, no JSON intermediate.** Avoids touching the `import`
  re-sequencing/archive-import contract entirely.
- **Excluded tables:** `ingest_stream`, `ingest_repo_cursor`, `ingest_record`,
  `authority_decision` are remote-serving infrastructure with no SQLite equivalent;
  a recovered local authority does not need them. Record the exclusion in the
  command's docs.
- **Placement:** `db` group ("Database maintenance"), implementation in
  `sprintctl/cli.py` + a new module-level function set in `sprintctl/pg.py` (bulk
  repo-scoped readers where the per-sprint listers are insufficient).

## Build scope (new backlog item, blocks #1219)

- `sprintctl db recover-from-remote` as specified above.
- Bulk read helpers in `pg.py` as needed (claims, refs, deps across all sprints of
  the repo).
- Tests: unit for envelope/insert mapping; pg integration test exercising
  export-then-verify against a seeded disposable Postgres
  (`tests/pg/` pattern).
- Docs: update `docs/guides/remote-mode.md` "Reverting to local mode" to make this
  command the primary recovery path; note pg_dump remains the pg→pg option.

Out of scope: deleting split-mode code (#1164), enabling remote for `export`/
`import`, supporting old clients (#1220), any write path to Postgres.

Verification (per sprintctl dispatch manifest — targeted first):

```
uv run pytest tests/test_db_recover.py tests/pg/ -x --tb=short
python /projects/dev/agentops/templates/dispatch/scripts/validate_verification_artifacts.py --root .
```

Risk surfaces touched: `claim-ownership` (`sprintctl/pg.py`) → `verify-state-protocols`
and `reconcile-project-contracts` required on change (depth 2); `document-linked-work`
via `cli.py`/docs.

## #1219 rehearsal, after the capability lands

1. `SPRINTCTL_BACKEND=remote sprintctl db recover-from-remote --output recovery.db --verify`
   against the served authority (read-only; no rollback needed).
2. Point a scratch environment at `recovery.db`, run `sprintctl doctor`,
   `sprint list`, `item list`, claim listing; confirm integrity checks pass.
3. Capture the parity report and operator procedure in docs; attach the rehearsal
   record to #1164's gate-evidence ledger (`docs/plans/1164-gate-evidence-ledger.md`).

## Bonus finding for #1220 (captured on the item)

The workstation global uv-tool install (sprintctl 0.2.0,
`~/.local/share/uv/tools/sprintctl/`) already exhibits the intended fail-closed
surface: `sprintctl doctor` reports `schema-version-mismatch: remote schema 3 is
incompatible with expected 1` and refuses further remote operations. This is live
evidence for #1220's "stale installs fail closed" gate; still needed there:
denied-**write** evidence from the stale install and the documented upgrade path.

## Open questions (user-level, non-blocking)

- Should the recovered SQLite carry a provenance marker (e.g. a
  `recovery_source` note in `.sprintctl/backend.json` or an event) so a recovered
  authority is distinguishable from a never-migrated one? **Decided 2026-07-24:
  yes** — write a single synthetic `recovery.completed` event (sprint-scoped, one
  per recovered sprint, or repo-scoped if the event model requires a sprint_id —
  confirm against the current event-type contract during build) as the last
  insert in `recover-from-remote`, before `--verify` runs.
  **Amended 2026-07-24 (pre-commit review):** the per-sprint events are written
  *inside* the same transaction as the bulk insert (`write_recovery_snapshot`
  `provenance=` parameter), so provenance is all-or-nothing with the data. A
  repo-level `recovery` record (single row instead of N sprint events) needs a
  dual-backend schema addition and is deferred to a follow-up backlog item.

## Semantics decisions from pre-commit review (2026-07-24)

The #1233 diff review escalated into a claim-semantics assessment. Three
decisions were folded into the implementation before commit:

1. **Recovery invalidates ownership.** `write_recovery_snapshot` strips
   `claim_token` from every claim row and closes active claims as `expired`.
   Rationale: a recovered SQLite is a new authority instance; preserving
   bearer tokens would manufacture split-brain continuity (the Postgres
   source may still be live with sessions heartbeating against it) and turn
   every recovery file into a secrets-bearing artifact. Claim history is
   retained for audit. `recovery.completed` payloads carry `claims_closed`.
2. **Schema drift fails closed.** Snapshot rows must match the local table
   column set exactly (modulo `repo_id`); any drift raises
   `RecoverySchemaMismatch` instead of silently intersecting columns. This
   makes a db.py/pg.py migration landing on one side only a loud failure
   rather than quiet data loss during disaster recovery.
3. **Provenance is atomic** (see amendment above). On failure the CLI removes
   the partial `--output` file so a retry does not require manual cleanup.

Follow-ups opened on the backlog: repo-level recovery record (dual-backend
schema change) and the claim-semantics simplification tract (advisory
reservations instead of capability-style leases; cross-repo, vuoro-mirrored).
