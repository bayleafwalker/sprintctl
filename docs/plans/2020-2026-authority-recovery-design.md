---
doc_id: sprintctl-authority-recovery-and-actor-convention
status: proposed-coordinator-decision
items: [2020, 2026]
date: 2026-07-28
---

# Coordinator decisions: terminal-claim recovery and actor convention

This document resolves design work only.  It neither grants a new authority nor
changes a claim implementation.  Both follow-up implementations require the
claim-ownership protocol gate at depth 2 and a fresh review of the released
Vuoro operation/identity contract.

## #2026: privileged terminal-claim recovery

### Decision

Do not add a client-side recovery switch, a local-store fallback, or an
"operator override" to normal `claim release`, `item done-from-claim`, or
claim status commands.  A lost terminal response is an **unknown settlement
outcome**, not evidence that a coordinator may issue a new or changed terminal
mutation.  Exact replay of the original immutable terminal record remains the
normal, retry-safe path and must return its recorded decision.

The future server operation is `work.claim.recover-terminal/v1`.  It is an
explicit, separately authorized recovery operation, not a variant of the
normal claim-arbitration authority.  It may only inspect and resolve a
terminal claim-release or lifecycle-arbitration request identified by its
immutable request/event id; it never accepts a raw claim token as a recovery
credential and it never creates or retries a mutation.

### Required operation contract

Input is:

- `repo_id`, `claim_id`, and the immutable terminal `request_id`;
- `expected_lease_epoch` and the requested terminal disposition;
- a server-validated, scope-bound coordinator recovery capability covering the
  repository, claim, request, disposition, and epoch, with expiry/revocation;
- a coordinator incident reference and a human/operator authorization
  reference, both immutable audit evidence;
- no bearer proof and no client-selected worker actor.

The response is one of `settled`, `not-settled`, `conflict`, or `unavailable`.
`settled` returns the durable terminal event identity and resulting item state;
`not-settled` is the only result that may permit a new, separately authorized
normal operation.  `conflict` includes no secret material and requires human
disposition.  `unavailable` is the stable served-operation-unavailable class
until the operation, identity grant, and deployment evidence exist.

The server looks up the immutable request/decision ledger before inspecting
current claim state.  A matching historical settled record returns `settled`
even if a later handoff, release, or reclamation changed the claim lineage.  An
unrecorded request then atomically compares repository, claim, lease epoch,
request id, and disposition against current state.  It rejects an active or
superseded claim on this unrecorded path, a mismatched epoch, a changed
disposition, an unrecognized request id, or a request that is not terminal.
Recovery is idempotent for the exact tuple and appends at most one
deterministically identified recovery-audit event linked to the validated
capability, incident, and authorization references.  It never mints or
reveals a claim token.

### Authority and rollout constraints

`work:claim-recovery` is distinct from `work:claim` and from ordinary worker
execution authority.  Only a named coordinator recovery identity can receive
it; a worker identity, the general CLI profile, and the hybrid worker profile
must not receive it.  The deployed adapter must reject the operation before
any direct-store construction when absent.  Local/remote recovery documentation
may describe observation and evidence collection, but must not claim a local
equivalent for served recovery.

Implementation acceptance requires table-driven cases for exact replay after a
lost response, duplicate recovery, request-id/disposition mismatch, stale
epoch, wrong repository, expired/revoked or scope-mismatched recovery
capability, active claim, and a historical already-settled command after later
claim-lineage change.  The released-wheel composition gate must exercise both
an accepted coordinator recovery and all rejected classes using the pinned
artifact.

## #2020: coordinator/worker actor convention

### Decision

Every dispatched unit has two non-interchangeable actor values:

- `coordinator_actor`: the authenticated authority principal that owns
  planning, claim lifecycle, recovery escalation, and final settlement; and
- `worker_actor`: an advisory execution identity recorded in session and
  evidence metadata only.  It holds no Sprintctl claim token and cannot
  perform claim lifecycle operations.

For served lifecycle records, `coordinator_actor`, outer invocation actor, and
the authenticated identity must be exactly equal.  The CLI must not silently
substitute either packet value for that identity.  A worker may
be named in evidence, handoff text, ActionQ session metadata, or a completion
result, but it is not a claim owner merely by appearing in those fields.

### Required command and event rules

Normal claim start, heartbeat, handoff, release, and terminal settlement are
performed by `coordinator_actor` and are authorized by the authenticated
principal.  `worker_actor` is optional for a direct coordinator loop and
required for a dispatched loop.  A handoff changes claim ownership only via
the existing proof-bearing handoff operation; changing an evidence field never
changes ownership.

Dispatch records must carry a stable `dispatch_id`, `coordinator_actor`, and,
when applicable, `worker_actor`.  Terminal evidence records include the same
values and the claim id/lease epoch, but never a claim token.  A new worker
attempt gets a new `worker_actor` or attempt id while the coordinator remains
the claimant.  An explicit, audited handoff may name only another coordinator
identity as its recipient; a `worker_actor` or worker-attempt label is never a
handoff target.

### Compatibility and enforcement

Existing actor fields remain accepted as advisory metadata where documented.
The first implementation must add named fields rather than reinterpret old
`actor` payloads.  Readers render both roles when available and label legacy
records as `actor_convention: legacy-ambiguous`; they must not infer a worker
from a model name, process id, or Git author.

Validation requires local, remote, and served fixtures that show: a coordinator
can renew and settle; served authenticated authority rejects a worker-as-
coordinator mismatch; no worker token is minted, persisted, or delegated in
local or remote dispatch evidence; an explicit proof-bearing handoff changes
the authoritative actor only to another coordinator identity; and legacy
records remain readable without being upgraded in place.  ActionQ's receipt
fencing stays an independent queue-authority check and does not become
Sprintctl claim proof.

## Shared non-goals

- No local SQLite or direct PostgreSQL fallback for an unavailable served
  operation.
- No delegation of claim tokens, privileged recovery credentials, or Sprintctl
  lifecycle authority to hybrid workers.
- No automatic repair, token recovery, or new terminal retry based only on a
  timeout or missing client response.
- No deployment or identity grant in this repository; those are separately
  authorized release work.

## Implementation order

1. Freeze request/event identifiers and actor fields in the Sprintctl protocol
   and verification context.
2. Implement and depth-2 test the server-side semantics in Sprintctl.
3. Publish an immutable adapter wheel, pin it in Vuoro, and run the released
   wheel composition compatibility gate.
4. Add the distinct identity grant and deployment evidence outside this
   repository.
5. Only then expose the served CLI path; before that it must retain
   `served-operation-unavailable`.
