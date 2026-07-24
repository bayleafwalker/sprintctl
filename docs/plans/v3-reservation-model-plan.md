---
doc_id: v3-reservation-model-plan
status: planned
supersedes: null
---

# v3 Clean-Sweep Plan: Claims as Advisory Reservations

Dispatch-plan output, 2026-07-24 (planning agent brief, assessed and placed
into the backlog by the orchestrating session; backlog item IDs added at
placement time). Substrate assumed: retained vuoro/sprintctl served authority
on Postgres (per agentops `docs/plans/assessment-review.md`, migration
complete at f22132c/#1195). Builds on the #1233 recovery semantics committed
at b38937e and the #1219 rehearsal record in
`docs/plans/1164-gate-evidence-ledger.md`.

Operator directive: treat this as a deliberate v3 shift that must hold up
long-term. Do not weight migration workload or backward compatibility
heavily; prefer deleting machinery over preserving it.

## 1. Long-term goal state

**A claim is a visible reservation, not a capability.** The v3 claim row is a
coordination signal: "this actor, in this session, is working item N." It is
created without minting secrets, read without redaction, and released or
overridden without proof. Its value is conflict *detection* — surfacing that
two sessions are converging on one item — not conflict *prevention*. The
record is: `claim_id, work_item_id, actor, session_id, status
(active|released|interrupted), claimed_at, last_activity_at`, optionally
`expires_at` used only for stale display and takeover policy.

**Mutation safety lives where it can actually be enforced.** Concurrent item
mutations are protected by expected-revision compare-and-swap (the
`item:<uuid>@status:<status>` revision already implemented in
`sprintctl/authority.py:item_revision` for the arbiter path, extended to the
direct CLI paths), by DB transactions and the served authority's
single-writer serialization, and by command idempotency (immutable authority
records with `event_id` identity and payload fingerprint — already
`concurrency-tested` per `docs/protocols/authority-faults.md`). Nothing about
a claim gates a mutation.

**Session presence is ephemeral runtime state.** `runtime_session_id`,
`instance_id`, hostname, pid, git context remain on the reservation as
advisory metadata for resume and operator display. Liveness is a display
property (`last_activity_at` age), not a lease. There is no heartbeat
contract to violate.

**Execution ownership is a dispatcher/runtime concern.** Exactly-once
execution, fencing of duplicate workers, and crash-safe resume belong to
actionq execution IDs, git branches/worktrees, and the served authority's
request ledger — not to bearer tokens in a work tracker.

**Recovery is invalidate-and-reacquire.** A recovered database is a new
authority instance: active reservations close as `interrupted`, a single
repo-level recovery record captures provenance atomically, and sessions
simply re-reserve. Nothing needs to be "carried over" because nothing was a
credential.

### Invariants that survive

- At-most-one *accepted decision* per idempotent authority command (request
  ledger, digest conflict rejection).
- Status-transition validity (`VALID_TRANSITIONS`), dependency gating,
  close-boundary atomicity, capability-receipt pointer validation.
- Append-only event history; claim history retained for audit.
- Backend parity as equivalent accepted/rejected histories.
- Recovery fails closed on schema drift; provenance is atomic with data.
- Revision CAS: a mutation whose basis is stale is durably rejected without
  effect.

### Invariants deliberately dropped

- "At most one live exclusive owner" as an enforced invariant (with its
  coordinator-delegation exception). Becomes: conflicting reservations are
  *detected and surfaced*; default reserve refuses on conflict but override
  is a first-class, proof-free operation.
- Proof-gated item mutation (`claim_id + claim_token` as ownership proof).
- Token rotation, rotate-mode handoff, legacy adoption, token recovery
  files, `lease_epoch` as future fencing, TTL-as-security ("a lapsed claim
  is exploitable" ceases to be a threat model because a claim grants
  nothing).
- Recovery continuity of active claims (already dropped by #1233; v3
  finishes the job by making it trivial).

## 2. Sequenced backlog items

Placed 2026-07-24 in sprint #407, track `v3-reservations` (except V3-7 =
existing #1234 in `projection-cutover`, and V3-9's agentops half in the
agentops backlog). Verification for every item: targeted pytest per the item
description, then
`python /projects/dev/agentops/templates/dispatch/scripts/validate_verification_artifacts.py --root .`.
Items touching the `claim-ownership` risk surface require
`verify-state-protocols` + `reconcile-project-contracts` at depth 2 per the
dispatch manifest.

| Plan ID | Backlog item | Title |
| --- | --- | --- |
| V3-1 | #1235 | Add expected-revision compare-and-swap to direct item and sprint mutations |
| V3-2 | #1236 | Remove proof-gated mutation: item and sprint transitions no longer require claim proof |
| V3-3 | #1237 | Collapse authority claim commands to credential-free reservation operations (catalog v2) |
| V3-4 | #1238 | Demote the claim schema: drop claim_token and lease_epoch, add interrupted status |
| V3-5 | #1239 | Replace heartbeat/TTL ceremony with implicit activity tracking and stale display |
| V3-6 | #1240 | Delete handoff rotation, token recovery, and adoption; replace with plain reassign |
| V3-7 | #1234 | Add repo-level atomic recovery record replacing per-sprint recovery.completed events (re-scoped for v3) |
| V3-8 | #1241 | Rewrite the coordination protocol contract, overlay, and operator guides for the reservation model |
| V3-9 | agentops #1242 | Update cross-repo agent guidance mirrors for the reservation model (covers agentops dispatch skills + sprintctl-bootstrap-template docs) |

