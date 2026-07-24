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
| 7 | Direct credential removal | Open (scope corrected, #1245 code merged but not deployable as designed) | Owner: appservice #1226. `sprintctl`'s own workstation `.envrc` flipped to served mode 2026-07-24 and verified (`doctor` ok, live `item show` correct) — that repo's direct credential can be removed. The other 7 workstation repos (`agentops`, `box`, `actionq`, `aligned-equity`, `_orchestration`, `homelab-analytics`, `scribectl`) cannot yet. vuoro #1245's code merged (sprintctl PR #3, vuoro PR #1, both CI-green) but its design is wrong for production: `Identity.repo_id` binds one bearer token to exactly one fixed repository, resolved entirely server-side from the identity registry. Production has only 2 tokens (`workstation-vuoro`, `devbox-agent-vuoro` — one per *host*, not per *repo*), and every other repo-identity resolution in this codebase is client/cwd-driven (`.sprintctl/backend.json`, `_repo_id_from_cwd()`). Deploying #1245 as merged would require minting 7+ new per-repo tokens instead of reusing the 2 existing ones — see "#1245 redesign needed before deploy" below for the corrected design and why the production identity secret was deliberately left untouched. |
| 8 | vuoro-dev four-domain evidence | Done | vuoro #1222 (done, 2026-07-24) — handshake (all 4 domains compatible), full 39-op catalog, and accepted+rejected invocation/decision evidence per domain (two distinct stable error surfaces: `authority-required`, `idempotency-key-required`). Required adding a broader disposable identity to vuoro-dev (previous identity was work:read-only). See sprintctl #1164 ref #351. |
| 9 | Export/recovery rehearsal (cross-backend) | Done | Owner: sprintctl #1219 — rehearsal completed 2026-07-24 against the live served authority using `sprintctl db recover-from-remote` (#1233, commit `b38937e`, CI run 30073378545 green). See "Row 9 rehearsal record" below. |
| 10 | Production promotion evidence | Done | vuoro #1223 (done, 2026-07-24) — `vuoro-shared` deployment/image/migration state recorded; historical sprintctl data backfilled (repo_id=`sprintctl` scope, no prior tool existed for this — see record) with exact row-count parity (sprint 21, track 51, work_item 195, claim 3, dep 57, ref 141, event 469); post-promotion health/parity verified via a live served-mode read (`sprintctl doctor`, `sprint list`, `item show --id 1164` all correct against production). See sprintctl #1164 ref #348. |
| 11 | Explicit operator gate | Open | Owner: sprintctl #1221 — decision event on #1164 authorizing removal, recorded only once every row above is green. Blocked by this ledger (#1218) and by #1219/#1220. |

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

## #1245 redesign needed before deploy (2026-07-24)

vuoro #1245's merged code (sprintctl PR #3, vuoro PR #1) is real,
CI-verified, and correctly makes `WorkApplication.invoke()` resolve
`repo_id` per call instead of once at process start — but the *source* of
that per-call `repo_id` is wrong for production. It reads
`context.identity.repo_id`, a field fixed on the `Identity` at bearer-token
mint time (`load_identities` in `composition.py`). That means each bearer
token still only ever unlocks exactly one repository, decided by whoever
issues the token — not "one running application serves every repository
tenant a bound identity is authorized for," which is what the composition
docstring (and #1245's actual goal) claims.

This was caught before touching the production `vuoro-identities` secret
(SOPS-encrypted, `appservice` repo): it holds exactly 2 tokens today,
`workstation-vuoro` and `devbox-agent-vuoro` — one per **host**, matching
how every other repo-identity resolution in this codebase already works
(`.sprintctl/backend.json`, `sprintctl.pg._repo_id_from_cwd()`: the
*client* knows and states which repo it's in; the server doesn't dictate
it). Deploying #1245 as merged would force minting a separate token per
`(host, repo)` pair — 9+ tokens instead of 2 — a real ongoing
credential-management burden that doesn't match the existing model, and
was never actually decided as the intended design.

**Corrected design, not yet implemented:**

1. Add `repo_id` to the wire protocol envelope (`InvocationRequest` /
   `InvocationRequestV2` in `vuoro_service/contracts.py`, currently
   `schema_version: "invocation/v1"` / `"invocation/v2"` — this is an
   additive envelope field, likely warranting `"invocation/v3"` or an
   additive bump of v2). The *client* sends it, exactly like
   `basis_revision`/`idempotency_key` are already client-supplied envelope
   fields, not per-operation JSON-schema arguments.
2. `vuoro_service/identity.py`'s `InvocationContext` gains a `repo_id: str`
   field (envelope-level), separate from `Identity`.
3. `Identity` gains an authorization concept instead of a single fixed
   `repo_id` — e.g. `repo_ids: frozenset[str]` with a wildcard sentinel
   (`{"*"}`) meaning "every repo," since both existing production
   identities are trusted per-host credentials that should authorize every
   repo on that host, not an enumerated list.
4. `app.py`'s `_dispatch` extracts `repo_id` from the request, validates it
   against `identity.repo_ids` (exact membership or wildcard), and passes
   it into `InvocationContext.repo_id`.
5. `WorkApplication.invoke()` (sprintctl `application.py`, already reworked
   for #1245) reads `context.repo_id` instead of
   `context.identity.repo_id`.
6. `packages/vuoro-client` (`AsyncVuoroClient.invoke`) and
   `sprintctl.served`'s facade functions (`served.py`) start sending
   `repo_id` (from `.sprintctl/backend.json`, same resolution the
   local/remote paths already use) with every `work.*` call.
7. `composition.py`'s `load_identities` accepts `repo_ids` (plural) instead
   of `repo_id`, still requiring it non-empty for any `work:`-authority
   identity.
8. Rework the tests this session added for the identity-bound design
   (`test_identity_registry_requires_repo_id_for_work_authorities`,
   `test_identity_registry_scopes_a_work_authority_to_its_repo_id` in
   vuoro, `test_invoke_scopes_to_the_identitys_repo_id_...` in sprintctl)
   to the corrected envelope-driven design.

Only after that redesign should: a new sprintctl adapter wheel be built and
published, `adapter-pins.json`'s work entry updated, a new `vuoro-service`
image built and pushed, `deployment.yaml`'s image digest updated, the
production `vuoro-identities` secret updated (existing 2 tokens gain
`repo_ids: ["*"]` — no new tokens needed under the corrected design), and
`vuoro-shared` redeployed. None of that happened this session; the
production identity secret was read (decrypted, to confirm exactly what it
contains) but never written.

## Sources

- agentops `docs/plans/clean-room-1164-cross-repo-backlog.md` (section 2, the
  original ledger draft this formalizes).
- sprintctl items #1163, #1193, #1194, #1195, #1212 (evidence for done rows).
- vuoro items #1222, #1223; appservice items #1225, #1226; sprintctl items
  #1219, #1220, #1221 (open rows, confirmed pending 2026-07-24).
