---
doc_id: adr-outbox-sync-model
status: draft
supersedes: adr-001-orchestration-boundary
---

# ADR: Outbox and synchronization model for sprintctl

This is the canonical decision record for how sprintctl records, synchronizes,
and arbitrates state. Other documents link here; they do not duplicate this
protocol text. This ADR formally supersedes
`sprintctl-orchestrator/ADR-001-orchestration-boundary.md`
(`doc_id: adr-001-orchestration-boundary`), which ruled that sprintctl would
never become network-native and never adopt PostgreSQL. That ruling is
contradicted by shipped code — `sprintctl/pg.py`, the remote backend,
`migrate-to-remote`, and a CNPG-backed production database — and ADR-001 has
been marked `superseded` with a forward pointer to this `doc_id`. Its analysis
of orchestration-layer responsibilities remains historically valuable and is
preserved, not deleted.

## Context

sprintctl currently has two split backend modes: a local SQLite backend
(`sprintctl/db.py`) and a remote PostgreSQL backend (`sprintctl/pg.py`),
selected by `SPRINTCTL_BACKEND` and pinned per repo by
`.sprintctl/backend.json` (see
[pg-backend-remote-mode-plan.md](pg-backend-remote-mode-plan.md)). A repo is
either local or remote; there is no offline write path for remote repos and no
synchronization between the two stores.

Capability receipts shipped in sprintctl `main` at commit
`a5aef844a8ea9c9776b31ebd7028c0d161d3d0fa` (2026-07-14): atomic sprint-close
boundary events, receipt pointers, and SQLite/PostgreSQL parity tests.

Two failure modes motivated this decision, both verified in the 2026-07
source-of-truth reconciliation
(`agentops/docs/plans/agentops/ops-upgrade-reconciliation-2026-07.md`, treated
here as established fact):

- **Recorded sprint state has drifted from actual development.** The sprintctl
  scope's active sprint sat two months past its end date while work such as
  capability receipts shipped with no corresponding active-sprint record;
  a second scope held an active sprint with all items done.
- **Tool and version drift has caused capability mismatches.** Stale global
  binaries, missing `[remote]` extras, and source/CLI capability mismatches
  recurred; the reconciliation classifies this as an installation-provenance
  and capability-negotiation problem, not proof that all clients must be
  replaced by HTTP.

The all-or-nothing backend split compounds both: a workstation without remote
credentials cannot record anything, and there is no mechanism that carries
observations made anywhere into one arbitrated record.

## Decision: producer outbox + synchronization model

The target is no longer separate "local" and "remote" sprintctl backends.
There is **one producer-side write mechanism**:

> Append to a durable local producer outbox.

- **Remote synchronization is optional for observational events but required
  for authority-changing operations.** Observations may accumulate offline and
  synchronize later; authority-changing operations require remote arbitration
  before they take effect.
- **Local reads come from a cached projection with a visible remote
  watermark.** Readers always know how stale their view is.

Two scope boundaries are part of the decision itself:

- This is **not** a generic multi-master database. There is no symmetric
  replication and no conflict merge of authoritative state.
- This is **not** a mandate to rebuild the ecosystem around one event-sourced
  schema. Existing domain stores remain; the outbox is the transport and
  provenance layer between producers and their domain authority.

## Observation / authority-command / remote-decision split

Every record belongs to exactly one of three semantic classes.

### Observations

Facts about what happened. Examples:

- notes and decisions;
- commits and touched paths;
- test and verification results;
- work-progress facts;
- audit events;
- session lifecycle exhaust;
- generated evidence and artifact pointers that do not themselves authorize a
  transition.

Rules:

- may be appended offline;
- remain true even when the referenced aggregate has advanced remotely;
- synchronize by union and deduplication;
- may be classified as concurrent or anachronistic relative to the aggregate;
- **must never be silently discarded because their basis revision is stale.**

### Authority commands

Requests to change shared authoritative state. Examples:

- actionq queue claim or lease renewal;
- sprintctl exclusive claim acquire, renew, handoff, or release;
- item status transitions such as `done`;
- sprint activation or close;
- takeup operations where ownership semantics apply;
- acceptance of validation-bearing capability-receipt pointers;
- any transition whose validity depends on current shared state, claim proof,
  evidence gates, or canonical artifact availability.

Rules:

- authority commands **require remote arbitration**. A local producer may
  record a `command.requested` intent, but it must not project the requested
  transition as effective until the remote authority emits an accepted
  decision;
- **offline claim acquisition is not supported by default.** Do not design
  optimistic offline exclusive claims;
