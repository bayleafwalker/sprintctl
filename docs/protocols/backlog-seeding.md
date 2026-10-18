---
doc_id: sprintctl.backlog-seeding
status: draft
supersedes: null
---

# Per-candidate backlog seeding

This documents the existing manual `sprint backlog-seed`/backend function and
bounded repair sprintctl#2778, frozen in native decision5578 and its Release.
It does not ratify a provider contract or enable automatic knowledge ingestion.
The source and target are sprint IDs within one backend authority/repository.

## Existing identity and initial reads

A `backlog-seeded` event in the target records the source event ID. That target
plus source event ID is the existing deduplication identity. A source event may
seed separate target sprints. Titles, actors and source work-item IDs are not
deduplication keys. Integer sprint/event IDs require their existing authority
mapping; this repair does not qualify migration or archival remapping.
Existing linkage values are compared without ID coercion or
historical repair; fabricated or deleted generic linkage is outside the proof.

The function checks source existence before target existence, eagerly reads and
decodes the finite candidate population in existing creation-time/ID order,
and returns immediately for no candidates. Otherwise it eagerly reads/decodes
target linkage events before any candidate write or PostgreSQL precommit.
Malformed receipt errors remain visible. Initially linked candidates are skipped
for that invocation; later source arrivals wait for another invocation. No new
kind restriction, source/target inequality or candidate acceptance gate is added.

Initially empty or all-linked calls create no track and do not commit caller
writes. Removing the old all-linked/missing-track setup side effect is an
intentional repair. It does not reconstruct a missing linked item or repair a
historical receipt.

## One selected candidate

Each initially fresh candidate has its own backend-owned transaction:

1. SQLite acquires its writer lock with `BEGIN IMMEDIATE`. PostgreSQL locks
   the distinct source/target sprint rows in database ID order, scoped to the
   repository, before revalidation. Same-source/target seeding locks one row.
2. Recheck source then target existence. Re-read the selected event by ID,
   source sprint and existing knowledge type. Missing/moved/reclassified events
   are skipped before target creation.
3. Re-read target linkage under the lock. A conforming competing writer's
   committed link makes this candidate a skip.
4. Obtain/create the `knowledge` track and create the pending item through
   existing noncommitting track/work-item core adapters. Preserve summary
   fallback only for an absent key, actor, source fields and backend item shape.
5. Require generic event admission, insert the canonical `backlog-seeded`
   event, then read/construct the complete item result before commit.
6. Commit this candidate before appending its prepared result to the response.

Track, item, linkage and result preparation share that candidate transaction.
A failure before commit rolls back newly introduced candidate state, including
its new track; an existing track is preserved. Earlier candidates remain
committed if a later candidate fails. This is not whole-command atomicity.

Retry returns only items newly created by that invocation. A committed link
permits a later conforming retry to skip its candidate; there is no cached
original response. Commit acknowledgement loss remains an unknown outcome and
is not converted into a safe instruction to mint a second identity.

## Caller transactions and isolation

SQLite initially fresh work requires an idle connection. Refusal occurs before
the rollback handler, so unrelated caller writes are neither committed nor
rolled back. Initial no-op calls do not introduce this refusal.

PostgreSQL retains the public committing-mutator convention for initially fresh
work: commit implicit caller state outside the owned failure handler, then
enter the owned transaction. Managed outer/two-phase commit refusals therefore
do not roll back caller work. A candidate that becomes linked or disappears
while waiting may already have crossed this precommit boundary. A later owned
failure does not undo unrelated precommitted work. This is not a promise of
SQLite/PostgreSQL caller-composition equivalence.

Conforming-writer deduplication is bounded to SQLite writer serialization and
PostgreSQL's default READ COMMITTED isolation. A receipt read after waiting on
a lock does not establish the same property under arbitrary REPEATABLE READ or
SERIALIZABLE snapshots. This repair adds no isolation guard, synthetic sprint
update or schema to widen that guarantee. Initial selection and candidate reads
are not one whole-command snapshot.

## Verification limits

Use temporary SQLite repositories and registered disposable PostgreSQL scopes.
Retain actual before-repair linkage insertion faults and after-repair row/event
observations, injected fault locations, caller-state checks and bounded ordered
concurrent histories. A SQLite attempted writer-lock acquisition and a measured
PostgreSQL blocker are distinct observations; neither becomes a global liveness
or fairness guarantee. Opposite-direction completion alone does not prove all
possible lock orders.

No new API/IDL/schema, served operation, hosted lease, deployment, cross-provider
conformance, process-kill or power-loss guarantee is supplied here. Retired
knowledge automation and the foundation contract chain remain separate work.
