# S4 bound proposal producer carrier

This slice extends the released 0.14.2 carrier to the additive owner admitted
in 0.15.0. Candidate 0.15.1 passed 1,857 unit tests and 820 disposable PostgreSQL tests
with 57 empty cleanup scopes; seven new actual-owner histories passed. Astra
approved the bounded source. Publication and deployment remain separate gates. Owner catalog, schema21,
authority and canonical six-field content digest are unchanged here.

The existing `authority proposal-queue/status/sync` commands accept two exact
envelope pairs: `native-proposal-request/v1` with `work.effect.propose-v1`, or
`native-bound-proposal-request/v1` with `work.effect.propose-bound-v1`. Both
have exactly schema_version, operation and arguments; neither has an envelope
idempotency key. Legacy arguments remain eight fields. Bound arguments add
exactly causal_basis, using the closed full-revision/Release/native-reserve-key/
trailer-commit/evidence-entry-tail guard contract. Capture preserves original
bytes, whitespace and guard values without reading or granting authority.

Both variants use the same append-only request/attempt tables, scoped producer
key namespace, process lock and FIFO ordering. Reusing a key across variants
or changing captured guards is refused. Synchronization resolves the captured
six-field run binding, then invokes its captured operation with argument key.
One credential snapshot spans the pass; each RPC has a fresh client. Unknown
outcomes and explicit refusal stop FIFO with original requests still pending.
Unavailable bound admission never falls back to legacy admission.

Legacy confirmations retain their prior shape. A bound confirmation requires
exactly repo_id, intent and admission. Its immutable content and principal
must match, its content digest must recompute and its current lifecycle may
have advanced. Its intent Release must equal the captured Release. Admission
must have exactly its v1 schema marker, the complete captured causal basis,
all six captured run-binding fields and a positive nonboolean reservation ID.
Every historical confirmed attempt must name its captured operation. Reading
history rechecks all original hashes and correlations without refreshing
current work, evidence or reservation prerequisites.

Qualification includes original confirmed and pending rows produced by the
actual attested 0.14.2 wheel, mixed FIFO, variant key collisions, receipt and
history damage, actual postcommit reply loss/current lifecycle, independent
producers and bound CLI/credential dispatch. The released fixture retains
original request bytes and confirmed receipt bytes. Its receipt was generated
by the old carrier facade, not a live owner; actual owner recovery is covered
separately by disposable PostgreSQL histories.

Service composition/deployment and the stopped-authority reserve → trailer →
evidence → bound proposal equivalence gate follow this source slice. Import,
hook cutover, attribution, soak and retirements remain separate requirements.
