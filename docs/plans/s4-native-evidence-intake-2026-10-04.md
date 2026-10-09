# S4 first native intake carrier: run-bound evidence

Status: proposed source slice for agentops#2485, 2026-10-04. This is not
#2485 acceptance, deployed intake, import, cutover, soak or auditctl retirement.
The governing direction is TS-6/Decision199 and the merged
[proposed S4 preparation](https://github.com/bayleafwalker/agentops/blob/49ac361481d185a2b0c3e18899d19756096473d1/docs/plans/2026-10-03-s4-evidence-home-preparation.md).
The missing-gate fixture and owner plan are in Sprintctl draft PR119,
`35114c4d133332db5c914f5836f4bda1a700c3ed`. Native owner baseline is
`f6936f410f41a7dfaf7e5a1390c552eb90954874` (0.12.0). The current integration
base is `0a1e5b0f93a9b44caf42c5c1e9f4dbac433e04c1` (owner 0.12.1 and
immutable adapter-kit 0.2.0); that compatibility release changes no native
evidence semantics or legacy operation/schema builders.

## Boundary and authority

This slice captures evidence intent into two append-only tables in the existing
producer outbox database. It supplies `authority evidence-queue`,
`authority evidence-status` and `authority evidence-sync`. It does not broaden
the batch record matrix, allocate observation origin sequence numbers, replace
observations or create a second authority. Existing `authority sync` reports
`pending_evidence_request_ids` but does not silently upload this separate stream.
An operator must explicitly run `authority evidence-sync` for this slice.

The producer must already have a real registered run's six-field
`work.run.resolve-v1` result: repository, run, principal, workspace, OAuth client
and grant. Capture binds the request to those expected fields and checks the
repository marker, but these are untrusted intent metadata until the owner
resolves that run for the authenticated caller. The carrier never registers a
synthetic offline run and never opens an authority database directly.
Receipt content comparison permits JSONB numeric normalization but distinguishes
booleans from numbers; raw authored bytes and native key identity stay exact.

Synchronization uses only `work.run.resolve-v1` and `work.evidence.append-v1`
through the existing scoped client/session abstractions. The owner's existing
capability and principal/workspace/client/grant checks remain authoritative.
No profile grant or capability is added.

The original request bytes and binding bytes, each with its own SHA256, are
stored unchanged. A canonical JSON argument representation supports local
comparison; the authoritative evidence digest supplied by the producer and the
owner's canonical idempotency digest are separate domains. Repeated capture of a canonically equivalent request (formatting or key order
only) returns the first request identity and its original digest,
plus the submitted request and binding digests, without replacing the original bytes or enqueueing
another effect. The complete served catalog remains the argument-schema
validator; offline capture checks the closed field set, basic structures, run
binding and credential-shaped values and confers no schema or authority approval.

## Attempt and receipt states

| Producer state | Meaning | Next permitted action |
| --- | --- | --- |
| Captured | Durable intent; nothing uploaded or effective | Explicit sync |
| Started without completion | Process interruption or not yet completed | Exact retry; no tail correction |
| Unknown | Transport/reply loss, malformed response or unresolved binding/receipt | Exact retry; no tail correction |
| Rejected | Measured native refusal code/status for the named operation | Exact retry; tail correction only as below |
| Confirmed | Owner receipt correlated to captured repository, run, content and expected tail, then durably stored | No further invocation from this producer |
| Superseded intent | Original request and all attempts remain immutable | Only its explicit correction is pending |

Status reads use a read-only SQLite snapshot, take no carrier lock and never
initialize native tables. Ordinary batch sync preserves its completed receipts
if native status is unavailable, reporting a null pending list plus an explicit
error instead of inferring zero pending requests.

A process-wide advisory file lock serializes capture, correction and sync for
one producer database across processes. It is neither an execution lease nor
a distributed authority lock. Started attempts are committed before invocation;
completion events are appended separately. A killed process releases the local
lock, leaving its started attempt pending. SQLite's FULL synchronous/WAL settings
come from the existing outbox owner. The remote effect and local receipt are not
one transaction: losing a reply or failing to persist a receipt requires exact
native retry. Native ledger/evidence atomicity remains with PostgreSQL.

