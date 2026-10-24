---
doc_id: portable-agent-workflows-sprintctl-v1
status: draft
supersedes: null
---

# Portable backlog and continuation for long-running workers

Status: investigated implementation plan, 2026-10-24. Source surveyed at
`b76bd692060ca9b5f53f78d07ee187918a5d265d` (canonical main).
Cross-repository dependencies, PW package definitions and qualification scenarios
are in the [programme plan](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-10-24-portable-agent-workflows.md).
Documentation landing does not certify implementation or deployment.

## Current boundary and decision

Use the existing served work authority, advisory reservations, run continuation,
checkpoints and owner read snapshots. `docs/reference/context-and-handoff.md`,
`docs/reference/owner-read-snapshots.md`, `docs/reference/vuoro-work-adapter.md`
and `docs/reference/effect-attempts.md` already supply substantial context and
effect-reference machinery. This is a gap inventory and integration plan, not
an instruction to replace them with a new session database.

Sprintctl owns readiness, dependencies, work revisions and visible coordination.
It does not invoke/schedule/retry providers. A reservation has no lease or TTL
and is not proof of permission. Hosted durable work leases and Sovite execution
leases are separate mechanisms with their own owners. Effect redemption does
not prove invocation or accepted output. Existing EC-05 (sprintctl#2738) and
continuation work should absorb these packages after live backlog reconciliation.

## Work packages

- **SC-PW1 / read contract map:** compare PW-01 needs with current context,
  next-work, handoff, predecessor-context and continuation-delta operations.
  Document exact released coverage, scoped visibility and missing fields.
  Served continuation uses constituent supported reads; do not promise the local
  combined resume command in served mode.
- **SC-PW2 / programme references:** shape an additive representation for bounded
  goals, exact governing refs, replanning scope, dependencies and checkpoint
  references using existing owner operations where possible. A goal does not
  confer capability. Long-running workers refresh ready work and dependencies;
  they may shape authorized items but cannot mark blocked work complete.
- **SC-PW3 / portable handoff:** retain repository/item/run identity, source
  revisions, checkpoint digest, candidate refs, unresolved decisions and bounded
  next actions. Files carry working memory, not credentials or work ownership.
  Distinguish successful local serialization from confirmed owner recording.
  Cross-host source/artifact access requires its own authorization.
- **SC-PW4 / execution correlation:** record Sovite/native attempt handles and
  independent evidence without taking over their lifecycle. Reconcile stale
  predecessor and changed work basis before any effect intent. Bind acceptance
  to current owner state and configured verification, not actor names, CWD,
  heartbeat, reservation ID or provider completion.
- **SC-PW5 / operator summary:** expose current ready/active/blocked work,
  overlapping reservations, stale observations, uncertain execution and pending
  verification/landing as distinct facts. Preserve source authority and read
  freshness; usage coverage absent is not zero spend.

SC-PW1 precedes schema changes. SC-PW2/3 pair with Vuoro and agentops' portable
packet fixtures. SC-PW4 consumes a released execution contract; it does not
implement a caller retry loop. Version any additions through normal owner
release and adapter pins.

## Acceptance and implementation gates

Use state-protocol verification for changed continuation, reconciliation,
publication or backend-parity surfaces, with temporary SQLite and disposable
PostgreSQL repository scopes. Verify equivalent local/served meaning without
pretending local-only commands exist remotely. Exercise conflicting advisory
reservations, predecessor forks, superseded work, source digest mismatch,
checkpoint changes between reads, owner-record failure after file creation,
revoked/wrong-tenant access and a late old-worker report after takeover.
Demonstrate an authorized successor reconstructing work on a clean host with
no local token recovery or private state-directory access. Run required tests
before commits; documentation landing does not close implementation items.
