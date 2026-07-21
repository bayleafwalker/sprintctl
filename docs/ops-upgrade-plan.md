# Task: Reconcile agent-ops documentation and build the implementation backlog

> **Ratified target update (2026-07-21):** installation provenance remains a
> useful diagnostic, but the `pilot cutover-evidence` rollout demonstrated the
> deeper problem: shared substrate capability is distributed through
> machine-local packages. The target is now the served operation catalog and
> deployment-owned schema contract in
> [`docs/plans/vuoro-served-authority-alignment.md`](plans/vuoro-served-authority-alignment.md).
> This task remains historical input for the outbox and session-mechanization
> work; its caution against assuming an HTTP service is superseded for shared
> authority delivery.

You are a documentation and backlog reconciliation agent operating across the `bayleafwalker` agent-ops ecosystem.

Your job is to turn the ratified product direction below into:

1. canonical, non-contradictory architecture and product documentation;
2. a phased, owner-correct implementation backlog in sprintctl;
3. explicit links between backlog items and their governing documents.

Do not implement production code, database migrations, Kubernetes changes, or runtime behaviour in this task. Documentation edits and sprintctl backlog mutations are in scope. Do not delete live database scopes or alter active production state without separate authorization.

## Repositories in scope

Inspect current HEAD and local instructions in at least:

- `sprintctl`
- `agentops`
- `actionq`
- `actionq-dispatch`
- `auditctl`
- `kctl`
- `appservice`
- `sprintctl-orchestrator`
- `homelab-analytics` as the mature consumer/pilot

Follow each repository’s `AGENTS.md`, validation commands, documentation conventions, and ownership boundaries.

The governing ownership rule remains:

> State ownership decides repository ownership.

Cross-repository architecture and operator projections belong in `agentops`. Sprint execution semantics belong in `sprintctl`. Queue and dispatch leases belong in `actionq`. Local audit capture belongs in `auditctl`. Deployment truth belongs in `appservice`.

## First: establish current truth

Do not trust the existing plan documents without checking them against current code, Git history, manifests, and sprint state.

Verify and reconcile at least these known findings:

- The capability-receipts implementation has landed in sprintctl `main` in commit `a5aef844a8ea9c9776b31ebd7028c0d161d3d0fa`. Treat it as shipped, not pending.
- `sprintctl/tests/test_pg_integration.py` accepts an arbitrary `SPRINTCTL_TEST_PG_URL`, creates an `itest-<uuid>` repo scope, and depends on normal fixture teardown for cleanup. This allowed test data to remain in production PostgreSQL.
- The workspace backup gap described by `agentops/docs/plans/agentops/substrate-resilience-plan.md` is stale. `appservice/clusters/main/kubernetes/apps/vscode/app/workspace-backup.yaml` contains a daily Restic backup and monthly restore drill. Distinguish:
  - implemented in GitOps;
  - deployed/runtime state;
  - last successful backup;
  - last successful restore drill.
  Do not create a backlog item to build a backup that already exists. Create verification, observability, documentation, or semantic-restore work only where evidence shows it is needed.
- The cockpit is not strictly read-only and does not have exactly one write route. `agentops/docs/plans/agentops/write-surface-policy.md` permits mediated writes, and `agentops/apps/web/lib/cockpit/sprintctl.js` directly implements a sprint activation transaction in JavaScript. Document this as an existing boundary exception and plan its removal or formal ownership.
- `sprintctl-orchestrator/ADR-001-orchestration-boundary.md` is obsolete because sprintctl now has remote PostgreSQL coordination. It must be formally superseded, not silently left contradictory.
- sprintctl’s recorded active sprint has historically drifted from actual development activity. Verify current state before creating items. Do not blindly add work to a stale active sprint.
- Tool installation/version drift has repeatedly produced stale binaries, missing `[remote]` extras, and source/CLI capability mismatches. Treat this initially as an installation provenance and capability-negotiation problem, not proof that all clients must be replaced by HTTP.
- Existing dispatch manifests and shared-skill plans already cover part of the proposed `agentops.toml` role. Do not create a second overlapping configuration format without a documented migration and consolidation decision.

Produce a short source-of-truth reconciliation before editing. Clearly label:

- verified current fact;
- GitOps intent but runtime unverified;
- stale documentation;
- unresolved question.

## Ratified product direction

### 1. Replace backend modes with an outbox and synchronization model

The target is no longer separate “local” and “remote” sprintctl backends.

There is one producer-side write mechanism:

> Append to a durable local producer outbox.

Remote synchronization is optional for observational events but required for authority-changing operations. Local reads come from a cached projection with a visible remote watermark.