Corrections keep the original request's queue position. Synchronization stops
at the first unconfirmed request across **all runs** in this producer database
and reports every
unsuperseded pending request. An unresolvable head (changed grant, key conflict, item-id reuse or differing
accepted tail) blocks every later run. The operator must retain the producer
and owner receipts, stop the S4 gate, and obtain a separately reviewed owner
reconciliation/withdrawal contract. Do not delete rows or rotate the producer
to conceal a gap. This slice promises no liveness for those histories.
No automatic retry, tail refresh, rebasing,
expiry decision or credential acquisition occurs in offline capture. Owner
exceptions retain only operation, code and status; arbitrary exception messages
and credential values are never copied into attempts.

## Existing owner idempotency and correction

`WorkApplication._evidence_append` invokes the existing ledger and evidence
chain in one native transaction. A successful key replay returns the stored
result, a changed semantic request conflicts, and a failed chain effect rolls
back the ledger. Failed effects are not permanent resource-command Decisions.
The native key identity deliberately excludes `chain_seq` and
`chain_prev_digest`. This carrier does not change that contract.

An explicit `--supersedes REQUEST_ID` correction is allowed only after the
latest completed attempt is an actual `work.evidence.append-v1`
`evidence-chain-conflict` HTTP409. Only the expected tail fields may change;
the same binding, key and semantic evidence remain. Original failed bytes and
attempts remain in the append-only tables. A similarly named refusal from run
resolution, an uncertain reply, a changed semantic request or a successful
receipt cannot authorize a correction. A stale tail must be corrected from a
fresh independently obtained owner tail; the carrier never invents it.

For its first observed confirmation the carrier requires the returned evidence
content and chain tail to match the captured request. This is deliberately
conservative: a valid native replay can name a different tail if another actor
already used the same semantic key. Such a response stays unconfirmed here;
native replay remains valid, but explicit owner reconciliation is needed before
this producer can establish its expected-tail proof. This slice supplies no
reconciliation command that could silently rewrite the original request or
claim that an unrelated-chain response confirmed it.

## Isolated use and remaining gate

Only after review/deployment and with an already registered run and authorized
served profile, the intended source commands are:

```text
sprintctl authority evidence-queue --request request.json --run-binding run-binding.json
sprintctl authority evidence-status
sprintctl authority evidence-sync
sprintctl authority evidence-queue --request corrected-tail.json --run-binding run-binding.json --supersedes REQUEST_ID
```

This PR's tests use temporary producer databases and a new disposable
PostgreSQL16 authority, calling real `WorkApplication.invoke` with explicit test
contexts. They exercise offline capture, original digests, duplicate capture,
commit-before-reply-loss, conflicting key content, invalid predecessor rollback
and corrected retry, stale-tail correction, wrong grant, malformed/unrelated
receipts, conservative different-tail native replay and two independent
producers contending through the real native ledger. They test the owner
application boundary, not deployed HTTP authentication or a production profile.
A transport-constructor trap proves offline CLI capture does not ask for
identity or credentials. Process lock contention and durable interrupted-attempt
reopen are bounded example tests, not a full process-kill or distributed proof.

Reserve and proposal intake remain missing separate owner work. A run must be
registered before going offline. Cross-stream reserve/evidence/proposal causal
ordering and continuously-online effective-state equivalence are not established.
The full isolated S4 absorb gate, digest-preserving legacy import, coordinated
cutover, rotated hook transition and ninety-day read-only soak are still open;
no dates start from this source PR. Resource-authority #2603 is not an intake
carrier. No live import, classification, grant, NDJSON upload, hook change,
legacy retirement or production migration is authorized by these tests.

## October 9 credential snapshot prerequisite

`authority evidence-sync` constructs one lazy native invoker for its entire pass.
The first authenticated request resolves the configured credential reference;
run resolution and every append or exact retry in that pass reuse that same
in-memory credential. Each RPC creates a fresh asynchronous client. Replacement
credentials become visible in a new pass; expiration or revocation stops the
current pass with its original identity and leaves pending intent for review.
The snapshot never enters producer rows or receipts.

This prerequisite does not establish causal proposal admission, offline/online
equivalence, import readiness, hook cutover or a soak start date.