**V3-3 vuoro twin — filed 2026-07-24.** vuoro `.sprintctl` backend config
bootstrapped (repo_id `vuoro`, already registered in remote postgres); vuoro
sprint 429 ("v3 Reservation Model: Vuoro Twin"), track `v3-reservations`,
item #1243. Q5 resolved: **retire** the invocation/v2 transient-credential
carrier — it was purpose-built solely to carry sprintctl `claim_token`
proofs (commit 0e7aa58, #1195 Group A) and has zero consumers now that v3
deletes `claim_token`; a carrier built for an abandoned pattern is not
general-purpose infra worth preserving. vuoro #1243 also covers the
composition/client-discovery refresh for the catalog v2 cutover once #1237
lands; it is sequenced to start after #1237, not concurrently.

**Sequencing:** V3-1 → V3-2 → V3-3 (+vuoro twin) → V3-4 → V3-5 → V3-6 →
V3-7 (#1234) → V3-8 → V3-9. V3-4/5/6 form one release train (V3-4's column
drop breaks heartbeat/handoff code paths); if built as separate PRs, merge
order within the train is 5, 6, then 4's schema commit last. The remaining
retirement-tract items (#1220, #1221, #1164) complete on current semantics
*before* the V3-4 schema train lands, so gate evidence isn't invalidated
mid-collection (#1219 completed 2026-07-24 on current semantics).

Per-item scope, rationale, file areas, and acceptance checks live in the
backlog item descriptions (source of truth); this document holds the model,
sequencing, outcome assessment, and open questions.

## 3. Outcome assessment per item

| Item | Simpler for agent sessions | Risk accepted | Evidence class changes |
| --- | --- | --- | --- |
| V3-1 | One universal concurrency concept (revision) across direct and served paths | CAS granularity is status-only; concurrent description edits remain last-writer-wins | New `concurrency-tested` CAS histories on both backends |
| V3-2 | `item done` needs no token bookkeeping; sub-agents need no coordinator token plumbing | A confused session can complete an item it didn't reserve — mitigated by advisory event + audit trail, accepted by design | `ownership-proof` coordination-failure histories become historical |
| V3-3 | Retry-safe reserve ends the "must not retry unknown outcome" trap in `work.claim.start` | Breaking catalog v2 cutover: one coordinated redeploy of served endpoint + clients | Catalog v1 contract docs superseded; `LEGACY_REMOTE_COMMAND_PARITY` re-baselined |
| V3-4 | No secrets in session state; nothing to store, redact, or lose | Sessions can knowingly override each other — conflict becomes operator-visible instead of enforced | `docs/verification/lease-epoch-schema.md` retired; token-collision/redaction tests deleted |
| V3-5 | Zero periodic ceremony; long tasks can't "lapse" mid-flight | Abandoned reservations linger until sweep/override; stale display is heuristic | `authority-faults.md` partition/expiry counterexample retired as a fault class; half-TTL guidance removed |
| V3-6 | Resume = look up your reservation; handoff = one reassign command | Proof-free reassign can be misdirected (audit event is the control) | `claim-ownership-corrected` event class frozen; recovery-file contract retired |
| V3-7 | Recovery story collapses to "new instance, re-reserve"; doctor shows provenance | One more dual-backend migration on the recovery-critical path (offset by fail-closed drift check) | Per-sprint `recovery.completed` events superseded by the repo record |
| V3-8 | A resuming agent reads one short protocol instead of leases+rotation+adoption+coordinator exceptions | Window where stale summaries of the old model float around; supersedes headers mitigate | `claim-ownership.md` superseded; overlay required-scenarios rebased |
| V3-9 | Fleet-wide: every dispatched agent gets the same 4-verb loop (reserve, work, done, release) | Template consumers with pinned old copies drift until refreshed | Bootstrap-template examples re-recorded against v3 CLI output |

Net deletion estimate: the tract removes token/rotation/recovery machinery
concentrated in `cli.py` (~1,000+ lines), both backends' handoff/proof
functions, and credential plumbing in `contracts.py`/`authority.py`/
`application.py` — against additions of CAS (small), reassign (small), and
one recovery table.

## 4. Open questions for the operator

- **Q1 — Conflict policy at reserve time.** Default refuse-with-`--override`
  (planning recommendation, preserves detection value), or warn-and-create
  allowing overlapping active reservations outright? Affects V3-4 acceptance.
- **Q2 — Claim-type taxonomy.** Keep `inspect/execute/review/coordinate` as
  informational metadata, or collapse to a single reservation kind now that
  coordinator delegation carries no exclusivity exception?
- **Q3 — Activity tracking mechanism.** Explicit `claim touch`, implicit
  bump on any mutating command by the reserving session, or both?
- **Q4 — Stale sweep policy.** Should `maintain check --fix` auto-mark
  long-idle reservations `interrupted` (at what horizon), or is staleness
  display-only with takeover always manual?
- **Q5 — Vuoro transient-credentials carrier.** ~~With the work domain no
  longer consuming invocation/v2 transient proofs, does vuoro retire the
  generic carrier (`vuoro_service/identity.py`, client resolver) or retain
  it for future domains?~~ **Resolved 2026-07-24: retire.** Filed as vuoro
  #1243 (sprint 429); see note above §2.
- **Q6 — Retirement-tract interleaving.** Confirmed by placement: #1220,
  #1221, #1164 complete on current semantics before the V3-4 schema train
  lands (#1238 is dependency-gated on #1164).
- **Q7 — Catalog cutover window.** V3-3 proposes a clean-break catalog v2
  with one coordinated redeploy. Acceptable, or is a brief dual-registration
  window on the served endpoint needed because homelab clients update
  lazily?
