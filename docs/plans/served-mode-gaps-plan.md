# Served-Mode Gaps — Implementation Brief

Status: partially implemented (source merged 2026-07-26; adapter release and
deployment verification remain separately authorized)
Related items: #1982, #1983, #1984, #1985 (sprint #407, track `served-mode-gaps`);
#1164 (split-backend retirement gate, whose dependency chain is now clear);
#1195 (served-backend cutover this whole gap set traces back to)

**ID renumbering note (2026-07-25):** these items (plus the sibling closed-loop
record item, formerly #1251) were originally filed via the direct-remote CLI
path against the legacy pre-cutover `sprintctl-cnpg-main` Postgres cluster on
2026-07-24, after that repo's one-off `sprintctl-remote-backfill` window had
already closed -- they never synced into `vuoro-shared-db` and were invisible
to every served-mode caller. Recovered 2026-07-25 via a scoped, transactional
row migration directly into `vuoro-shared-db`'s `work` schema (content and doc
refs preserved verbatim; sprint/track linkage preserved by reusing the
existing sprint #407 and newly-created `served-mode-gaps` track; fresh
`aggregate_uuid`s and Postgres-assigned identity IDs, since the destination's
global identity sequence already had unrelated rows at 1247/1250). Old -> new
ID mapping: #1247 -> #1982, #1248 -> #1983, #1249 -> #1984, #1250 -> #1985,
#1251 -> #1986. All citations below are updated to the new IDs. See sprintctl
item #1989 (closed-loop record: this renumbering + the legacy DB being made
read-only in the same pass; filed `done`) for full evidence, including the
old->new mapping, migration mechanism, and revoke verification. Note #1986
is the migrated item itself (the authority_repo_uuid fix record), not the
closed-loop record -- #1989 is the new record documenting this recovery.

## Why these are in-tract, not deferred work

All four were discovered during the #1220/#1221 gate-status reconciliation
(2026-07-24, session 7), while the served substrate was already assumed
complete per #1195. They are direct #1195 completion gaps -- the served
catalog covers the day-to-day agent-dispatch commands (`item show`, `item
note`, `claim`, `sprint pilot`) but not the admin/reporting/cross-tool
surface. Sequencing them into a "someday" tract would leave #1164's eventual
dead-code removal working from an incomplete parity inventory. This session
orchestrates the plan; a following session (or this one, resumed) executes
it against this brief.

## Blind-agent command coverage (2026-07-26 source state)

The served adapter now has explicit catalog routes for `item list`, `item ref
add/list/remove`, `item dep add/list/remove`, `claim list`, `claim
list-sprint`, identity-filtered `claim resume`, and non-secret `claim show`.
Ref/dependency listing is
an explicit CLI command backed by `work.read.item`; it is not an undocumented
instruction to scrape `item show`.  Writes are direct catalog operations and
never fall back to a local or direct-Postgres store.

The following remain deliberately fail-closed until their *full* contracts are
served: `usage --context`, `next-work --explain`, `session resume`, sprint
`handoff`, `claim recover`, coordinator `claim create`, and
atomic `item done-from-claim`.  A raw aggregate read is not context parity:
the context contract also owns stale-work, conflict, decision, and next-action
derivation.  `sprintctl doctor` probes every operation used by the supported
routes, so a client cannot silently claim this coverage against an old Vuoro
catalog.

## Sequencing

```
#1982 (vuoro read op: work.read.events)
   |
   +--> #1983 (kctl served source)
   |
   +--> #1984 event-list sub-part (reuses work.read.events directly)

#1984 has three independent sub-parts beyond event-list -- event-add,
sprint-show, item-add -- each needs its own new operation and can land in
any order, or in parallel across separate builds.

#1985 is fully independent of the other three; smallest, most contained
change; no reason to sequence it after anything.
```

