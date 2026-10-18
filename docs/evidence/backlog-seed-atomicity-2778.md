# Per-candidate backlog seeding repair

Bounded child `sprintctl#2778` of unfinished repository-layer item `sprintctl#2150`.
Native decision #5578 freezes the reviewed plan under Release `59e43ee3315ebc2c8b7ce650fef9218c71d2e4e6590ab866177cf7b3cf33f49a`.
Actual before-repair faults ran on `191fc9e`; the candidate was moved onto qualified
sibling base `a5d79985e66c61bf6a8a569ca7b0b3cf857ae3a2` before its full gates.

The existing manual seeding function promised event-backed retry idempotency,
but public item creation committed before its `backlog-seeded` event. Actual
SQLite and disposable PostgreSQL INSERT triggers left a committed item and
knowledge track with no link. Removing the fault and retrying produced two
items and one link. The retained counterexample contains actual before/after
rows and log hashes; it does not turn an expected RED into a passing gate.

A private shared workflow now owns one candidate's noncommitting track/item
creation, admitted canonical link and prepared returned item inside the backend
transaction. SQLite serializes writers; PostgreSQL locks the distinct source
and target sprint rows in ID order, then rechecks the selected event and target
receipts. Initial candidate IDs are finite; eager validation and receipt errors
remain before writes. Earlier successful candidates stay committed on a later
failure, and retry returns only newly created items.

Initially all-linked calls intentionally stop making an empty missing track or
committing caller writes. This is a repair delta. PostgreSQL fresh work retains
its public precommit convention; initially no-op work avoids it. A candidate
made redundant while waiting may already have crossed precommit. SQLite active
caller refusal occurs before unrelated rollback. See the descriptive
[protocol draft](../protocols/backlog-seeding.md) for exact scope and limits.

The frozen oracle has 26 shared cases per backend, 3 additional SQLite caller
cases and 6 PostgreSQL caller/tenant cases: 29 SQLite and 32 actual PostgreSQL focused tests
passed. It covers real linkage SQL faults, after-insert/result exceptions,
second-candidate rollback with an earlier committed prefix, noops, historical
receipt representation, actor/default/title/source linkage and caller state.
Seven actual ordered histories/backend cover a waiting competing writer,
selected-event deletion/move/reclassification, opposite-direction calls and
source/target disappearance. Workers independently verify READ COMMITTED.

SQLite's attempted BEGIN and PostgreSQL's measured blocking PID establish
different bounded observations. Opposite-direction completion is recorded;
sorted lock SQL is separately source-inspected. No combined pending-caller/
selected-fresh race or exhaustive schedule/property proof is claimed. The
isolation guarantee excludes arbitrary REPEATABLE READ/SERIALIZABLE snapshots.
Scoped integer IDs require their existing authority mappings; this is not an
archival/migration or cross-provider identity proof.

The full local CI selection passed 2,711 tests, with 4 skips and 14 performance
cases deselected, in 278.47s. The full disposable PostgreSQL suite passed 1,385
tests in 79.94s; all 81 cleanup instances across 4 fixture names reported zero
remaining rows. Actual ordered histories are extracted only from those completed
full runs. Exact commands and artifact hashes are retained in the result packet.
Source-base provenance is declared by the run; byte hashes do not authenticate
external origins. Exact-head canonical CI remains a publication gate. All fixtures are local
temporary repositories or registered disposable PG scopes. Historical evidence
packets remain unchanged. No new provider API/schema/IDL, served ingestion,
retired automation or service deployment is included; parent #2150 remains open.
