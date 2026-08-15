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
| Conflict report | `reserve` returns `conflict`, `conflicting_reservations`, and `conflict_severity` (`warning` for execution-beside-execution, otherwise `informational`) |
| Operations | reserve, touch, release, reassign, list, show |
| Roles | `execution`, `verification`, `observation` — the relationship to the work, which is what makes an overlap classifiable |
| Reservation precondition | Item exists; no proof is required and no exclusivity is enforced. Overlap is reported, never refused |
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
- PostgreSQL reservation creation takes a repo-scoped
  `pg_advisory_xact_lock`, reads the item's active reservations
  `FOR UPDATE`, and inserts within the same transaction. The transaction
  commit is the durable linearization point. Because reservations are
  advisory, multiple active reservations on the same item are permitted.
- PostgreSQL reassignment and release take effect at their update/delete
  commit.

The intended invariant is visibility: active reservations are surfaced to
operators and read contracts. It is not at-most-one live owner. Independent
connection histories exercise overlapping reservation creation and observe
that both are accepted and then reported as conflicts. This is
`concurrency-tested` visibility evidence, not a fencing-token or distributed
lease claim.

## Conflict policy

A reservation is a detector, not a lease. `reserve` therefore always records
the reservation and reports the overlap it found:

```text
reservation reserve
  → succeeds
  → conflict=true / conflicting_reservations=[...]
  → both reservations remain active and visible
```

Refusing the second actor would not stop that actor working. It would only
stop the work being recorded, either turning the ledger into de-facto locking
or pushing the second session into working unobserved — the worst outcome for
a coordination ledger.

Displacing another session is a separate, deliberate act:

```text
reservation reserve --interrupt-existing
  → interrupts the item's active execution reservations
  → records `interrupted by <actor> (<session>)` and a durable audit event
  → creates the new reservation
```

The flag is scoped to `execution` reservations: a takeover replaces the party
claiming to be doing the work, not everybody else's coordination signals. It
is deliberately not named `--override`, which suggests bypassing an
authorization check — precisely the concept v3 deletes.

## Activity

`last_activity_at` is an operational heuristic, not a heartbeat and not proof
of ownership. Nothing lapses, and no reservation ever changes state because
time passed.

- It advances **implicitly** on a successful item-scoped mutation attributed
  to the reservation's *session* — status, edit, note, ref, and dep
  operations. Attribution is by session id, never by a matching actor name,
  and reads never qualify.
- `reservation touch` remains available for work happening outside sprintctl
  (long external or git-only work).

## Staleness is policy, not model

The ledger stores facts; what an age *means* is operator policy in
`sprintctl/reservation_policy.py`:

| Horizon | Default | Effect | Override |
|---|---|---|---|
| `stale_after` | 4 hours | Read surfaces mark an active reservation `stale`. Display only. | `SPRINTCTL_RESERVATION_STALE_AFTER_HOURS` |
| `interrupt_after` | 7 days | An **explicitly invoked** `maintain sweep` may interrupt reservations idle for longer. | `SPRINTCTL_RESERVATION_INTERRUPT_AFTER_DAYS` |

The seven-day horizon means "a sweep an operator runs may interrupt
reservations older than this", not "something expires in the background after
seven days".

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
shapes for the bounded scenarios, not identical SQL. Neither backend arbitrates
who may reserve: the `idx_reservation_active_execute` partial unique index that
once made exclusivity a database law was removed in SQLite schema 22 and
PostgreSQL schema 12, because a constraint that refuses registration cannot
prevent work — only its record. Independent connections on both backends
create overlapping execution reservations, all of which commit and are then
reported as conflicts.

The serialization that remains is narrower and exists for a different reason.
SQLite opens `BEGIN IMMEDIATE`, taking a whole-database write lock; PostgreSQL
takes a repo-scoped `pg_advisory_xact_lock`. Both do so because maintenance
activation gates on a *count* of active reservations, which no index can
enforce: without that lock, an activation counting zero and a concurrent
`reserve` could both commit. An active exact-plan maintenance capability is
consequently the only condition under which `reserve` still refuses, and it is
a property of the repository rather than of who else is working on the item.

Both backends durably record reservation creation, touch, reassignment, and
release, and share one role taxonomy and one policy module, so the facades
cannot drift. The visibility result is classified as `concurrency-tested`, not
as a general cross-operation linearizability proof.

## Schema compatibility

The v0.3 runtime admits exactly the PostgreSQL schema it was built against
(`MINIMUM_SCHEMA_VERSION == CURRENT_SCHEMA_VERSION == 12`). A wider window
would be a false promise: reservation storage only arrived in schema 8, the
live `claim` relation only disappeared in 10, and the overlap/role correction
is 12 — a client admitted at 5..11 would pass the handshake and then fail on
its first reservation call. Migrations are deployment-owned, so the cutover is
one coordinated migrate-then-deploy step, and rollback is restoring the
pre-cutover database and runtime together.