Recommended execution order: **#1985 first** (small, isolated, no cross-repo
release needed beyond sprintctl itself), then **#1982**, then **#1983 and
#1984's event-list sub-part in parallel**, then **#1984's remaining three
sub-parts** (event-add, item-add, sprint-show, in that order -- sprint-show
last because its `--detail` mode needs its own design pass, see below).

Every operation added to the served catalog must also be added to
`served.py`'s `_DOCTOR_PROBE_COMMAND_PATHS` / `EXPECTED_OPERATIONS` in the
same change. This is not optional bookkeeping: the #1195 postmortem found
the catalog probe had already silently drifted out of sync with newly-wired
routes once (missing `claim.handoff`, then `pilot.cutover-evidence`),
meaning `doctor` was not actually verifying the catalog before commands ran.
Recurring this omission is the single most likely way this plan ships
broken and undetected, exactly like the `authority_repo_uuid` bug did.

## #1982 — Vuoro work-adapter sprint-scoped-events read operation

**Fully a sprintctl-side change.** `vuoro_service/composition.py`'s
`register_work_catalog` call already registers whatever is present in
`sprintctl.vuoro_adapter.WORK_OPERATION_CONTRACTS` generically -- landing
this needs no vuoro source change, only the same
build-wheel/cut-release/bump-pin/redeploy sequence already exercised this
session for the `authority_repo_uuid` fix (sprintctl commit `0938568` ->
release `vuoro-adapter-v1-0938568` -> vuoro `adapter-pins.json` bump at
commit `3c262a8` -> tag `vuoro-service-v0.1.4` -> appservice
`vuoro-shared/app/deployment.yaml` digest bump -> `flux reconcile
kustomization vuoro-shared --with-source` -- see sprintctl item #1986 for
the closed-loop record of that exact sequence).

Four touch points, all in `sprintctl/`:

1. **`sprintctl/vuoro_adapter.py`** — add a `work.read.events` entry to
   `WORK_OPERATION_CONTRACTS` (`WorkOperationContract(name, input_schema,
   result_schema, required_authority="work:read", execution_semantics="read",
   idempotency="not-allowed")`). Follow the existing `work.read.records` /
   `work.read.decisions` tuple-comprehension template a few lines above.
   Input schema: `sprint_id` (int, required), `work_item_id` (int, nullable,
   optional filter), `after_offset`/`limit` (optional, for pagination
   parity with `event list --limit`).
2. **`sprintctl/application.py:333-345`** (`WorkApplication.invoke`'s
   `handlers` dict) — add `"work.read.events": target._read_events`, plus
   a new `_read_events` method modeled directly on the existing
   `_read_item` method (~line 367), which already does
   `self.backend.list_events(self.store, item["sprint_id"])` filtered in
   Python. Backend calls already exist and need no new DB work:
   `db.py:1057 list_events(conn, sprint_id)` /
   `pg.py:1806 list_events(store, sprint_id)`, plus a limited variant
   `list_events_limited(..., limit)`.
3. **`sprintctl/served_routes.py`** — add
   `ServedRoute("event.list", "work.read.events")`. The file's own
   docstring already documents that plain `event add`/`event list` (as
   opposed to the already-routed `event.observation.add`) were deliberately
   left out of #1195's scope, not overlooked -- this is the citation for why
   this is a #1195 completion gap, not new scope creep.
4. **`sprintctl/served.py`** — one new facade function `read_events`,
   copying the exact shape of `read_item`/`read_sprints` (~lines 65-96):
   build an `arguments` dict, `asyncio.run(_invoke_operation(served_profile,
   "work.read.events", arguments, repo_id=repo_id))`.

