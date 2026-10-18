# Preserve caller transactions on refused sprint mutations

Bounded child `sprintctl#2779` of unfinished repository-layer item `sprintctl#2150`.
Native decision #5586 freezes the approved scope under Release
`2c729967ab238bf3a00541cdd3c9930584b820f36b8b4b6eca23ac5316eb3685`.

The three SQLite sprint mutation entrypoints owned a BEGIN IMMEDIATE transaction
and rollback handler. A nested caller transaction caused BEGIN to fail, then
the handler rolled back unrelated caller writes. Three actual before snapshots
retain this counterexample. An idle-connection check now runs after existing
pure validators and before each owned handler. It issues no SQL and performs
no commit or rollback. Active callers receive ValueError requiring an idle
SQLite connection; this deliberately replaces OperationalError plus lost work.
Valid idle-path locking, CAS, transitions and lifecycle events are unchanged.

The coordinator-owned oracle ran before product edits: nine nested cases failed
and six pure-validation cases passed. After repair all 15 passed. The oracle
covers each function with writes, explicit reads and inner/outer savepoint
writes; it checks complete target/caller rows, independent durable reads,
no event or total_changes effects, savepoint rollback/release and outer rollback.
Existing pure argument, actor and revision errors retain precedence. Affected
maintenance/application/kind/close-fault regressions passed 257 tests. Independent
review found no issues and independently passed 42 tests.

The context and result packets in `verification/` retain scope, claims, commands
and byte hashes, including actual before-failure output. Full local gate passed 2,697 tests (4 skips, 14 performance cases deselected)
in 255.21s. After a clean rebase onto accepted seeding repair #2778, 301 combined
regression and documentation tests passed in 27.04s. The affected function
and oracle bodies were unchanged. Exact-head canonical CI remains a gate. No PostgreSQL source, caller-composition contract,
physical-crash, schema/API/IDL, deployment or parent completion claim is made.
