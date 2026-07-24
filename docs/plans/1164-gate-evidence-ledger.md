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
| 3 | Work adapter/catalog + legacy remote-command inventory | Done | sprintctl #1194 (done) — Click-independent core extracted; `LEGACY_REMOTE_COMMAND_PARITY` inventory published in `sprintctl/vuoro_adapter.py` (11 legacy command groups mapped to catalog operations). |
| 4 | Endpoint/identity workstation cutover | Done | sprintctl #1195 (done) — served-mode compatibility release; endpoint/identity profile wiring; verified via events #1396/#1397 (Group C landed and independently re-verified, commit `f22132c`, CI run 30003277273 green across the full matrix). |
| 5 | Catalog parity for legacy remote-relevant commands (current catalog) | Done | `sprintctl/served_routes.py` wires 10 of 11 `LEGACY_REMOTE_COMMAND_PARITY` entries; the 11th (`work.project.batch` / "project dispatch batching") has no legacy CLI command to give parity to. sprintctl #1212 (done) made that an explicit decision, not an oversight: no CLI entry point exists or is currently planned, so there is nothing for a served route to replace. Parity is complete against the current catalog. |
| 6 | Runtime-role DDL denial (deployed) | Done | appservice #1225 (done, 2026-07-24) — `sprintctl-schema-migrate-v3` job live-verified `Complete 1/1`, 0 restarts, 39h stable. Job embeds a DDL-denial probe: post-migration it connects as `sprintctl_runtime` and attempts `CREATE TABLE`, expecting `InsufficientPrivilege`; `backoffLimit: 0` means the job would fail loudly if DDL unexpectedly succeeded. `sprintctl-cnpg.yaml` confirms `sprintctl_runtime` is DML-only (no CREATE/createdb/createrole/superuser). See sprintctl #1164 ref #347. |
| 7 | Direct credential removal | Open | Owner: appservice #1226 — workstation and cluster credential sweep; rotate/remove all direct DB credentials except the migration job's. |
| 8 | vuoro-dev four-domain evidence | Open | Owner: vuoro #1222 — four-domain handshake/catalog/invocation/decision evidence bundle on vuoro-dev. |
| 9 | Export/recovery rehearsal (cross-backend) | Done | Owner: sprintctl #1219 — rehearsal completed 2026-07-24 against the live served authority using `sprintctl db recover-from-remote` (#1233, commit `b38937e`, CI run 30073378545 green). See "Row 9 rehearsal record" below. |
| 10 | Production promotion evidence | Open | Owner: vuoro #1223 — promotion record (image digest, config hash, migration state, post-promotion health/parity). |
| 11 | Explicit operator gate | Open | Owner: sprintctl #1221 — decision event on #1164 authorizing removal, recorded only once every row above is green. Blocked by this ledger (#1218) and by #1219/#1220. |

## Expected-finding check

Per `docs/plans/next-session-dispatch.md` in agentops, the open rows were
originally #1219/#1220 (sprintctl), #1222/#1223 (vuoro), #1225/#1226
(appservice), confirmed against live item state on 2026-07-24. Since then,
#1219 and #1225 closed (see rows 9 and 6 above).

- Rows 1–6 and 9 are Done, each backed by a done item with recorded
  verification evidence (no unrecorded-but-actually-done gaps found).
- Rows 7, 8, 10 remain Open, owned by #1226, #1222, #1223 respectively —
  confirmed `pending` with no refs/events yet as of 2026-07-24 (checked via
  `sprintctl item show` in each owning repo).
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

## Sources

- agentops `docs/plans/clean-room-1164-cross-repo-backlog.md` (section 2, the
  original ledger draft this formalizes).
- sprintctl items #1163, #1193, #1194, #1195, #1212 (evidence for done rows).
- vuoro items #1222, #1223; appservice items #1225, #1226; sprintctl items
  #1219, #1220, #1221 (open rows, confirmed pending 2026-07-24).