- the critical distinction: **`work.completed` is a bufferable observation**
  ("the work is done" is a fact the producer witnessed); **`item.done` is an
  authority command** (the shared item transition needs arbitration against
  current state).

### Remote decisions

Outcomes authored only by the remote domain authority. Examples:

- command accepted or rejected;
- claim granted, renewed, expired, or denied;
- authoritative item or sprint transition;
- validation-bearing pointer accepted;
- lease timeout or conflict determination.

Rules:

- only the remote domain authority authors these events;
- **a stale or invalid command remains visible as an immutable request plus a
  rejection decision. It never mutates authoritative state.** The request and
  its rejection are durable history, not errors to be dropped.

## Identity and cursor model

### Repository identity

- `repo_id` is a **minted UUID committed in the canonical repo manifest**.
- The Git root commit is **not** the repo identity: it cannot distinguish
  forks (which share the root commit) and may be unavailable in shallow
  clones.
- A repository without a committed identity remains **unregistered /
  local-only** until it is explicitly initialized or adopted. No fake identity
  is minted implicitly.

### Aggregate identity

- Sprints, work items, and other portable aggregates get **stable UUIDs**.
- Integer IDs may remain internal database keys during migration; they are not
  portable identity.

### Stream and record fields

Every outbox record carries:

| Field | Meaning |
|-------|---------|
| `origin_stream_id` | Durable UUID for one producer-owned outbox stream. |
| `origin_seq` | Monotonically increasing sequence, allocated atomically within that stream. |
| `runtime_session_id` | Semantic session identity, distinct from transport identity. |
| `event_id` | Globally unique record identity. |
| schema version | Version of the record's payload contract. |
| actor | Who authored the record. |
| timestamps | When the record was authored. |
| refs / payload | Referenced aggregates and record body. |
| `basis_revision` | Aggregate revision visible when the record was authored. |
| correlation / causation IDs | Links across requests, decisions, and derived records. |
| optional digests | Payload or artifact digest for integrity/pointer validation. |

## Remote ingestion semantics

The remote side must provide:

- **uniqueness on `(origin_stream_id, origin_seq)`** — one logical record is
  ingested at most once;
- **idempotent retry** — re-uploading a batch after a lost response is safe;
- **explicit gap detection** — a missing sequence number is surfaced, never
  silently accepted;
- **a server-assigned monotonically increasing `ingest_offset`** — the remote
  ordering cursor that consumers and watermarks are keyed on;
- **at-least-once delivery with idempotent consumers**;
- **no claim of global causal ordering** — `ingest_offset` orders ingestion,
  not causality across streams.

A local producer **never writes remote-origin events into its own outbox**.
The outbox contains only records that producer authored.

## Local read side

The local read side consists of exactly three parts:

1. **producer-authored outbox records** — this producer's own pending and
   synchronized appends;
2. **a cached projection** — local materialization of remote-authoritative
   state;
3. **a watermark** — the highest fully applied remote `ingest_offset`.

Invariants:

- projection changes and watermark advancement **commit atomically**; the
  watermark never claims events that are not fully applied;
- **offline reads expose their staleness honestly** — every read surface shows
  the watermark (and its age) rather than presenting a stale projection as
  current.

## Domain authority preservation

The target is one integration protocol and operator surface, not one
undifferentiated database. Domain authority is retained as-is:

| Domain | Authority |
|--------|-----------|
| sprintctl | Sprint execution memory and work claims. |
| actionq | Queue and dispatcher leases. |
| auditctl | Durable local capture / outbox. |
| kctl | Knowledge review lifecycle. |
| agentops | Cross-domain projection, gateway, and operator UX. |

If deployment consolidation is ever considered later, prefer a **modular
service with separate schemas, separate roles, and domain-owned command
handlers**. A one-schema rewrite is **not authorized** by this decision.

The existing export envelopes can backfill snapshots and known events into the
new model. They **cannot manufacture an authoritative historical event stream
or causal ordering**. Synthetic imports must remain permanently labelled as
imported snapshots/history, distinguishable from genuinely produced events.

## Failure cases and invariants

The verification model covers these failure scenarios explicitly. Each must
have a defined outcome in implementation and, where implementation is
deferred, a formal-model or verification-context backlog item.

