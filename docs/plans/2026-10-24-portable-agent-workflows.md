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

## Implementation refinement: handoff evidence and admission (2026-10-30)

The Hanzo/Spool review supplies useful failure cases for SC-PW3/4/5. It does
not select either platform or add a second work authority. The Sprintctl
source checked for this refinement is
`bb6e83790e54a25b2367bff2eacd70cb4e8db611`. The following are implementation
requirements; this document does not assert that an adapter or hosted rollout
already implements them.

### Keep the observed stages separate

Use the existing [inbox receipts](../reference/agent-inbox.md),
[work leases](../protocols/reservation-model.md#the-durable-work-lease-is-a-separate-mechanism)
and [effect-attempt facts](../reference/effect-attempts.md) as distinct records:

- Reading or acknowledging an inbox message establishes receipt. The
  `accepted` receipt selects one handling instance; it does not reserve native
  execution capacity, acquire a work lease, authorize an effect or accept work.
- AgentOps/native adapters check declared capability, accessible source,
  current capacity and applicable execution admission before accepting a
  continuation. A directory entry is a dated observation, not that check.
  Capacity may change afterwards: revalidate at launch and record an explicit
  `not-started` result if admission fails before invocation. Do not imply an
  atomic transaction between the inbox and an external runtime.
- The current `refused` receipt is a message-wide handling decision. A busy
  instance must not use it as a transient capacity observation when another
  instance may handle the message. Defer with bounded backoff, or refuse only
  when recipient policy actually declines that message. A new per-instance
  offer/refusal lifecycle requires a versioned owner contract.
- Only the winning instance starts the successor. The current v1 inbox has no
  automatic acceptance expiry/reoffer transition. A missing heartbeat does
  not permit a consumer to retire an instance, overwrite its acceptance or
  launch a duplicate. A changed acceptance lifecycle needs its own Vuoro
  contract and Sprintctl store change, with replay/race qualification.
- After an uncertain launch, retain `launch-ambiguous` and inspect the native
  attempt before retrying. Lost connectivity is compatible with continued
  execution. `result-linked: completed` remains a reported message outcome;
  work settlement and independent effect verification keep their own rules.

### Renew execution from evidence of progress

Sovite/native execution supervision owns progress-aware renewal. Its evidence
must distinguish fresh progress, a declared bounded tool call, idle capacity,
provider refusal and unknown observation. A live process or connected box is
insufficient. A legitimate long tool call needs an explicit deadline and
cancellation/reconciliation behavior; absence of frequent output alone is not
proof of failure. Sprintctl's durable work-lease clock and advisory reservation
semantics are unchanged by this integration.

Expiry or reassignment does not establish that an old worker stopped, nor
that its external effect did not occur. Preserve the existing intent, target
and attempt lineage. A consumed authorization or replay receipt never grants
another invocation. The effect owner reconciles ambiguous outcomes and the
receiving resource enforces whatever fence/idempotency contract it actually
supports before a successor performs a consequential retry.

### Carry capture evidence without recreating it

SC-PW3 references AgentOps' native capture envelope and semantic session note
through existing supported evidence/ref paths. Sprintctl does not normalize
provider transcripts or copy raw logs into its message bodies. The capture
record names exact immutable source ranges, declared gaps and unknown
coverage; the session note explains attempted work, decisions and next checks.
An unchanged reference or a valid self-hash proves neither complete capture
nor accessible bytes on the successor host.

Restricted raw capture and an export with redactions are different artifacts.
Their digest lineage and the release decision for the exact exported bytes
belong to the capture/execution boundary. A scan result, a `public` label or
possession of a blob reference is not permission to fetch or release the raw
source. A capsule is evidence attached to existing work, never a new Release,
Decision, execution grant or cross-harness resume promise. References to
effect and usage records retain their source classification; missing provider
segments or unknown usage cannot become confirmed non-invocation or zero cost.

### Falsifying cases for the existing work packages

| Case | Required result | Owner/package |
| --- | --- | --- |
| Two instances observe the same message and both have capacity | One v1 acceptor; the loser does not launch. | Inbox store + AgentOps; SC-PW4 |
| Target has no capacity, or loses it between precheck and launch | Bounded deferral or a policy-intended refusal before acceptance; an explicit post-acceptance `not-started` result if capacity vanished; no invented capacity reservation. | AgentOps/Sovite; SC-PW4 |
| Launch response is lost, then the original worker posts progress | Reconcile the existing attempt; no second launch inferred from transport failure. | AgentOps/Sovite; SC-PW4 |
| Old worker reports after takeover, or its external mutation is unresolved | Retain its report with source/attempt identity; owner settlement and target fencing decide separately; no automatic effect replay. | Work/effect owners; SC-PW4 |
| Collector crashes, rotates a log, or retains only an excerpt | Exact retained range and unknown/lost coverage remain visible; neither a clean process exit nor a hash chain fills absent records. | AgentOps capture; SC-PW3 |
| Redacted export has a different digest from restricted raw capture | Authorized reader gets only the approved artifact; raw-reference access and exported-byte integrity are tested separately. | Capture boundary; SC-PW3 |
| Agent reports completion with missing verifier or terminal Decision | Summary shows reported completion and pending/unknown acceptance independently. | Work owner + projection; SC-PW5 |
| Provider changes after a quota event and later usage is unavailable | Both observed runtime segments and the unknown cost denominator remain visible. | Native capture + projection; SC-PW3/5 |

Use the existing continuation qualification parents (`sprintctl#2661`,
`sprintctl#2662`) and EC-05 (`sprintctl#2738`) after checking their current
native scope; these labels do not create new work or close those parents.
Qualification retains failed cases and a declared population. A successor
reads the session note, owner continuation and referenced observations without
opening raw transcripts, and distinguishes known work/effects from unknowns.
Measure extra reads and recovery effort separately from serialization success.
The source review did not establish a reproducible 78-case handoff-loss corpus;
do not turn that discussion figure into the test denominator.

Ship additive read/projection changes behind the existing owner contract and
adapter-version checks. Older capture artifacts retain unknown coverage and
are not rewritten to claim stronger guarantees. If a consumer rollout fails,
disable that consumption path and preserve the recorded evidence; do not
rewrite historical receipts or loosen work/effect admission to restore service.

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
