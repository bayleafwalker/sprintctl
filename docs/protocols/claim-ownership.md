---
doc_id: sprintctl.claim-ownership
status: draft
supersedes: null
---

# Claim ownership protocol

This document closes the verification boundary around claim creation, proof-bearing mutation, expiry, handoff, and SQLite/PostgreSQL parity. It records intended safety separately from what current evidence establishes.

## Contract

| Field | Contract |
|---|---|
| Subject | One claim set for one repository-scoped work item |
| State variables | claim ID, item ID, type, exclusive flag, status, expiry, lease epoch, token, owner metadata, coordinator claim |
| Operations | create/start, heartbeat, status mutation, release, handoff, resume, recover |
| Claim precondition | Item exists; no conflicting live exclusive claim, except a proof-authorized coordinator delegation |
| Proof precondition | `claim_id + claim_token`; identity and Git metadata are advisory |
| Success effect | The backend commit durably creates, updates, rotates, expires, or removes the claim |
| Failure effect | Validation and conflict failures must not apply the requested claim mutation; diagnostic events are separate history effects |
| Unknown outcome | A lost response after commit may leave a created, refreshed, released, or token-rotated claim even though the caller did not receive success |
| Idempotency | Claim creation and token rotation are not idempotent requests; retry only after observing current state |
| Recovery | `claim resume`, local-mode `claim recover`, or explicit proof-bearing handoff/adoption |
| Projection | Resume, context, next-work, and handoff surfaces derive from backend rows and events and never include claim secrets |
| Liveness | No automatic progress is promised; expiry, operator retry, heartbeat, or handoff enables later progress |

## Linearization candidates

- SQLite claim creation enters `BEGIN IMMEDIATE`, checks conflicts, inserts, then commits. The commit is the durable linearization point; the reserved write transaction serializes competing local writers.
- SQLite handoff and proof-bearing mutations take effect at their update/delete commit.
- PostgreSQL claim creation locks the repository-scoped `work_item` row with
  `SELECT ... FOR UPDATE`, then checks the live exclusive claim set and inserts
  within the same transaction. The work-item row lock is the arbitration point;
  the transaction commit is the durable linearization point. A time-dependent
  uniqueness constraint is not used because expiry is evaluated by backend time
  and proof-authorized coordinator delegation intentionally permits multiple
  exclusive rows.
- PostgreSQL handoff and proof-bearing mutations take effect at their update/delete commit.

The intended invariant is at most one live exclusive owner outside an
authorized coordinator delegation. Independent-connection histories exercise
two overlapping PostgreSQL claim attempts at `READ COMMITTED`: the second
attempt waits at the work-item row lock and, after the first commits, rejects
against the newly visible claim. SQLite retains its `BEGIN IMMEDIATE` writer
serialization. This is bounded concurrency evidence for the application
invariant; it is not a fencing-token or distributed lease claim.

## Token rotation and stale proof

Successful rotate-mode handoff mints a new token. After the handoff commit, the old token must fail heartbeat, release, status mutation, and further handoff. If the response containing the new token is lost, the outcome is unknown to the caller; observing claim state and using the documented recovery path is required before retry.

Remote-mode expiry is append-only: maintenance and reacquisition mark a claim
`expired` and retain its row instead of deleting it. Active-claim projections
require both `status=active` and an expiry later than backend time. Local SQLite
keeps its existing purge behavior; it carries the same columns for schema
parity.

TTL expiry alone is still not a fencing token.

`lease_epoch` is the future fencing value for a claim lineage. It starts at 1,
advances when remote ownership proof rotates, and advances again when a new
remote claim reacquires an item after expiry. It is exposed now so retained
history has the right shape, but no command accepts an expected epoch and no
downstream fencing enforcement is implemented in the single-operator path.
Local SQLite carries the column for schema parity without changing its claim
behavior.

## Backend parity evidence

Backend parity means equivalent accepted/rejected histories and public
contract shapes for the bounded scenarios, not identical SQL. SQLite uses a
reserved writer transaction and PostgreSQL uses the work-item row lock; both
accept exactly one of two overlapping unrelated exclusive claim attempts. The
bounded exclusivity result is classified as `concurrency-tested`, not as a
general cross-operation linearizability proof.