1. Crash before and after local outbox fsync.
2. Producer restart with pending (unsynchronized) records.
3. Duplicate batch upload.
4. Lost server response after an accepted ingest.
5. Missing origin sequence (gap in a producer stream).
6. Cursor application crash (mid projection apply).
7. Remote state advancing while local is offline.
8. Claim expiration during a partition.
9. Stale item-completion or sprint-close command.
10. Artifact pointer unavailable remotely.
11. Session start without a clean end.
12. Duplicate or conflicting scribe proposals.
13. Projection schema upgrade and rebuild.
14. Remote log retention or snapshot recovery.

Required invariants:

1. No locally committed outbox record is lost across producer restart.
2. One `(origin_stream_id, origin_seq)` is ingested at most once.
3. Sequence gaps are detected.
4. The watermark never advances beyond unapplied remote events.
5. Authority state changes only from remote-authored decisions.
6. Stale commands produce rejection, not mutation.
7. Claims are shown as valid only while backed by an unexpired remote grant.
8. Accepted/rejected proposal processing is idempotent.
9. Shadow projections remain equivalent to current authoritative state during
   migration.

## Migration and rollback

Migration is phased, reversible, and independently valuable at each phase.
Every phase states its rollback; no phase requires the next to be worthwhile.

### P1 — transport foundations

- **Committed repository identity and manifest consolidation.** The minted
  `repo_id` lands in the existing `sprintctl.dispatch.json` manifest format
  (per-repo dispatch manifests already exist). No second manifest format is
  introduced without an explicit, documented consolidation decision.
  *Rollback:* remove the identity field; unregistered repos stay local-only.
- **Stable aggregate UUIDs**, alongside existing integer keys.
  *Rollback:* UUIDs are additive columns; drop them.
- **Producer outbox schema and crash-safe sequence allocation.**
  *Rollback:* the outbox is additive; existing backends keep functioning.
- **Observation/command/decision contracts** in `contracts.py`.
  *Rollback:* contracts are additive types.
- **Remote deduplication, gap handling, and ingest cursor.**
  *Rollback:* ingestion tables are additive; disable the sync endpoint.
- **Cached local projection and transactional watermark.**
  *Rollback:* reads fall back to the current backend path.
- **Formal protocol model and stateful/fault-injection verification
  contexts** for the failure list above.
  *Rollback:* not applicable (verification assets only).

### P3 — migration and removal

- **Dual-record / shadow-projection pilot** — write through the outbox while
  the current backend remains authoritative.
  *Rollback:* stop shadow writes; authoritative path is untouched.
- **Projection parity checks against the current SQLite and PostgreSQL
  implementations** (invariant 9). *Rollback:* checks are read-only.
- **sprintctl dogfooding pilot** on the sprintctl repo scope.
  *Rollback:* revert the pilot repo's flag to the current backend.
- **Per-repo feature-flagged cutover.**
  *Rollback:* per-repo flag revert; no fleet-wide switch exists.
- **Direct agentops SQL write removal** (the cockpit sprint-activation
  transaction moves behind a sprintctl-owned command handler).
  *Rollback:* the mediated write path is restorable from history until removal
  is proven safe.
- **Backend-mode code removal only after evidence** — parity checks green,
  dogfooding stable, cutover complete. *Rollback before this point:* the
  backend-mode code is still present; after it, restoration is a revert of the
  removal commit plus re-migration from retained export artifacts.

## Explicit non-goals

This decision does not propose or imply:

- one universal event-sourced Postgres schema;
- transparent optimistic offline exclusive claims;
- last-write-wins state transitions;
- remote events copied into producer outboxes;
- autonomous scribe mutation of sprint state;
- mandatory backlog creation for every session;
- raw prompt/transcript persistence by default;
- immediate retirement of all existing stores;
- rebuilding the already implemented workspace backup;
- treating imported snapshots as genuine historical events;
- a second manifest format without consolidating the existing dispatch
  manifest;
- fake repository identity for workspace-level orchestration.

## Related documents

- `agentops/docs/plans/agentops/session-mechanization-plan.md` — session
  bookkeeping (Tier 0/1/2 exhaust, capsule, reconciler, periodic scribe);
  being authored in parallel with this ADR.
- `agentops/docs/plans/agentops/state-event-command-matrix.md` — per-event
  ownership, offline eligibility, validation, and projection behaviour.
- `agentops/docs/plans/agentops/ops-upgrade-reconciliation-2026-07.md` — the
  verified source-of-truth reconciliation this ADR's context relies on.
- [pg-backend-remote-mode-plan.md](pg-backend-remote-mode-plan.md) —
  predecessor plan that built the current split-backend remote mode; subsumed
  by this decision.
- `sprintctl-orchestrator/ADR-001-orchestration-boundary.md` — superseded by
  this ADR (see header).