**Open question:** should `work.read.events` support `event_type`/
`--knowledge` filtering server-side (matching `event list`'s CLI flags), or
should the served facade fetch everything for the sprint and filter
client-side? Server-side filtering is more scalable for large sprints but
enlarges the input schema surface; client-side filtering is a smaller
served-catalog surface but ships more bytes over the wire. Recommend
server-side filtering for `work_item_id` (cheap, already the `_read_item`
pattern) and client-side for `event_type`/`--knowledge` (rarely
selective enough to matter, avoids catalog schema churn if CLI flags
change).

## #1983 — kctl served-mode source

`/projects/dev/kctl/kctl/source.py` (216 lines) has a clean three-way
dispatch shape already (`open_sprintctl_source()`, line 183) but only
implements `local`/`remote`; `served` falls through to a `ValueError`
(lines 192-195). `RemoteSprintctlSource` (line 84) queries sprintctl's
Postgres schema directly via raw psycopg (`fetch_events`,
`list_preflight_targets`) -- kctl has **zero** dependency on the
`sprintctl` package or `vuoro-client` today.

Plan: add a `ServedSprintctlSource` mirroring `RemoteSprintctlSource`'s two
methods, but backed by `vuoro-client` directly (same pattern sprintctl
itself uses: `pyproject.toml`'s `served` extra, pinned by commit SHA) rather
than shelling out to the `sprintctl` CLI -- this preserves kctl's existing
architecture of reading the shared substrate directly instead of adding a
process-exec dependency on another tool's CLI. `fetch_events` maps directly
onto #1982's `work.read.events`. `list_preflight_targets` has no obvious
existing operation to reuse (not mentioned in #1982's scope) -- **open
question for whoever picks up #1983: does `list_preflight_targets` need its
own new served operation, or can it be satisfied by composing
`work.read.sprints` (already served) with something else?** Needs a design
pass before implementation starts; don't assume it's covered by #1982 as
filed.

## #1984 — event add / event list / sprint show / item add served-mode awareness

All four currently call `_get_store(obj)` unconditionally, bypassing the
`_served_config_or_none` branch that `item status`/`item note` already use.
The four sub-parts are **not uniform** -- do not treat this as one
mechanical find-replace:

- **`event list`** (`cli.py:3749`) — once #1982 lands, this is direct reuse
  of `served.read_events`. Smallest of the four sub-parts; do this one
  alongside or immediately after #1982, not as a separate later pass.

- **`event add`** (`cli.py:2464-2519`, `_event_add_impl`) — currently a
  direct, non-outbox, non-CAS `create_event` call. Best template is
  `WorkApplication._item_note` (`application.py:669-720`), whose own
  docstring says it's exactly this shape: a direct synchronous write
  mirroring the local CLI's `create_event`, no outbox/retry semantics,
  forcing the recording actor to the authenticated identity rather than a
  client-supplied field (important: don't let a served `event add` accept
  an arbitrary `actor` argument the way the local CLI does). New operation:
  `work.event.add`, copy `_item_note`'s shape targeting `create_event`.

- **`item add`** (`cli.py:1100`, calls `create_work_item` + track
  resolution) — same non-outbox, no-CAS shape as `event add`. New
  operation: `work.item.create`, forcing actor to authenticated identity,
  resolving/creating the track server-side (`get_or_create_track`
  semantics move server-side, not client-side).

- **`sprint show`** (`cli.py:716-730`) — the odd one out. Beyond the sprint
  row itself it has `--detail` (sprint/track health + stale-item-count
  aggregation, extra queries), `--watch` (interactive polling loop, not
  a single request/response shape at all), and `--id` defaulting to the
  active sprint. **Do not attempt this as one operation.** Phase it: a
  basic `work.read.sprint` (singular, by id or "active") operation first,
  covering plain `sprint show` with no flags. `--detail`'s health
  aggregation is an explicit follow-up decision requiring its own design
  pass (it may already be servable by composing `work.read.sprints` plus
  something else -- unconfirmed, don't assume). `--watch` may not be
  servable at all in the same shape (polling loop against a request/
  response catalog operation is a client-side concern, not a new operation)
  -- resolve by having `--watch` poll the new `work.read.sprint` operation
  in a loop client-side, same as it presumably already polls locally.

## #1985 — advisory lock for `_advance_identity_sequences`

`sprintctl/pg.py:1185-1201`. For each table: `SELECT
pg_get_serial_sequence(...)` then `SELECT setval(seq, COALESCE(MAX(id),1),
EXISTS(...))`. Two call sites exist:

- `pg.py:1069`, inside schema migration v2 -- **already lock-protected**,
  `migrate_schema` wraps its whole body in
  `pg_advisory_xact_lock(*SCHEMA_MIGRATION_LOCK_KEYS)` per
  `pg_migrations.py:138`. No change needed here.
- `pg.py:2881` and `pg.py:2922`, inside `import_records` (the
  remote-backfill/import path) -- **not** lock-protected. This is the
  actual race: sequences are global across repos (per the existing code
  comment: "global sequence ids may already be taken by other repos"), so
  two concurrent backfills into the same shared Postgres can both read the
  same stale `MAX(id)` and `setval` to it.

Fix: define `IDENTITY_SEQUENCE_LOCK_KEYS` as a new two-int32 magic-constant
pair (matching the existing convention at `pg_migrations.py:25`,
`SCHEMA_MIGRATION_LOCK_KEYS = (0x53505249, 0x4E544354)` -- ASCII-derived,
e.g. from "SEQL"/"OCKS" or similar), and acquire
`pg_advisory_xact_lock(%s, %s)` with those keys once at the top of
`import_records`'s existing `with store.conn.cursor() as cur:` transaction
block -- the whole function already runs in one transaction, so a single
acquisition covers both call sites. Use the two-int32 fixed-key form (like
schema migration), not the one-arg `hashtextextended(ref, 0)` form used
in `authority.py:537,649`, because this lock is correctly global (not
per-repo) -- sequences aren't repo-scoped.

Test: two concurrent `import_records` calls against the same destination,
asserting the resulting sequence is `>= MAX(id)` across *all* rows inserted
by both callers, not just the last writer's stale view (a naive test that
only checks one call's rows would not catch the race this is fixing).

## Next-steps checklist for the orchestrating session

- [x] #1985: advisory lock + concurrency regression test landed in sprintctl
      (`8c7111e`).
- [x] #1982: `work.read.events` (operation contract, handler, served
      route, served.py facade), update `_DOCTOR_PROBE_COMMAND_PATHS` /
      `EXPECTED_OPERATIONS`, and tests landed in sprintctl (`eadc53a`).
      Remaining: cut a new `vuoro-adapter-v1-<sha>` release, bump vuoro's
      adapter pin, deploy through the separately authorized appservice path,
      then verify `doctor: ok` and live served calls from both hosts.
- [x] #1984 event-list sub-part landed in sprintctl (`d8bd7fa`), pending the
      same adapter release/deployment verification as #1982.
- [x] #1983: `ServedSprintctlSource` landed in kctl (`2187c5c`) with a
      `served` extra and durable event-ID watermarking. Preflight deliberately
      reports the absent `maintain.check` catalog operation instead of
      claiming a clean result; a served `kctl doctor` is not an existing
      command to verify.
- [x] #1984 remaining sub-parts: `event add` (`work.event.add`, recording
      actor forced to the authenticated identity), `item add`
      (`work.item.create`, with server-side track creation), and plain
      `sprint show` (`work.read.sprint`) landed in sprintctl. Each is routed,
      doctor-probed, and covered by application/facade/CLI tests. `--watch`
      safely polls the selected read operation client-side. Follow-up source
      work added the dedicated `work.read.sprint-detail` aggregate: it builds
      health, stale count, track health, and takeup from one server-side
      snapshot, and the CLI preserves the local JSON/text contract without
      opening a local store. Remaining: the shared adapter release/deployment
      and live served verification authorized separately from this source
      change.
- [x] Update this doc's status as work lands in more than one pass.
