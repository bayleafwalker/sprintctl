---
doc_id: sprintctl.authority-faults
status: draft
supersedes: null
---

# Authority fault verification boundary

This document defines the depth-2 verification boundary for four authority
faults required by `adr-outbox-sync-model`. It separates current direct-backend
behavior from the remote command and decision protocol that is not implemented
yet. The executable histories are verification evidence, not authorization to
change product semantics.

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

This is a confirmed application-invariant failure, not a general fencing or
linearizability result. Repair belongs to a separately authorized item.

## Stale item and sprint commands

Current direct backend calls re-read authoritative state. A second `done`
transition after item completion and a second close after sprint close raise an
invalid-transition error and do not apply a second mutation. The sprint close
path also retains exactly one close-boundary event.

The ADR requires more for the future transport: the immutable command request
and a remote-authored rejection decision must remain visible. No current
command ledger or decision handler exists, so durable stale-command rejection
history remains `unknown` even though direct-backend mutation safety is tested.

## Unavailable capability artifacts

A capability-receipt draft pointer is accepted only after the sprint is closed,
one local close boundary exists, and the referenced private artifact can be
read and validated. SQLite and PostgreSQL both reject a missing artifact before
appending the pointer event.

This proves process-local validation and no-event-on-failure. It does not prove
that a future remote authority can fetch an artifact that exists only on the
producer filesystem. Remote artifact availability and retry semantics remain
`unknown` until the command arbiter defines its artifact transport boundary.

## Duplicate and conflicting proposals

The bounded reference model uses a stable proposal ID plus payload digest:

- an accepted proposal applies one effect;
- retrying the same ID and digest returns the same decision without another
  effect;
- reusing an accepted ID with a different digest yields a durable conflict
  rejection;
- retrying an invalid proposal returns the same rejection.

The model is exhaustively exercised for all length-three histories over two
payload digests, plus repeated invalid input. Sprintctl has no proposal store or
proposal acceptance handler, so there is deliberately no implementation
refinement mapping yet. The future authority implementation must run the same
history oracle against durable backend state.

## Evidence classification

| Boundary | Current evidence | Residual strength limit |
|---|---|---|
| Partition / expiry / stale heartbeat | `concurrency-tested` bounded independent-connection counterexample on SQLite and PostgreSQL | No fencing or general distributed claim |
| Stale direct transitions | `concurrency-tested` bounded stale-reader histories on both backends | Durable request plus rejection is `unknown` |
| Missing capability artifact | `example-tested` on both backends | Remote availability is `unknown` |
| Proposal idempotency | `exhaustively-checked-within-bound` for the reference model | Product refinement is `unknown` |
