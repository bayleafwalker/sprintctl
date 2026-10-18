# Shared explicit sprint-close sequencing

Bounded child `sprintctl#2780` of unfinished `sprintctl#2150`, frozen by native
No doc decision5600 under Release
`302f9ee474e60afeea2529154b02ac4361dbad656805599bf6fdf8b44d2a3c36`.
The actual baseline is accepted SQLite caller repair merge
`0b1f4ad11b52c5526443487e6cc5f90a84a5a8cb`.

Private sprintcore sequencing now shares missing/basis/transition checks, the
closed status write and canonical typed boundary payload. Backend participants
retain exact SQL, tenant filtering, decoding and locks. The PG SELECT/UPDATE
cursor still exits before canonical insertion. Public wrappers retain actor then
basis validation, SQLite2779 idle refusal outside its handler, SQLite BEGIN,
and their original commit/rollback behavior. No generic-event gate is introduced
for the owner-written typed boundary. Maintenance auto-close remains separate.

The actual paired oracle passed before product edits: 16 SQLite and 24 disposable
PG cases. Those three oracle files stayed byte-identical after extraction.
Real database INSERT aborts and after-real-insert exceptions retain actual owner
rows plus independent durable observations, checked before fault-fixture cleanup
can hide a missing rollback. Retry closes once. Two independent same-basis
closers record one success, one StatusConflict and one boundary on each backend.
SQLite records an attempted BEGIN while held; PG records a blocking PID. These
are bounded schedules, not exhaustive concurrency or linearizability proof.

PG implicit success commits pending caller work; ordinary failure rolls it back.
Managed/TPC calls can leave closed status, boundary and caller writes pending
when commit then rollback are forbidden. The actual baseline and unchanged
oracle retain both pending and independent durable states until caller-owned
outer/TPC rollback. This extraction preserves that existing limitation; it does
not provide safe composition or a new early-refusal policy. SQLite caller
protection remains the separately accepted #2779 behavior.

The corrected full local run passed 2,742 tests (4 skips, 14 performance cases
deselected) in 259.93s. Full disposable PG passed 1,409 tests in 101.18s; all 82
cleanup instances across 4 fixture names reported zero remaining rows.


Eight ordered histories are extracted from the completed full JUnit runs, not
retrospectively attributed to the baseline. The baseline's separate eight
observations and explicitly labeled function AST hashes remain in its own artifact.
An initial isolated-cache local run failed one harness dependency check; the
normal-cache exact case and corrected full run above passed without test changes.
The first post-PG command's duplicated quiet flag suppressed the summary, so its cleanup checker
failed despite zero failed XML cases; it is not the passing gate above.
Independent source reviews were clear. Result/context packets retain exact
commands, current source/oracle hashes and raw-run digests; these establish byte
identity and declared run provenance, not external origin authentication.

The broad captures above ran before the final base update to accepted
`777c19f161ca85586e7fd01b7313c8a95c33a9c5`. Six refactor-owned symbol ASTs
and all three frozen oracle files are unchanged. Combined affected checks and
exact-head CI qualify the final combined source; the earlier full captures
remain historical, not relabeled as post-update runs. Combined local checks
passed 351 tests in 28.63s; combined PG passed 112 tests in 5.65s with 21 cleanup
instances reporting zero rows.

No new provider contract, schema/API/IDL, status/kind/activation policy,
maintenance atomicity, physical crash qualification or deployment is included.
Parent #2150 remains open; exact-head canonical CI remains a publication gate.
