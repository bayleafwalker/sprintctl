# Shared locked item-status workflow

Bounded child `sprintctl#2784` of unfinished repository refactor `sprintctl#2150`.
Native scope #5624 and oracle freeze #5644 govern this source change under
Release `d24c6b3f1760b18c774af9c342b3d7b5f89c9cd7db07342e862e6aa8f88b8949`.

The existing SQLite and PostgreSQL helpers repeated the locked domain sequence:
read current item, compare basis, validate transition/reason/blockers, write status
or an accept Decision, then perform any pending-release consequences. A private
workflow in `workitemcore` now owns that sequence. Immutable private participants
supply noncommitting backend operations. Public wrappers still own their original
validation, guards and transaction policies; SQL, tenant predicates, timestamps,
row locks and PostgreSQL lease/report consequences remain backend-owned.

The paired oracle ran before product edits: 53 shared examples per backend plus
17 PostgreSQL cases. SQLite also ran 13 accepted caller-guard regressions: 66 local
tests passed; 70 disposable PostgreSQL tests passed and cleanup was empty. The
oracle covers the status matrix, error precedence, all done blocker resolutions,
actor defaults, session filtering, opaque callback order and private composition.
Real after-write, Decision, reservation-event and final-release exceptions observe
pending owner state and independent durable state before cleanup; retry succeeds.
The private owner writes a pending assignee before delegation to expose premature
commits. Stored timestamps/native types remain visible; volatile age/stale display
values do not participate in durable equality. Timestamp ties impose no new order.

PostgreSQL retains its existing caller behavior: successful public mutation commits
implicit caller writes, ordinary handler failure rolls them back, and syntax errors
before the handler leave them pending. Managed/two-phase commit and rollback can
be forbidden after domain writes, leaving effects pending until the caller cleans
up. This is compatibility evidence, not a new composition guarantee. SQLite keeps
its separately accepted idle guards. An empty PG lease-ID list remains present;
SQLite omits that field. An accept can reject an awaiting report as unverified;
it does not invent verification.

Independent oracle review was clear after four test-only robustness corrections.
The three oracle files stayed unchanged through extraction. Nine public/adjacent
transaction and effect primitives also stayed unchanged. Post-extraction local
oracle/guard checks passed 66 tests; affected PG passed 309 tests with seven
empty cleanup checks. Full local passed 2,855 tests (4 skips, 14 performance
cases deselected) in 305.90s; full PG passed 1,479 in 92.87s with all 83
registered cleanup checks empty. Fourteen sequential observations were extracted
from the completed full JUnit runs. Shared snapshots include native type metadata;
PG lease/report extension timestamps serialize as strings. There are zero new
concurrent schedules. These broad captures precede the final base update;
combined regression checks passed 170 local tests and 371 PG tests, with
nine PG cleanup instances empty. Nine protected owner ASTs, four candidate
symbol ASTs and all three frozen oracle files remained unchanged. Exact-head
canonical CI remains a publication gate for that later source. No new concurrency schedule, physical crash,
provider/API/schema/IDL, deployment or parent completion is qualified here.
