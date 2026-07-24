---
doc_id: 1164-gate-evidence-ledger
status: draft
supersedes: null
---

# #1164 gate-evidence ledger

Built from agentops `docs/plans/clean-room-1164-cross-repo-backlog.md` section
2, with concrete evidence links resolved against sprintctl item history.
Purpose: inventory every gate #1164 names, mark it satisfied-with-evidence or
open-with-owning-item, so #1164 proceeds only when every row is green. This
item only inventories gaps; it does not close them.

| # | Gate required by #1164 | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Selected-repo cutover | Done | sprintctl #1163 (done) — per-repo authority/projection dogfood with parity, watermark/reconciliation lag, and rollback rehearsal evidence. |
| 2 | Deployment-owned migrations, runtime DDL denial (client side) | Done | sprintctl #1193 (done) — client-shipped DDL replaced by a read-only compatibility probe; migration/runtime roles defined; client-side DDL denial verified. Deployed-cluster DDL denial is tracked separately as row 6. |
| 3 | Work adapter/catalog + legacy remote-command inventory | Done | sprintctl #1194 (done) — Click-independent core extracted; `LEGACY_REMOTE_COMMAND_PARITY` inventory published in `sprintctl/vuoro_adapter.py` (12 legacy command groups mapped to catalog operations as of `work.item.note`, 2026-07-24). |
| 4 | Endpoint/identity workstation cutover | Done | sprintctl #1195 (done) — served-mode compatibility release; endpoint/identity profile wiring; verified via events #1396/#1397 (Group C landed and independently re-verified, commit `f22132c`, CI run 30003277273 green across the full matrix). |
| 5 | Catalog parity for legacy remote-relevant commands (current catalog) | Done | `sprintctl/served_routes.py` wires 11 of 12 `LEGACY_REMOTE_COMMAND_PARITY` entries (as of `work.item.note`, 2026-07-24 — see "Follow-up finding" below); the 12th (`work.project.batch` / "project dispatch batching") has no legacy CLI command to give parity to. sprintctl #1212 (done) made that an explicit decision, not an oversight: no CLI entry point exists or is currently planned, so there is nothing for a served route to replace. Parity is complete against the current catalog. |
| 6 | Runtime-role DDL denial (deployed) | Done | appservice #1225 (done, 2026-07-24) — `sprintctl-schema-migrate-v3` job live-verified `Complete 1/1`, 0 restarts, 39h stable. Job embeds a DDL-denial probe: post-migration it connects as `sprintctl_runtime` and attempts `CREATE TABLE`, expecting `InsufficientPrivilege`; `backoffLimit: 0` means the job would fail loudly if DDL unexpectedly succeeded. `sprintctl-cnpg.yaml` confirms `sprintctl_runtime` is DML-only (no CREATE/createdb/createrole/superuser). See sprintctl #1164 ref #347. |
| 7 | Direct credential removal | Done | Owner: appservice #1226. `vuoro-shared` redeployed 2026-07-24 with #1245's corrected design (image `sha256:713f57d6f3...`, schema migrated 3→4, both identity tokens gain `repo_ids: ["*"]`) and verified live end-to-end: a real `work.read.item` and `work.item.note` from the workstation, and independently from devbox-agent after both hosts' CLIs were reinstalled with the fixed `vuoro-client` dependency pin. `vuoro-shared` can now serve every repository tenant a bound identity is authorized for — the capability the other 7 workstation repos (`agentops`, `box`, `actionq`, `aligned-equity`, `_orchestration`, `homelab-analytics`, `scribectl`) need is live. Individually flipping each of those repos' own `.envrc` to served mode is separate follow-up work, not a gate blocker (each just needs `SPRINTCTL_BACKEND=served` + the existing shared credential; no per-repo server-side change required). See "#1245 deployed and verified live" below for the full record. |
| 8 | vuoro-dev four-domain evidence | Done | vuoro #1222 (done, 2026-07-24) — handshake (all 4 domains compatible), full 39-op catalog, and accepted+rejected invocation/decision evidence per domain (two distinct stable error surfaces: `authority-required`, `idempotency-key-required`). Required adding a broader disposable identity to vuoro-dev (previous identity was work:read-only). See sprintctl #1164 ref #351. |
| 9 | Export/recovery rehearsal (cross-backend) | Done | Owner: sprintctl #1219 — rehearsal completed 2026-07-24 against the live served authority using `sprintctl db recover-from-remote` (#1233, commit `b38937e`, CI run 30073378545 green). See "Row 9 rehearsal record" below. |
| 10 | Production promotion evidence | Done | vuoro #1223 (done, 2026-07-24) — `vuoro-shared` deployment/image/migration state recorded; historical sprintctl data backfilled (repo_id=`sprintctl` scope, no prior tool existed for this — see record) with exact row-count parity (sprint 21, track 51, work_item 195, claim 3, dep 57, ref 141, event 469); post-promotion health/parity verified via a live served-mode read (`sprintctl doctor`, `sprint list`, `item show --id 1164` all correct against production). See sprintctl #1164 ref #348. |
| 11 | Explicit operator gate | Done | Owner: sprintctl #1221 — decision event #1442 recorded on #1164 (2026-07-24), authorizing removal now that every row above is green and #1245 is deployed and verified live. Explicit operator sign-off obtained before recording (this is a human decision, not one an agent self-authorizes). Rollback procedure referenced: `sprintctl db recover-from-remote` against retained SQLite exports (#1219, #1233), rehearsed 2026-07-24. |

