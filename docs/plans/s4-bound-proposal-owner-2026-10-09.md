# S4 bound proposal owner admission

This additive owner slice follows the 0.14.2 producer carrier.
The isolated source implementation passed 1,821 unit tests and 813 disposable
PostgreSQL tests, including 27 bound admission histories; 56 fixture scopes
cleaned up with zero rows. Astra approved this bounded source after the lock
and authorization histories qualified. Release 0.15.0 publication, service
qualification and deployment remain separate gates. Existing `propose-v1`, its
catalog descriptor and its canonical six-field content digest stay unchanged.

The proposed `work.effect.propose-bound-v1` uses the same effect-propose
capability and an argument idempotency key. It additionally requires a closed
`causal_basis`: full owner-observed `expected_revision`, `release_digest`,
`reserve_idempotency_key`, a trailer-bearing 40-character lowercase hexadecimal
`commit_sha`, and `evidence_tail` containing its item identifier, sequence and
entry digest. The entry digest uses the owner's existing chain definition.

First admission authenticates the complete registered run binding, claims the
existing internal `propose_effect` idempotency namespace, locks the item, and
then acquires the existing per-run evidence-chain advisory lock. A fixed bound
operation discriminator and every causal argument participate in its request
digest. Sharing the internal namespace serializes legacy/bound key collisions
before either request takes item locks; distinct public operation descriptors
remain intact.

Under those locks, the full current item revision must match, the current
Release digest must match, and that Release's recorded item revision must equal
the locked full revision. Digest equality alone is insufficient after a
description edit. Ordinary committed reads must find the caller-scoped native
reserve ledger entry with its original admission snapshot, the exact
Release/commit pair in `release_commit`, and the exact current evidence tail.
No admission path claims or waits for a reserve key, invokes trailer ingestion,
locks an existing intent, or acquires a repository advisory lock after the item.

A fresh proposal and its immutable admission receipt commit in one owner
transaction. Every refusal or SQL failure rolls both back. Successful exact
replay authenticates and compares the complete request digest, then returns the
original admission alongside the current proposal lifecycle without rechecking
changed item or evidence preconditions. Changed guards under a committed key
conflict; refresh/correction requires an explicit reviewed request key.

Required disposable independent-connection histories include description/revise
and evidence-append orderings, legacy/bound and bound/bound same-key races,
uncommitted reserve/trailer prerequisites without circular waits, existing
intent acceptance/application contention, maintenance/lease contention without
reverse lock acquisition, rollback after insert and commit-before-reply loss.
Only after these qualify can the separate real isolated offline pipeline bind
reserve, exact Release, trailer ingestion, evidence and proposal admission.
Legacy import, hook cutover and soak remain later independent gates.

The existing-intent sentinel holds an unrelated existing intent lock during
first admission. Cross-operation same-key adoption is tested separately in
the shared-ledger races. Replay is tested while an independent connection
holds both item and evidence-chain admission locks. Authorization refusals
exercise the actual bound operation, including foreign-scoped reserve keys.
The catalog baseline is captured from the attested published 0.14.2 wheel.