This is not a generic multi-master database and not a mandate to rebuild the ecosystem around one event-sourced schema.

### 2. Separate observations from authority commands

Document three semantic classes:

#### Observations

Examples:

- notes and decisions;
- commits and touched paths;
- test and verification results;
- work-progress facts;
- audit events;
- session lifecycle exhaust;
- generated evidence and artifact pointers that do not themselves authorize a transition.

Observations:

- may be appended offline;
- remain true even when the referenced aggregate has advanced remotely;
- synchronize by union and deduplication;
- may be classified as concurrent or anachronistic;
- must not be silently discarded because their basis revision is stale.

#### Authority commands

Examples:

- actionq queue claim or lease renewal;
- sprintctl exclusive claim acquire, renew, handoff, or release;
- item status transitions such as `done`;
- sprint activation or close;
- takeup operations where ownership semantics apply;
- acceptance of validation-bearing capability-receipt pointers;
- any transition whose validity depends on current shared state, claim proof, evidence gates, or canonical artifact availability.

Authority commands require remote arbitration. A local producer may record a `command.requested` intent, but it must not project the requested transition as effective until the remote authority emits an accepted decision.

Offline claim acquisition is not supported by default. Do not design optimistic offline exclusive claims.

Distinguish explicitly:

- `work.completed` as a bufferable observation;
- `item.done` as an authority command.

#### Remote decisions

Examples:

- command accepted or rejected;
- claim granted, renewed, expired, or denied;
- authoritative item or sprint transition;
- validation-bearing pointer accepted;
- lease timeout or conflict determination.

Only the remote domain authority authors these events.

A stale or invalid command remains visible as an immutable request plus rejection. It does not mutate authoritative state.

### 3. Event, identity, and cursor model

Specify and backlog a model containing:

- `repo_id`: minted UUID committed in the canonical repo manifest;
- stable UUIDs for sprints, work items, and other portable aggregates;
- integer IDs may remain internal database keys during migration;
- `origin_stream_id`: durable UUID for one producer-owned outbox stream;
- `origin_seq`: monotonically increasing sequence allocated atomically within that stream;
- `runtime_session_id`: semantic session identity, distinct from transport identity;
- `event_id`, schema version, actor, timestamps, refs and payload;
- `basis_revision`: aggregate revision visible when the record was authored;
- correlation and causation identifiers;
- optional payload or artifact digest.

Remote ingestion must provide:

- uniqueness on `(origin_stream_id, origin_seq)`;
- idempotent retry;
- explicit gap detection rather than silently accepting missing sequence numbers;
- a server-assigned monotonically increasing `ingest_offset`;
- at-least-once delivery with idempotent consumers;
- no claim of global causal ordering.

A local producer must never write remote-origin events into its outbox.

The local read side consists of:

- producer-authored outbox records;
- a cached projection;
- a watermark identifying the highest fully applied remote `ingest_offset`.

Projection changes and watermark advancement must commit atomically. Offline reads must expose their staleness honestly.

Do not use the Git root commit as the normal repo identity. It cannot distinguish forks and may be unavailable in shallow clones. A repository without a committed identity remains unregistered/local-only until explicitly initialized or adopted.

### 4. Preserve domain authorities

The target is one integration protocol and operator surface, not one undifferentiated database.

Retain:

- sprintctl authority over sprint execution memory and work claims;
- actionq authority over queue and dispatcher leases;
- auditctl as durable local capture/outbox;
- kctl authority over knowledge review lifecycle;
- agentops ownership of cross-domain projection, gateway and operator UX.

If later deployment consolidation is considered, prefer a modular service with separate schemas, roles and domain-owned command handlers. Do not authorize a one-schema rewrite in this documentation pass.

The existing export envelopes can backfill snapshots and known events. They cannot manufacture an authoritative historical event stream or causal ordering. Synthetic imports must remain labelled as imported snapshots/history.

### 5. Mechanize session bookkeeping

The product direction is:

> Move recording from instruction to mechanism wherever possible. Move genuine judgment to deterministic trigger points where it does not compete with the primary task.

Document and backlog the following layers.

#### Tier 0 — mechanical session exhaust

Create a harness-neutral session wrapper as the primary lifecycle mechanism. Harness-specific hooks may supplement it.

Mechanically record:

- `session.started`;
- repo, harness/model, runtime session and origin-stream identities;
- initial prompt digest, but not raw prompt content by default;
- explicit work-item or claim reference;
- starting remote watermark;
- base/head commits and commit list;
- dirty-state or patch digest;
- diff statistics and touched paths;
- observable verification commands/results;
- `session.ended` or `session.end-inferred`;
- exit reason and timestamps.

