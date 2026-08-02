---
doc_id: sprintctl.maintenance-capability
status: draft
supersedes: null
---

# Exact-plan maintenance capability protocol

Sprintctl persists lifecycle authority for an independently reviewed
`maintenance-envelope/v1`. It does not execute Git, publication, backup,
migration, or cluster commands. The capability ID is a non-secret reference;
every state change is fenced by an exact revision and immutable request ID.

## Authority boundary

`prepare` freezes canonical envelope bytes, digest, exact plan reference,
operator decision, repositories, steps, commands, reviews, verification,
publication evidence, window, JIT constraints, abort, and audit policy.
`attest` records that preparation has been independently checked. Preparation
and attestation may occur while the accountable coordinator's ordinary work
claims exist.

`activate` is different: it queries the authoritative backend claim table and
fails unless there are zero live ordinary claims. It also requires Plan 1's
zero-session and zero-claim evidence in the frozen envelope. After activation,
the maintenance capability—not a claim, lease, credential, approval, recovery
record, or publication grant—is the sole authority for its exact reviewed
steps. A later work claim does not enlarge the capability.

Each `activate` or `observe` effect identifies one exact step and registered
command plus content-addressed command and effect receipts. Required JIT values
must be declared in the envelope, match their frozen patterns, and be bound no
later than their target-step deadline. They cannot select commits, commands,
paths, reviewers, authority, or recovery policy.

## State and failure model

The durable sequence is `prepared → attested → active → observing →
reconciled`. `abort` and `revoke` terminalize any non-terminal capability;
evaluation at or after immutable `expires_at` terminalizes it as `expired`.
Expiry cannot be renewed or extended. Continuing work requires a new envelope,
new review, new capability ID, and a new zero-claim activation gate.

Every request carries a canonical UUID and expected revision. Replaying the
same request and bytes returns the prior receipt; changing bytes under the same
request ID rejects. A stale revision, wrong plan/step/command, missing receipt,
late JIT binding, active ordinary claim, invalid transition, or expired window
has no requested authority effect. SQLite uses `BEGIN IMMEDIATE`; PostgreSQL
locks the repository-scoped capability row. This is bounded CAS/idempotency
evidence, not a general distributed linearizability claim.

## Recovery and audit

Recovery `observation` and `requested-command` records append immutable audit
input only and return `authority: none`. They cannot prepare, attest, activate,
advance, reconcile, abort, revoke, or expire a capability. Reconciliation must
retain command/effect/review/publication/JIT/start-gate/abort receipts, correlate
the incident and actor, export an append-only or content-addressed history, and
redact credentials, claim tokens, and capability secrets.

PostgreSQL parity tests must use disposable repository scopes. Fault and
concurrency tests must never run against the shared served backend.
