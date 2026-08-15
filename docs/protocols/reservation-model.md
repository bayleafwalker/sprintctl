---
doc_id: sprintctl.reservation-model
status: draft
supersedes: sprintctl.claim-ownership
---

# Reservation model protocol

This document closes the verification boundary around credential-free
reservation creation, activity tracking, reassignment, release, and
SQLite/PostgreSQL parity. It records intended safety separately from what
current evidence establishes.

This protocol supersedes `sprintctl.claim-ownership`.

## Contract

| Field | Contract |
|---|---|
| Subject | One reservation set for one repository-scoped work item |
| State variables | reservation ID, item ID, role, status, actor, session id, instance id, created at, last activity at, released at, interruption reason |
| Operations | reserve, touch, release, reassign, list, show |
| Reservation precondition | Item exists; no proof is required and no enforced exclusivity check is performed |
| Proof precondition | None. Reservations are advisory coordination signals, not capabilities. |
| Success effect | The backend commit durably creates, updates, reassigns, releases, or removes the reservation |
| Failure effect | Validation failures must not apply the requested reservation mutation; diagnostic events are separate history effects |
| Unknown outcome | A lost response after a commit may leave a created, touched, reassigned, or released reservation even though the caller did not receive success |
| Idempotency | `reservation reserve` is not an idempotent request; retry only after observing current state. `reservation touch` and `reservation release` are idempotent by effect. |
| Recovery | List reservations and reassign or recreate; there is no recoverable credential |
| Projection | Read surfaces derive from backend rows and events and never include secrets |
| Liveness | No automatic progress is promised; activity tracking, operator reassignment, or release enables later progress |

## Linearization candidates

- SQLite reservation creation enters `BEGIN IMMEDIATE` and inserts the
  reservation row. The commit is the durable linearization point; the reserved
  write transaction serializes competing local writers.
- SQLite reassignment and release take effect at their update commit.
- PostgreSQL reservation creation may lock the repository-scoped `work_item`
  row with `SELECT ... FOR UPDATE`, then insert within the same transaction.
  The work-item row lock is an arbitration point for related mutations; the
  transaction commit is the durable linearization point. Because reservations
  are advisory, multiple active reservations on the same item are permitted.
- PostgreSQL reassignment and release take effect at their update/delete
  commit.

The intended invariant is visibility: active reservations are surfaced to
operators and read contracts. It is not at-most-one live owner. Independent
connection histories exercise overlapping reservation creation and observe
that both are accepted and then reported as conflicts. This is
`concurrency-tested` visibility evidence, not a fencing-token or distributed
lease claim.

## Retired proof concepts

The following concepts from `sprintctl.claim-ownership` are retired:

- `claim_token` as a bearer secret
- `lease_epoch` as a future fencing value
- rotate-mode handoff that invalidates a prior token
- `claim recover`, `claim resume`, and local token sidecars
- TTL-as-security and heartbeat contracts
- coordinator-delegation exclusivity exception

Database recovery (`sprintctl db recover-from-remote`) never restores active
reservations: the recovered SQLite carries reservation rows for audit, but
active reservations are closed as `interrupted`. A recovered database is a new
authority instance — pre-recovery reservations do not continue, and work must
be re-reserved. This keeps recovered files free of usable credentials and
prevents split-brain continuity when the source authority is still reachable.

## Backend parity evidence

Backend parity means equivalent accepted/rejected histories and public contract
shapes for the bounded scenarios, not identical SQL. On both backends the
`idx_reservation_active_execute` partial unique index is the arbitration point:
at most one `active` `execute` reservation can exist per work item, and the
database enforces it rather than application code. The surrounding
serialization differs. SQLite opens `BEGIN IMMEDIATE`, taking a
whole-database write lock. PostgreSQL takes a repo-scoped
`pg_advisory_xact_lock` and then `SELECT ... FOR UPDATE` on the item's active
execute rows — the advisory lock exists because maintenance activation gates
on a *count* of active reservations, which no index can enforce. Both durably
record reservation creation, touch, reassignment, and release. The visibility
result is classified as `concurrency-tested`, not as a general cross-operation
linearizability proof.