Raw prompts and transcripts must be opt-in private artifacts with explicit retention. A prompt digest is correlation evidence, not enough semantic evidence for reconciliation.

Hooks and wrappers for manual work should generally fail open. Claim acquisition and dispatcher verification gates fail closed.

Provide crash recovery for started sessions lacking a clean end event.

#### Tier 1 — deterministic read-side context injection

At session start, inject a small ranked context packet containing potentially relevant sprint items.

Ranking should prefer:

1. explicit item or sprint reference;
2. exact path or manifest scope overlap;
3. linked documentation and ownership boundaries;
4. deterministic lexical/semantic candidate matching;
5. repo-level candidates.

Include the cached projection watermark and age.

Only an explicit target may cause automatic claim acquisition before the harness begins. Inferred candidates are advisory context, not automatic claims.

Bound the injected packet to a few useful candidates. Do not paste the whole backlog into the prompt.

#### Tier 2 — post-session reconciliation

Do not depend on the exhausted primary agent to perform bookkeeping correctly.

The session-end mechanism should create a `session-capsule/v1` artifact and enqueue reconciliation. A fresh reconciler receives:

- session capsule;
- commit and diff evidence;
- verification evidence;
- current sprint projection;
- candidate work items;
- linked plan documents and done criteria;
- claims held during the session.

It may propose:

- link session to existing item;
- mark an item advanced;
- propose completion;
- identify conflict or duplicate work;
- propose a new item;
- classify the work as incidental/no backlog change.

Every code-bearing session should eventually receive a reconciliation outcome, but not every session should create backlog activity.

#### Canonical periodic scribe

The periodic scribe is the correctness path. Immediate per-session reconciliation is only a latency optimization.

The scribe:

- consumes unreconciled session exhaust up to a durable cursor;
- creates reviewable `reconciliation-proposal/v1` artifacts;
- never directly mutates authoritative sprint state;
- groups related sessions where useful;
- records explicit no-change outcomes;
- tolerates delayed execution without losing evidence.

Each proposal must include:

- stable proposal and source-session IDs;
- evidence refs;
- observed and current aggregate revisions;
- proposed commands;
- confidence and uncertainty;
- deduplication key;
- lifecycle: `pending`, `accepted`, `rejected`, `superseded`.

Accepted proposals execute through normal sprintctl authority commands. Rejections are durable so the scribe does not repeatedly rediscover the same proposal.

### 6. Dogfooding is an explicit product metric

The architecture must measurably reduce silent sprint drift.

Document and backlog metrics for:

- percentage of manual sessions with Tier-0 traces;
- percentage of code-bearing sessions linked explicitly at start;
- unreconciled session count;
- median and p95 reconciliation age;
- commits older than a threshold without a session/item link;
- accepted, rejected and no-change proposal rates;
- duplicate-work incidents despite claims;
- local projection watermark age;
- stale tool/version incidents;
- human review effort.

The target failure mode is bounded, visible reconciliation lag—not months of silent divergence.

## Required documentation outputs

Create or update documents according to repository conventions. At minimum, ensure the ecosystem has:

1. A canonical sprintctl decision/ADR covering:
   - outbox model;
   - observation/command/decision split;
   - identities and stable aggregate IDs;
   - remote ingestion and cursor semantics;
   - claim and transition authority;
   - failure cases;
   - migration and rollback;
   - explicit non-goals.

2. A cross-repository agentops plan covering:
   - session wrapper and Tier-0 exhaust;
   - context-pack injection;
   - session capsule;
   - reconciler and periodic scribe;
   - proposal lifecycle;
   - cockpit surfaces and metrics.

3. A state/event/command matrix assigning:
   - event or command type;
   - owning repository/domain;
   - offline eligibility;
   - remote validation;
   - local projection behaviour;
   - authoritative result event.

4. Updated ecosystem and resilience documentation removing known contradictions.

5. Formal supersession of sprintctl-orchestrator ADR-001, with a link to the current canonical decision. Preserve history; do not delete the ADR.

6. Documentation of the current agentops direct-write exception and the intended migration to a domain-owned handler.

7. A migration plan that is reversible and independently valuable at each phase.

Use status/supersession/provenance frontmatter where repository conventions support it. There should be one canonical document for each decision, with other documents linking rather than duplicating protocol text.

## Backlog construction

Before adding items:

- inspect all active and planned sprints across relevant repo scopes;
- search for overlapping existing items;
- inspect existing doc refs and implementation plans;
- reconcile or annotate stale items rather than duplicating them;
- do not activate a new sprint automatically;
- do not silently close an item based only on code or documentation inference.

