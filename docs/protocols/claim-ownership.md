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
| State variables | claim ID, item ID, type, exclusive flag, expiry, token, owner metadata, coordinator claim |
| Operations | create/start, heartbeat, status mutation, release, handoff, resume, recover |
| Claim precondition | Item exists; no conflicting live exclusive claim, except a proof-authorized coordinator delegation |
| Proof precondition | `claim_id + claim_token`; identity and Git metadata are advisory |
| Success effect | The backend commit durably creates, updates, rotates, or removes the claim |
| Failure effect | Validation and conflict failures must not apply the requested claim mutation; diagnostic events are separate history effects |
| Unknown outcome | A lost response after commit may leave a created, refreshed, released, or token-rotated claim even though the caller did not receive success |
| Idempotency | Claim creation and token rotation are not idempotent requests; retry only after observing current state |
| Recovery | `claim resume`, local-mode `claim recover`, or explicit proof-bearing handoff/adoption |
| Projection | Resume, context, next-work, and handoff surfaces derive from backend rows and events and never include claim secrets |
| Liveness | No automatic progress is promised; expiry, operator retry, heartbeat, or handoff enables later progress |

## Linearization candidates

- SQLite claim creation enters `BEGIN IMMEDIATE`, checks conflicts, inserts, then commits. The commit is the durable linearization point; the reserved write transaction serializes competing local writers.
- SQLite handoff and proof-bearing mutations take effect at their update/delete commit.
- PostgreSQL claim creation currently checks for a conflicting row and then inserts in the transaction. The schema has a token uniqueness index but no database constraint shown here that enforces at most one live exclusive claim per item.
- PostgreSQL handoff and proof-bearing mutations take effect at their update/delete commit.

The intended invariant is at most one live exclusive owner outside an authorized coordinator delegation. That invariant is not yet established for concurrent PostgreSQL claim creation: the present check-then-insert path requires an independent-connection history test and may require a lock, constraint, serializable transaction, or other repair. This document does not select or authorize that repair.

## Token rotation and stale proof

Successful rotate-mode handoff mints a new token. After the handoff commit, the old token must fail heartbeat, release, status mutation, and further handoff. If the response containing the new token is lost, the outcome is unknown to the caller; observing claim state and using the documented recovery path is required before retry.

Expiry is not a fencing token. An expired claim ceases to block according to backend time, but downstream systems cannot use its age as proof against a stale actor. Any future lease-based external side effect requires an explicit epoch or fencing design.

## Backend parity evidence

Backend parity means equivalent accepted/rejected histories and public contract shapes for the bounded scenarios, not identical SQL. Record SQLite and PostgreSQL results separately. Until the concurrent PostgreSQL scenarios run, classify exclusivity parity as `unknown`, not linearizable or concurrency-tested.
