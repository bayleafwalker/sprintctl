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
| 6 | Runtime-role DDL denial (deployed) | Open | Owner: appservice #1225 — migration job + migration/runtime role split, deployed, with DDL-denial evidence captured from the live runtime role. |
| 7 | Direct credential removal | Open | Owner: appservice #1226 — workstation and cluster credential sweep; rotate/remove all direct DB credentials except the migration job's. |
| 8 | vuoro-dev four-domain evidence | Open | Owner: vuoro #1222 — four-domain handshake/catalog/invocation/decision evidence bundle on vuoro-dev. |
| 9 | Export/recovery rehearsal (cross-backend) | Open | Owner: sprintctl #1219 — must complete before removal; replaces the split backend's fallback role per the clean-room rollback invariant. |
| 10 | Production promotion evidence | Open | Owner: vuoro #1223 — promotion record (image digest, config hash, migration state, post-promotion health/parity). |
| 11 | Explicit operator gate | Open | Owner: sprintctl #1221 — decision event on #1164 authorizing removal, recorded only once every row above is green. Blocked by this ledger (#1218) and by #1219/#1220. |

## Expected-finding check

Per `docs/plans/next-session-dispatch.md` in agentops, the open rows should be
exactly #1219/#1220 (sprintctl), #1222/#1223 (vuoro), #1225/#1226
(appservice). Confirmed against live item state on 2026-07-24:

- Rows 1–5 are Done, each backed by a done sprintctl item with recorded
  verification evidence (no unrecorded-but-actually-done gaps found).
- Rows 6–10 are Open, owned by #1225, #1226, #1222, #1219, #1223
  respectively — all five confirmed `pending` with no refs/events yet
  (checked via `sprintctl item show` in each owning repo).
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

## Sources

- agentops `docs/plans/clean-room-1164-cross-repo-backlog.md` (section 2, the
  original ledger draft this formalizes).
- sprintctl items #1163, #1193, #1194, #1195, #1212 (evidence for done rows).
- vuoro items #1222, #1223; appservice items #1225, #1226; sprintctl items
  #1219, #1220, #1221 (open rows, confirmed pending 2026-07-24).