## Expected-finding check

Per `docs/plans/next-session-dispatch.md` in agentops, the open rows were
originally #1219/#1220 (sprintctl), #1222/#1223 (vuoro), #1225/#1226
(appservice), confirmed against live item state on 2026-07-24. Since then,
#1219, #1225, #1223, and #1222 closed (see rows 9, 6, 10, and 8 above).

- Rows 1–6 and 8–10 are Done, each backed by a done item with recorded
  verification evidence (no unrecorded-but-actually-done gaps found).
- Row 7 (#1226) is the only open evidence row left. It was explicitly gated
  on row 10 (production promotion) landing, which it now has, so it is
  actually actionable now, not just nominally open.
- Row 11 (#1221) is Open and correctly excluded from the "gap" list: it is
  the terminal operator-gate decision, gated on every other row, not an
  independent evidence gap.
- #1220 (old-client fail-closed guidance) has no dedicated ledger row above;
  it is part of #1164's scope text ("old-client failure guidance") rather
  than one of the eleven named gates, but it gates #1164 the same way and is
  tracked as open alongside #1219.

No row was found to be satisfied-but-unrecorded beyond what #1195's own
event history already captured (row 5, closed here as Done rather than the
backlog doc's original "To verify").

## Row 9 rehearsal record (2026-07-24)

Operator procedure and captured evidence for the #1219 cross-backend
export/recovery rehearsal:

1. `SPRINTCTL_BACKEND=remote sprintctl db recover-from-remote --output
   recovery.db --verify` against the live served authority (read-only;
   repo_id `sprintctl`). Parity report: sprint 21/21, track 50/50,
   work_item 187/187, claim 4/4, ref 133/133, dep 45/45,
   event 465 (+21 in-transaction `recovery.completed` provenance events)
   = 486/486 — all `[ok]`; `PRAGMA integrity_check` ok, zero FK violations.
2. Scratch environment pointed at the recovered file
   (`SPRINTCTL_BACKEND`/`SPRINTCTL_URL` unset, `SPRINTCTL_DB=recovery.db`):
   `sprintctl doctor` exit 0 with `schema: expected=14 actual=14`;
   `sprint list`, `item list`, `claim list-sprint` all serve correct
   current-state data.
3. Ownership-invalidation semantics verified live: the one active claim at
   snapshot time (claim #163 on #1219, held by the rehearsing session)
   came back `expired` in the recovered file; zero `claim_token` values
   present in any recovered row; every `recovery.completed` payload carries
   `source_row_counts` and `claims_closed=1`.

Bonus finding fixed during the rehearsal: `sprintctl doctor` falsely
reported `schema-version-mismatch: local schema 14 is incompatible with
expected 11` against any current local database — `doctor.py` and
`pyproject.toml` both pinned a stale copy of the SQLite schema version (11)
while `db.py` migrations had advanced to 14, and the release-integrity test
compared the two stale copies against each other. Fixed by making
`db.CURRENT_SCHEMA_VERSION` the single source of truth (doctor imports it;
pyproject bumped; fresh-database regression test added).

## #1220 stale-install fail-closed record (2026-07-24)

Owner: sprintctl #1220 (old-client fail-closed guidance and write denial).
`sprintctl doctor` schema-version-mismatch on read is already recorded
(event #1433). This closes the write-side half: a disposable PostgreSQL 16
instance was bootstrapped with `pg.PG_DDL` only (schema left at version 1,
below `MINIMUM_SCHEMA_VERSION=4` — the unmigrated-install scenario) and
exercised against the current CLI in `SPRINTCTL_BACKEND=remote` mode.

Every remote-mode command funnels through one choke point,
`cli.py::_get_store()`, which calls
`pg_migrations.startup_schema_handshake()` before returning a store to any
command handler — so schema-compatibility denial is structural, not
per-command. Representative write entry points were run directly against
the stale schema to confirm this empirically:

| Command | Result |
| --- | --- |
| `sprintctl doctor` | exit 1, `schema-version-mismatch: remote schema 1 is incompatible with expected 4` |
| `sprintctl sprint status --id 1 --status active` | exit 1, same schema-incompatibility message, raised at connection setup before the command runs |
| `sprintctl claim start --item-id 1 --actor test-actor` | exit 1, same schema-incompatibility message |

Post-run row counts on the disposable database: `sprint=0, work_item=0,
claim=0, ingest_record=0` — no write reached the database from any denied
command. The upgrade path (reinstall the `uv tool` with the `remote,served`
extras from the current repository) is documented in
`docs/reference/postgres-schema-compatibility.md`.

### Follow-up finding: served catalog gap for item metadata writes

Not part of #1220's original scope. While capturing the write-denial
evidence above, `sprintctl item note`, `sprintctl item ref add`, and
`sprintctl item add` all failed with `could not connect to postgres from
SPRINTCTL_URL: 'NoneType' object has no attribute 'encode'` under a plain
`SPRINTCTL_BACKEND=served` environment (no direct DSN configured). Cause:
`vuoro_adapter.WORK_OPERATION_CONTRACTS` had no `work.note.*` / `work.ref.*`
/ `work.item.add` operations — served mode covered only `work.claim.*`,
`work.lifecycle.arbitrate`, `work.evidence.ingest`, `work.batch.apply`,
`work.project.*`, and `work.pilot.cutover-evidence`. Every other CLI write
fell back to the direct-remote path, which a served-only client (no
`SPRINTCTL_URL`) cannot use.

**`item note` closed the same session (2026-07-24):** added `work.item.note`
to `WORK_OPERATION_CONTRACTS`, a `WorkApplication._item_note` handler
(direct synchronous write via `backend.create_event`, actor always the
authenticated identity — never a client-supplied argument, matching
`work.claim.start`), a `served.item_note` client facade, and an
`item.note` route in `served_routes.py`'s allowlist (now 12 routes, 10
operations in `EXPECTED_OPERATIONS`). Verified: unit tests against SQLite
and a real disposable PostgreSQL, a `served.py` client-shape test, and a
CLI-level test monkeypatching the served facade — all in the same commit
as this record.

**Still open:** `item ref add` and `item add` remain served-incompatible.
Both need real new service-side operations (`work.ref.add`, `work.item.add`)
with their own authority/validation decisions — `pg.add_ref` has no
existing outbox/durable-record precedent to lean on the way `item note`
did, and `item add` additionally touches sprint/track defaulting. Next
session: design `work.ref.add` and `work.item.add` following the
`work.item.note` precedent in this commit (contract in `vuoro_adapter.py`,
handler in `application.py`, client in `served.py`, route in
`served_routes.py`, tests against both SQLite and real PostgreSQL).

## #1245 design correction, implemented (2026-07-24)

vuoro #1245's first merge (sprintctl PR #3, vuoro PR #1) correctly made
`WorkApplication.invoke()` resolve `repo_id` per call instead of once at
process start — but sourced it from `context.identity.repo_id`, a field
fixed on the `Identity` at bearer-token mint time. That would have bound
each bearer token to exactly one repository, forcing a new production
token per `(host, repo)` pair (9+ instead of the 2 that exist:
`workstation-vuoro`, `devbox-agent-vuoro` — one per **host**). Caught
before writing anything to the production `vuoro-identities` secret
(SOPS-encrypted, `appservice` repo; it was decrypted read-only to confirm
its exact contents).

**Corrected and merged same session** (sprintctl PR #5, vuoro PR #2, both
CI-green): the client now sends `repo_id` in the invocation envelope
(`InvocationRequest`/`InvocationRequestV2.repo_id`), matching how every
other repo-identity resolution in this codebase already works
(`.sprintctl/backend.json`, cwd-driven). `Identity.repo_ids:
frozenset[str]` replaces the single `repo_id` field, with an `ALL_REPOS =
"*"` wildcard and `authorizes_repo()`; `OperationDefinition.repo_scoped:
bool` marks which operations require it (every `work.*` operation except
`work.project.*`, which aggregates across member repos via its own
`origin_repo` argument). `app.py`'s `_dispatch` validates the client's
`repo_id` against the identity's authorization before `invoke()` runs.
`sprintctl.application.WorkApplication.invoke()` reads
`context.repo_id`; `served.py` and every served CLI call site (`item
show`, `item status`, `sprint status`, `claim start/heartbeat/
handoff/release`, `item note`, `authority sync`, `pilot
cutover-evidence`, `next-work`) now send `config.repo_id` (already
resolved from cwd by `load_backend_config`, previously unused for this
purpose).

Verified: vuoro-service (74 tests, 4 new integration tests exercising
missing/unauthorized/authorized/wildcard `repo_id` end-to-end through
`create_app`), vuoro-client (10 tests), sprintctl full suite (1198
passed) and disposable-PostgreSQL suite (109 passed) — all green.

## #1245 deployed and verified live (2026-07-24)

All four remaining deployment steps completed, with production identity
edits confirmed via explicit user sign-off before executing:

1. Built the sprintctl wheel from commit `f96fb54` and published it as
   GitHub release `vuoro-adapter-v1-f96fb54`; updated vuoro's
   `adapter-pins.json` work entry (source_revision, artifact_url,
   artifact_sha256) and verified `docker build .` succeeds with the new
   pin (vuoro PR #3, merged).
2. Tagged `vuoro-service-v0.1.2`, triggering `publish-service-image.yaml`
   → `ghcr.io/bayleafwalker/vuoro-service@sha256:713f57d6f341d54c1964541
   5a480e9a8e949d6d98dbe95e3b940647da1098597`.
3. Updated the production `vuoro-identities` secret (SOPS-encrypted,
   `appservice`): both `workstation-vuoro` and `devbox-agent-vuoro` gain
   `"repo_ids": ["*"]`. Committed in the same commit as the deployment
   image digest bump — deploying the image without this would have
   crash-looped the service, since the new `load_identities` requires a
   non-empty `repo_ids` on every `work:`-authority identity.
4. Pushed to `appservice` main, forced a Flux reconcile. The rollout hit
   two real issues, both resolved:
   - **Schema-version mismatch**: the new image requires PostgreSQL
     schema 4 (this session's `_apply_schema_version_4`, the
     `ref_type='command'` change), but production was still at 3. Ran a
     one-off migration job (`vuoro-migrate-v4-work.yaml`, following
     `vuoro-migrate-v1.yaml`'s established pattern) — `sprintctl
     remote-schema migrate`: `from_version=3, to_version=4`. Committed to
     `appservice` for audit history.
   - **Stale lock blocking the migration**: the migration's `ALTER TABLE
     ref` blocked for several minutes on an `idle in transaction`
     connection (pid 674, `vuoro_work_runtime`, open since 10:19 that
     morning — a leaked connection from the old pod's single
     long-lived-connection composition pattern, `sprintctl.pg`'s one
     `PgStore` per process with no reconnect/pooling; a pre-existing
     latent issue, not introduced this session). Terminated it with
     `pg_terminate_backend`; the old pod kept serving throughout (its
     next request opened a fresh connection) and the migration completed
     immediately after.
   - The Deployment's rolling update kept the old pod serving the whole
     time — zero observed downtime.

**Live verification, both hosts, after this deploy:**
- `sprintctl doctor` → `ok`, full catalog including the new
  `work.item.note` operation.
- A real `work.read.item` (`item show --id 1220`) and the first-ever
  `work.item.note` call in production (events #1440 workstation, #1441
  devbox-agent) both succeeded.
- Both hosts' installed `uv tool` needed reinstalling: their
  `vuoro-client` dependency was still pinned to commit `55d80c5`
  (predates `AsyncVuoroClient.invoke` sending `repo_id` at all) — fixed by
  bumping `pyproject.toml`'s `served` extra to vuoro commit `1cce813`
  (includes the fix) and reinstalling on both hosts.
- Operational note for future sessions: `direnv exec DIR CMD` only loads
  `DIR`'s `.envrc` environment — it does **not** `cd` into `DIR`. Since
  served-mode `repo_id` is resolved from the process's actual cwd
  (`.sprintctl/backend.json`), a scripted invocation must `cd DIR` first
  (letting the normal shell hook fire) rather than relying on `direnv exec
  DIR` from elsewhere — the latter silently resolves the wrong (or no)
  repo_id with no error, since both production identities carry a
  wildcard `repo_ids: ["*"]` that authorizes any string.

## Sources

- agentops `docs/plans/clean-room-1164-cross-repo-backlog.md` (section 2, the
  original ledger draft this formalizes).
- sprintctl items #1163, #1193, #1194, #1195, #1212 (evidence for done rows).
- vuoro items #1222, #1223; appservice items #1225, #1226; sprintctl items
  #1219, #1220, #1221 (open rows, confirmed pending 2026-07-24).