Every created or materially updated item must contain:

- owning repo and track;
- concise objective;
- why the work exists;
- exact scope and non-scope;
- likely files/components;
- implementation outline;
- dependencies;
- verification and done criteria;
- rollback or compatibility plan;
- governing document reference.

Descriptions must not be empty. Add machine-readable doc refs through sprintctl rather than relying only on notes containing paths.

### Suggested priority structure

Validate against existing work before creating anything.

#### P0 — truth and safety

- isolate PostgreSQL integration tests from production using a dedicated test database/role, server-side guard and ephemeral CI database;
- plan safe cleanup of leaked `itest-*` scope without performing it;
- verify deployed workspace backup and restore drill, and add semantic validation/observability if currently only file-presence is checked;
- reconcile stale resilience, ecosystem and write-surface documentation;
- supersede ADR-001;
- address tool installation provenance and add a version/capability doctor;
- reconcile sprintctl’s own stale sprint state and add drift detection.

#### P1 — transport foundations

- committed repository identity and manifest consolidation;
- stable aggregate UUIDs;
- producer outbox schema and crash-safe sequence allocation;
- observation/command/decision contracts;
- remote deduplication, gap handling and ingest cursor;
- cached local projection and transactional watermark;
- formal protocol model and stateful/fault-injection verification contexts.

#### P2 — session capture and reconciliation

- harness-neutral session wrapper;
- Tier-0 hook/capsule schema;
- stale-session end inference;
- deterministic overlap/context packet;
- fresh post-session reconciler;
- periodic scribe and proposal schema;
- review and reconciliation-lag cockpit surfaces.

#### P3 — migration and removal

- dual-record/shadow-projection pilot;
- projection parity checks against current SQLite and PostgreSQL implementations;
- sprintctl dogfooding pilot;
- per-repo feature-flagged cutover;
- direct agentops SQL write removal;
- backend-mode code removal only after evidence;
- archive sprintctl-orchestrator after supersession is visible.

Assign each item to the repository that owns the affected state or contract. Keep cross-repo sequencing in agentops rather than hiding all work in one meta-repository.

## Verification model

The documentation and backlog must explicitly cover:

- crash before and after local outbox fsync;
- producer restart with pending records;
- duplicate batch upload;
- lost server response after accepted ingest;
- missing origin sequence;
- cursor application crash;
- remote state advancing while local is offline;
- claim expiration during a partition;
- stale item-completion or sprint-close command;
- artifact pointer unavailable remotely;
- session start without clean end;
- duplicate or conflicting scribe proposals;
- projection schema upgrade and rebuild;
- remote log retention or snapshot recovery.

Required invariants include:

- no locally committed outbox record is lost across producer restart;
- one `(origin_stream_id, origin_seq)` is ingested at most once;
- sequence gaps are detected;
- watermark never advances beyond unapplied remote events;
- authority state changes only from remote-authored decisions;
- stale commands produce rejection, not mutation;
- claims are shown as valid only while backed by an unexpired remote grant;
- accepted/rejected proposal processing is idempotent;
- shadow projections remain equivalent to current authoritative state during migration.

Create formal-model or verification-context backlog items where implementation is not part of this pass.

## Non-goals

Do not propose or imply:

- one universal event-sourced Postgres schema;
- transparent optimistic offline exclusive claims;
- last-write-wins state transitions;
- remote events copied into producer outboxes;
- autonomous scribe mutation of sprint state;
- mandatory backlog creation for every session;
- raw prompt/transcript persistence by default;
- immediate retirement of all existing stores;
- rebuilding an already implemented workspace backup;
- treating imported snapshots as genuine historical events;
- a second manifest format without consolidating the existing dispatch manifest;
- fake repository identity for workspace-level orchestration.

## Completion requirements

Before finishing:

1. Run repository documentation and formatting validation.
2. Run `git diff --check` in every modified repository.
3. Ensure no implementation code or production manifests changed.
4. Ensure each backlog item has a description, owner, dependency information, done criteria and doc ref.
5. Produce a final report containing:
   - verified current-state corrections;
   - documents created and updated;
   - obsolete documents superseded;
   - backlog items created or updated, with IDs and owning repos;
   - dependencies and proposed execution order;
   - unresolved questions;
   - commands or runtime checks still requiring an authorized operator.
6. State explicitly whether live backup/restore and cockpit/daemon state were actually verified or only inferred from GitOps.

Do the reconciliation and create the documentation/backlog. Do not merely restate this prompt as another assessment.
