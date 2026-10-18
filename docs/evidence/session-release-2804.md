# Caller-session reservation release extraction

Bounded child `sprintctl#2804` of unfinished repository refactor `sprintctl#2150`.
No doc5778 and Release `615e721a0d50e54056edd38fb2ddd3af0a2eb09d74a6a7a9166d03462d5ebb76`
govern the change on accepted base `7af2f81f7f6b4ab50da4eb16fb62505a4fd0e3d2`.

The distinct private participant in `reservationcore` owns falsey-session and
empty-selection exits, conversion and clock ordering, batch release, selected-row
event order and returned IDs. Backend adapters retain their original SQL,
tenant predicates and dynamic event helper. Constructors only store a handle.
The helper never starts, commits or rolls back a transaction. PostgreSQL's SELECT
cursor exits before the clock and its UPDATE cursor exits before event insertion.

All active roles for the exact session release regardless of actor. Returned IDs
and events follow the original selected snapshot, while UPDATE continues to match
all currently active rows for that item/session. A deliberate same-owner raw
insertion at the clock hook characterizes this distinction; it is not a normal
concurrency scenario or a repair. Sprint ID conversion remains inside each event
iteration after UPDATE, preserving provisional effects on a late conversion error.

The oracle has 16 shared actual-backend cases and two additional PG cases.
Primary review found two missing discriminators in the initial passing oracle:
falsey tests started idle, and a descending input could hide descending sorting.
The refined oracle uses actual pending caller work and explicit transaction
cleanup tripwires, plus independently expected nonmonotonic selected-row order.
Four actual temporary wrong implementations fail as intended: early commit,
early rollback, ascending sorting and descending sorting. Earlier captures and
freeze remain historical; note5793 corrects their qualification, and note5794
freezes the independently reviewed revised oracle before any product edit.

The refined baseline passed 132 local tests and 399 PG tests, with 23 registered
cleanup checks empty. The unchanged post-extraction selection passed the same
132 and 399 tests, again with 23 empty cleanup checks. Independent actual source
review and a separate 16-case SQLite run are clear. All 396 adjacent existing
functions/classes retain their ASTs; only the two original helpers delegate.

Actual UPDATE trigger faults preserve native behavior: SQLite remains ACTIVE;
PostgreSQL becomes INERROR and refuses reads until its owner rolls back. Python
errors after actual event insertion leave preceding effects provisional. Tests
observe primary pending work and independent durable state before owner cleanup.
Four retained admission waits use preexisting caller-held SQLite writer or PG
item locks. The extracted helper adds no lock or serialization promise.

Full local regression passed 2988 tests with 4 skips;
full PG passed 1640 tests with 87 registered cleanup checks empty.
Twenty retained before observations and twenty full-run observations are separate
captures, each with sixteen sequential observations and four actual held-lock waits.
See `verification/results/session-release-2804.json` for exact execution provenance.
No public provider/API/schema/IDL, lease/report policy, live identity, deployment,
whole-provider parity or parent2150 completion is claimed.
