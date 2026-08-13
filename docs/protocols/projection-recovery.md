---
doc_id: sprintctl.projection-recovery
status: draft
supersedes: null
---

# Cached projection fault and recovery boundary

This document closes four read-side failure outcomes required by
`adr-outbox-sync-model`. The local SQLite cache is disposable evidence; the
remote ingestion ledger remains the source of truth. These histories authorize
no mutation or repair against a shared remote backend.

## Outcomes

| Fault | Durable observation | Required outcome | Safe recovery |
|---|---|---|---|
| Crash during projection apply | Prior records and prior watermark | The apply transaction rolls back completely; the watermark never describes a partially applied batch | Re-read from the unchanged watermark and retry the same remote range |
| Projection schema rebuild | Empty initialized cache and complete retained remote log | Replay starts at offset 1 and produces the same ordered envelopes and final watermark | Replace the disposable cache only after the fresh replay reaches the expected remote offset |
| Remote advance while offline | Local watermark remains fixed while higher remote offsets exist | Offline reads report the old watermark and its age; they do not imply current authority | On reconnect, request strictly after the visible watermark and apply every offset in order |
| Remote log retained past the local cursor | First available remote offset is greater than `watermark + 1` | Raise `ProjectionGapError`; insert no suffix records and do not advance the watermark | Stop normal replay. Install a separately validated snapshot with an explicit base offset, then resume after that base |

Snapshot creation, validation, and installation are not implemented by this
verification-only tract. A retained suffix is never treated as a snapshot, and
the cache must not skip to its first offset. Until a trusted snapshot path is
implemented, the safe recovery result is an explicit blocked rebuild rather
than a fabricated current projection.

## Executable mapping

| Invariant | Implementation anchor | Executable history |
|---|---|---|
| Records and watermark commit atomically, then retry converges | `sprintctl/projection.py:apply_ingested_records` and `sprintctl/sync.py:synchronize_outbox` | `tests/test_projection.py:test_failed_record_application_rolls_back_records_and_watermark` and `tests/test_sync.py:test_sync_recovers_after_lost_response_or_projection_apply_failure` |
| Offline catch-up consumes the contiguous suffix | `sprintctl/projection.py:apply_ingested_records` | `tests/test_projection.py:test_offline_catch_up_and_full_log_rebuild_converge` |
| Complete-log rebuild converges | `sprintctl/pg.py:list_ingested_records` and projection apply | SQLite history above plus `tests/pg/test_outbox.py::TestProducerOutboxIngestion::test_disposable_remote_history_rebuilds_projection` |
| Retention cannot jump a cursor | `sprintctl/projection.py:ProjectionGapError` | `tests/test_projection.py:test_retained_suffix_cannot_advance_an_empty_projection` |

The PostgreSQL expansion is guarded by disposable credentials and repository
scope. The committed packet
`verification/contexts/projection-fault-recovery.json` is reusable intent, not
an execution result or a claim of snapshot support.

## Guarded projection-backed reads (work item #1162)

`sprintctl/projection_reads.py` adds a per-repository, opt-in flag that lets
some CLI read surfaces consult the cached projection above instead of
querying backend (SQLite/PostgreSQL) directly. It is additive and disabled by
default; enabling or disabling it changes nothing about how the pilot mirrors
or synchronizes data, only where reads look first.

### Enabling and rollback

```bash
sprintctl projection-reads status  [--json]   # inspect current state and cache health
sprintctl projection-reads enable  [--json]   # opt this repository in
sprintctl projection-reads disable [--json]   # rollback: every read surface below
                                               # returns to backend-only behavior
```

`SPRINTCTL_PROJECTION_READS=1|0` (also `true`/`false`, `yes`/`no`, `on`/`off`)
overrides the persisted per-repository file for one invocation, taking
precedence over it -- convenient for tests and quick opt-in without touching
repo-local state. `SPRINTCTL_PROJECTION_STALE_SECONDS` overrides the default
300-second staleness threshold used below.

### Freshness and guarded fallback

Every read surface that consults this flag reports the outcome under a
`"projection"` object in `--json` output and a `Projection: ...` line in text
output:

- `source`: `"projection"` when content was actually served from the cache,
  `"backend"` otherwise.
- `fallback_reason`: `null` when `source` is `"projection"`; otherwise one of
  `"missing"` (no cache file yet), `"never-synchronized"` (cache exists but
  its watermark has never advanced -- see `apply_ingested_records`),
  `"stale"` (watermark age exceeds the threshold), `"schema-upgrade-required"`
  (the cache's `cached_projection_meta.schema_version` does not match
  `projection.PROJECTION_SCHEMA_VERSION` -- run `sprintctl pilot sync` against
  a rebuilt cache, or delete and resynchronize the projection file), or
  `"unsupported-read-surface"` (the cache is healthy but this particular
  surface has no projection-backed content -- see below).
- `watermark_offset`, `watermark_age_seconds`, `schema_version`: always
  populated once the cache file exists, even on a fallback, so an operator or
  agent can see exactly how far behind (or how incompatible) the cache is.

Fallback is always explicit: a stale, missing, unsynchronized, or
schema-mismatched cache is disclosed and the read proceeds against backend,
never silently substituted.

### What is actually projection-backed today

Only observation-classified events (see `contracts.SPRINTCTL_RECORD_TYPE_CLASSES`)
are ever mirrored into the shadow-pilot outbox and, from there, into the
cached projection -- authority commands (item status/title/assignee changes,
claim mutations, sprint transitions) are never mirrored. This bounds what a
guarded read can honestly reconstruct:

| Read surface | Projection-backed content | Always backend-sourced |
|---|---|---|
| `item show` | The item's event/notes history (`events`), reconstructed from cached observation records filtered by `refs.work_item_id` | Item core fields (status, title, assignee, sprint), refs, claims, deps |
| `item list` | none | The full listing (requires materialized item-table state the cache does not have) |
| `next-work` | none | Ready-item suggestions (requires item/dependency state) |
| `usage --context` | none | Sprint summary, claims, ready/blocked/stale items, recent decisions |

`item show` is therefore the one surface where `source` can read
`"projection"`; the other three surfaces always disclose freshness (when the
flag is enabled) via `fallback_reason: "unsupported-read-surface"` once the
cache is otherwise healthy, and their content is unchanged from current
backend-mode behavior. `item list --json` and `next-work --json` (without
`--explain`) keep their existing bare-array shape for compatibility and omit
the `"projection"` key entirely; use `sprintctl projection-reads status --json`
to check freshness for those two commands.

Extending materialized coverage to item/sprint/claim state would require
either mirroring authority-command effects into the cache (a write-path
change explicitly out of this item's scope) or a separate read-model
materializer outside `sprintctl/projection.py`'s existing ingest-envelope
cache -- both are follow-on work, not part of this guarded-reads flag.

### Parity and fallback coverage

`tests/test_projection_reads.py` covers: the config module's default-off
behavior, atomic enable/disable, and env-var precedence; `assess_freshness`
for the healthy, never-synchronized, stale, and schema-mismatch cases; and
CLI-level parity between `item show`'s projection-backed and backend-sourced
event lists for the same mirrored data, plus explicit-fallback disclosure for
missing/stale/never-synchronized/schema-mismatch caches on `item show`, and
freshness disclosure without a content or JSON-shape change on `item list`,
`next-work`, and `usage --context`.
