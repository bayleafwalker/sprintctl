# Changelog

## Unreleased

## 0.23.0 — 2026-10-24 (candidate)

Schema 28 is a coordinated owner/runtime cutover. This source candidate adds
durable operator-request packets, revisioned dependants and append-only request
events, including procedure progress, performed reports, human attestation,
decline and supersession. Reads and writes are scoped to the caller's
authenticated workspace and repository. Source verification, offline
reconciliation and continuation delivery remain follow-on work; this candidate
does not claim those flows are qualified.

### Added

- A separate `operator-request/v1` owner record family. It does not change work
  status, effect acceptance or agent inbox records.
- Catalog operations for publishing, reading, revising, listing, linking
  dependants and recording request outcomes, with request operations withheld
  from schema-27 owners.
- Typed Python client transport methods and TypeSpec request models.

## 0.22.0 — 2026-10-20

Same schema 27 as 0.21.0: no owner migration. All existing operation
descriptors remain byte-for-byte unchanged.

### Added

- Exact immutable evidence reads by run and chain sequence, with the full
  repository/principal/workspace/client/grant binding (`work.evidence.entry-v1`).
- Bounded item-linked parking history (`work.lease.parking-context-v1`),
  publishing only digest-validated denial scalars and declared models for the
  cited parked report and successor lease. Private predecessor evidence
  chains remain inaccessible to successors. No model choice or automatic
  release is introduced (agentops#2826, prerequisite for E4 #2472).
- PostgreSQL privacy, nonmutation and optional real Vuoro edge composition
  tests, including older citations and reclaim replay after later appends.

## 0.21.0 — 2026-10-20

Same schema 27 as 0.20.0: no owner migration.

### Fixed

- Served clients complete the Vuoro handshake before invoking an authenticated
  operation; 0.20.0 went straight from catalog to invoke.
- SQLite refused reserve, item-status and sprint mutations keep the caller's
  open transaction; reservation lifecycle, stale sweeps, stale-item blocking,
  carryover and backlog seeding each commit atomically with their events.
- Approval expiry compares UTC instants; maintenance prepare checks expiry
  after taking the writer lock; fresh retirement decisions require a reason.
- Graph-ready dispatch admissibility stays unknown instead of inferred;
  scoped reservation recommendations are executable commands.
- Repository reads are classified for bounded connection recovery.
- Existing served maintenance checks are exposed through the CLI; agent
  protocol status commands are labelled by backend; note results are JSON for
  served coordination.

### Added

- Opt-in local CLI debug summaries.
- Offline inspection of bounded owner-read result packets (new `inspection`
  extra) and of authority producer preflight evidence.
- Effect explanation reports the exact acceptance facts without inventing
  outcome proof.
- Bounded resume briefing and diagnosis of resume context gaps; served
  handoffs are built from one read snapshot.
- Served sprints are classified with scoped state-basis CAS.
- Passive bounded reader-event capture for qualification preparation
  (`scripts/collect_reader_events.py`, source checkout only).

### Changed

- Reservation admission and release, Decision writing, item and sprint status,
  sprint close, ready-item projection, unmet-obligation reporting, Release
  policy and dependency admission guards share one core across the SQLite and
  PostgreSQL drivers.

## 0.20.0 — 2026-10-17

Requires an explicit PostgreSQL owner migration to schema 27. Previous
schema-25 runtimes cannot serve the upgraded store.

### Added

- Run-scoped delegated credentials: owner-held SHA-256 bearer records, a closed
  operation allowlist, bounded expiry and explicit/settlement revocation.
  Delegated reads and writes recheck the live record in the owner transaction.
- Protected exact-CWD `.vuoro/profile.json` discovery lets launched sessions
  use their run credential without receiving the host inbox credential.

- `work.run.resolve-v2`: resolve-v1's binding, under the same binding rule,
  plus `issued_at` (the owner's `run.created_at`), `superseded_by_run_id`
  (a successor: the lowest `run_id` among runs registered with this run as
  `predecessor_run_id`, else null) and `state` (`active` or `superseded`; a run has no recorded close,
  so there is no `closed`). resolve-v1 is unchanged: its catalog descriptor
  is byte-frozen, so the new fields are a new version.
- Structured approvals: `work.approval.record-v1` records an immutable
  approval of a `transition` for a `run_id` until `expires_at` (at most 7
  days), as the authenticated approver, under the new `work:approve`
  authority (minted only by the operator-controlled identity registry; Vuoro
  Cloud has no scope for it); `work.approval.get-v1` (`work:read`) reads one
  by `approval_id` with the owner's `evaluated_at` and `expired`. A principal
  cannot approve its own run (`self-approval-refused`). Approvals are not
  consumed: the consumer must record each use itself. Rationales are
  readable by every `work:read` holder in the workspace. PostgreSQL only;
  standalone SQLite refuses them with `unsupported-backend`.
- PostgreSQL schema 26 (`_apply_schema_version_26`,
  `sprintctl/approval_schema.py`) adds the append-only `work_approval`
  table, makes `run_predecessor` append-only (supersession is permanent; a
  successor run can no longer be deleted) and makes a run's binding columns
  and `created_at` immutable. Successor registration and approval recording
  serialize on a `FOR NO KEY UPDATE` lock of the run. The compatibility handshake publishes `approval_storage` with an
  exact catalog fingerprint. The runtime admits exactly schema 26, so the
  next release is a coordinated cutover.

### Changed

- `work.effect.export-proposal-v3` admits schema 24, 25 or 26.
- Integration-test cleanup (`pg_testing`) and the archival-result closure
  know `work_approval`; an archival closure refuses a run with approvals.

## 0.19.0

### Added

- Agent directory and durable inbox, store side of vuoro contract
  `agent-directory-and-inbox` v1 (with the vuoro#219 `launch-ambiguous`
  amendment). Ten served operations: `work.agent.register-v1`,
  `observe-v1`, `retire-v1`, `list-v1` and `describe-v1`, and
  `work.message.send-v1`, `read-v1`, `status-v1`, `receipt-v1` and
  `cancel-v1`. They run on PostgreSQL only; standalone SQLite refuses them
  with `unsupported-backend`. See `docs/reference/agent-inbox.md`.
- PostgreSQL schema 25 (`_apply_schema_version_25`,
  `sprintctl/agent_inbox_schema.py`) adds instances, declaration revisions,
  observations, the per-repository `inbox_seq` counter row, messages and
  append-only receipts. The compatibility handshake now publishes
  `agent_inbox_storage` with an exact catalog fingerprint. The runtime
  admits exactly schema 25.
- CLI: `sprintctl agent register|observe|retire|list|describe`,
  `sprintctl message send|read|status|receipt|cancel`,
  `sprintctl run context --run-id` (`work.run.predecessor-context-v1`) and
  `sprintctl run note --run-id --note --idempotency-key`
  (`work.session-note.write-v1`). They read and print JSON, need served mode,
  and add the `agent-inbox/v1` capability.
- Test support: `sprintctl.pg_testing.freeze_inbox_clock`,
  `shift_inbox_clock` and `release_inbox_clock` control the owner's clock for
  one session. They take effect only after one of the first two switches on a
  process-local flag, and only on a database commented as disposable. A
  session setting alone is ignored.

### Changed

- `work.effect.export-proposal-v3` now admits schema 24 or 25 instead of
  exactly schema 24.
