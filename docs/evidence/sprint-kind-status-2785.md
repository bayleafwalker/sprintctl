# Shared public sprint kind/status sequencing

Bounded child `sprintctl#2785` of unfinished `sprintctl#2150`, frozen by native
No doc decision #5637 under Release
`56e0b36efecb24043b5036bf1a8b28a47298f950a41f893c6265ac2334bf33ae`.
Actual baseline: accepted `9d2add58002047990948dd4f270917f2e53eda6c`.

Two private workflows share public kind CAS and generic status decision
sequencing. Kind keeps its strict ID/kind/basis prevalidation, original row
overlay and prepared kind_revision. Generic status keeps different validation,
returns None and emits no event even when closing. Backend participants retain
fixed SQL, tenant binding, locks, normalization and cursor lifetime. Public
wrappers retain original validation, accepted SQLite caller guard delegation,
BEGIN where present, commit and rollback. SQLite get_sprint and transition hooks,
PG inline transition globals and exception ownership remain dynamic.

A same-kind current-basis request still executes UPDATE and leaves its basis unchanged;
two such recorded competing requests both succeed. State-changing kind and
status races each produce one success and one StatusConflict. Kind basis includes
lifecycle status; status basis excludes kind, so current archive admission still
matters. These are state comparisons, without monotonic revisions or ABA proof.
The existing CRUD helper, hosted activation, explicit close and maintenance
workflows remain separate and unchanged.

The frozen paired oracle passed BEFORE product changes: 28 SQLite and 38 actual
PG cases. Those three oracle files remain byte-identical afterward. Existing
kind-change/archive-activation helpers and accepted caller oracles are reused.
Real BEFORE/AFTER UPDATE aborts check owner transaction state and owner/durable
rows before fixture cleanup. A separate result-construction exception retains
actual post-UPDATE rows, then observes rollback and retry. Twenty-two full-run
ordered histories retain six actual competing schedules, fourteen fault/retry
observations and two representative PG managed/TPC caller observations.

Normal implicit PG success commits pending caller work; in-handler failure
rolls it back. Representative managed-kind/TPC-status calls leave target/caller
changes pending when explicit commit and rollback are forbidden, until caller
cleanup. Pending and independent durable states are retained separately. This
preserves existing limitations; it adds no safe composition or early refusal.
SQLite's observed attempted BEGIN and PG's measured blocker PID support different
bounded observations, not exhaustive concurrency or fairness guarantees.

Full local: 2830 passed, 4 skipped, 14 deselected in 389.91s (0:06:29)

Full disposable PostgreSQL: 1447 passed in 87.11s (0:01:27). pg-cleanup-report-check: ok: 83 fixture cleanup(s) across 4 fixture name(s).

Focused checks separately passed 76 local and 131 PG tests with 21 cleanup instances
empty. Source review was clear. Context/result packets retain exact commands,
current byte hashes, separately labeled baseline AST hashes and raw-run digests.
Baseline 22 observations and full 22 observations stay separate; the extractor
selects only this test module from completed JUnit files. Declared provenance
and hashes do not authenticate external origins.

No new provider contract, schema/API/IDL, permissions, lifecycle policy, physical
crash proof or deployment is included. Parent #2150 stays open; exact-head CI is
still required before publication.
