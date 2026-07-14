---
doc_id: sprintctl.authority-faults
status: draft
supersedes: null
---

# Authority fault verification boundary

This document defines the depth-2 verification boundary for four authority
faults required by `adr-outbox-sync-model`. It separates retained direct-backend
behavior from the opt-in remote command and decision protocol. The executable
histories are verification evidence, not authorization to change product
semantics.

## Partition and claim expiry

| Field | Contract |
|---|---|
| Subject | One repository-scoped work-item claim set |
| Authoritative state | Claim rows and backend time |
| Operations | acquire, observe expiry, reassign, heartbeat old proof, list active claims |
| Intended safety | A claim is valid only while backed by an unexpired remote grant; reassignment must prevent a partitioned former owner from reviving authority |
| Commit boundary | Backend claim insert or heartbeat update commit |
| Unknown outcome | A response can be lost after either commit |
| Recovery | Observe current remote claim state before retry; expiry is not fencing |

The current SQLite and PostgreSQL implementations filter expired claims during
admission but `heartbeat_claim` validates only the stored token. A bounded
independent-connection history demonstrates this counterexample:

1. owner A acquires an exclusive claim and becomes partitioned;
2. the claim expires according to backend time;
3. owner B acquires a replacement exclusive claim;
4. owner A reconnects and heartbeats the old token;
5. both exclusive claims are now active.

This remains a confirmed application-invariant failure in the retained direct
backend, not a general fencing or linearizability result. The feature-flagged
remote arbiter repairs this bounded history: renewal locks the grant, compares
its basis and proof, checks remote expiry, and durably rejects the stale owner
with `expired-grant`. It does not claim fencing for work performed outside the
claim protocol.

## Stale item and sprint commands

Current direct backend calls re-read authoritative state. A second `done`
transition after item completion and a second close after sprint close raise an
invalid-transition error and do not apply a second mutation. The sprint close
path also retains exactly one close-boundary event.

The remote transport now retains the immutable command request and a
remote-authored `command.rejected` decision when its basis is stale. Identical
retry returns the first decision without a second effect. The retained direct
commands still provide only mutation safety and do not manufacture journal
history.

## Unavailable capability artifacts

A capability-receipt draft pointer is accepted only after the sprint is closed,
one local close boundary exists, and the referenced private artifact can be
read and validated. SQLite and PostgreSQL both reject a missing artifact before
appending the pointer event.

This proves process-local validation and no-event-on-failure. The remote
arbiter now performs the same validation within its effect savepoint and
retains `artifact-unavailable` as a semantic rejection. Artifact transport is
still outside sprintctl: the authority must be able to resolve the canonical
private pointer from its own runtime.

## Duplicate and conflicting proposals

The bounded reference model uses a stable proposal ID plus payload digest:

- an accepted proposal applies one effect;
- retrying the same ID and digest returns the same decision without another
  effect;
- reusing an accepted ID with a different digest yields a durable conflict
  rejection;
- retrying an invalid proposal returns the same rejection.

The model is exhaustively exercised for all length-three histories over two
payload digests, plus repeated invalid input. The authority request identity
and full-record fingerprint now implement that retry/conflict boundary for
shipped sprintctl command types. A broader proposal store remains outside this
tract.

## Evidence classification

| Boundary | Current evidence | Residual strength limit |
|---|---|---|
| Partition / expiry / stale heartbeat | Direct-backend counterexample retained; remote renewal/reassignment history `concurrency-tested` | No fencing or general distributed claim |
| Stale transitions | Direct mutation safety on both backends plus remote request/rejection history `concurrency-tested` | Retained direct commands do not emit journal decisions |
| Missing capability artifact | Direct validation plus remote semantic rejection `example-tested` | Artifact distribution remains external |
| Proposal idempotency | Reference model and shipped command identity/fingerprint path checked within bound | No generic cross-domain proposal store |
