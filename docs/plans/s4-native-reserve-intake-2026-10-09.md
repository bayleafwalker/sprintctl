# S4 native reserve intent carrier

Status: bounded source slice for agentops#2485, on Sprintctl owner 0.14.0
merge `e8c6cf32656090c0a2e5206a14cb7eb237f196a7`. This continues the
[reserve owner prerequisite](s4-native-reserve-owner-2026-10-04.md).
Sprintctl 0.14.1 adds producer commands; catalog descriptors, SQLite schema 25,
remote schema 21 and immutable adapter-kit 0.2.0 stay unchanged.

## Capture and synchronization contract

`authority reserve-queue --request REQUEST --run-binding BINDING` captures
intent offline. The closed JSON envelope has `schema_version` equal to
`native-reserve-request/v1`, `operation` equal to
`work.reservation.reserve-v1`, `idempotency_key`, and `arguments`. Arguments
require positive `item_id`, nonempty `actor` and `session_id`, and the full
previously observed owner Release `expected_revision`, including its revise
generation. Optional role, correlation, interruption and execution acceptance
contract use owner normalization. Non-execution requests cannot freeze a Release.

The separately supplied binding names the previously registered run and its
repository, principal, workspace, client and grant. Offline capture does not
look up credentials, create a run, reserve work, freeze a Release or allocate an
observation sequence. It stores original request/binding bytes and their separate
SHA256 digests alongside normalized forms in two append-only producer tables.
Same scoped owner key and identical normalized content/binding return the
original capture. Different content, run or grant under that key refuses; it
does not overwrite the retained bytes. This slice offers no correction API.

`authority reserve-status` reads without creating tables or opening a network
connection. Partial table pairs and damaged intent/confirmation content refuse.
Producer mutation uses its own nonblocking file lock. Existing evidence intake
and observation streams remain independently synchronized.

`authority reserve-sync` requires served mode. It resolves the exact registered
binding, invokes native reserve with the captured key in the invocation envelope,
and for execution reads the returned Release digest. The carrier confirms only
after correlating immutable admission identity, current immutable reservation
fields, overlap annotations, full basis, normalized contract and recomputed
Release content digest. Current state, actor, session, activity and correlation
may change legitimately after admission; replay never recreates or touches them.
Every invocation records a durable started attempt first. Confirmation retains
the correlated reservation/Release responses and a content digest. Lost replies,
malformed receipts and typed owner refusals remain pending and stop later reserve
requests. A refusal after reserve admission does not imply rollback of that
already committed effect. Exact retry uses the original key and retained input.
The entire sync pass uses one lazily resolved credential snapshot held only in
memory. Each operation still constructs a fresh client within its own event loop.
Replacing the credential file midpass cannot switch the authenticated replay
namespace; expiry or revocation fails with the original credential.

Generic `authority sync` reports reserve pending IDs but does not invoke native
reserve. An unavailable reserve status is explicit rather than an empty queue.
No reserve request enters the legacy observation batch operation.

## Verification boundaries

Tests use temporary SQLite producers and the actual native application on a
disposable PostgreSQL16 owner. Histories exercise offline no-effect capture,
protected contracts, lost postcommit replies, stale-basis head blocking, invalid
and missing receipts, replay after reassignment/release, non-execution roles,
changed caller/binding, corrupted confirmation, and two producer contention.
Independent connections synchronize immediately before native admission and
check one retained effect; this is bounded application-invariant evidence, not
a deployed HTTP/authentication proof or a new measurement of lock wait order.

Original bytes and stored content hashes detect the tested damage; this is not
an adversarial filesystem integrity guarantee. Removing all carrier tables or
replacing the entire database is outside this bounded claim. There is no new
retention/purge policy. These receipts contain internal identities; publication
of real receipt packets requires the established public-repository review.

## Remaining S4 gates

This source does not deploy the owner, enroll a current principal/workspace-bound
caller, import historical shards, change hooks, start soak, or complete S4. The
proposal carrier and causal reserve/evidence/proposal pipeline remain separate.

For an isolated offline commit/trailer rehearsal, first obtain a genuine owner
run and Release while online, then release the preparatory reservation normally.
After stopping only that disposable authority, capture a new reserve against
the observed full basis and commit with the previously owner-observed Release
digest. An offline intent cannot mint that digest. On reconnect synchronize
reserve first and require the returned digest to equal the previously observed
digest before harvesting the commit trailer or synchronizing dependent evidence.
A mismatch fails the dependent leg; it must not rewrite the recorded commit.

The current proposal owner records its current Release under the item lock but
has no expected-Release/evidence admission precondition in public arguments.
A bounded no-intervening-change rehearsal can prove observed linkage, not general
atomic causal admission across streams. Broader proof requires an explicit owner
precondition contract or a clearly limited gate. Dead harness, expiry, real
offline/online equivalence, import isolation, hook cutover and soak are still
independent operational gates.
