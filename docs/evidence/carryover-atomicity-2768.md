# Carryover transaction evidence

Delivery: sprintctl#2768, child of the unfinished repository-layer item2150.
Execution contract: native Release
`6093199e2ec833606e42d7a3e6cd4a227cdd4d563368bcc10fa32ba445c51526`
and No doc event5502. Implementation base:
`3f9cc86122ed1a67572c7a6d0241ef805e30ed1e`.
The resulting contract is [per-item carryover](../protocols/carryover.md).

The retained shared regression failed **three cases on each backend before
source repair**: injected `carryover`, `item-decided` and `carried-out` event
failures left durable partial state instead of the before-state observed by an
independent connection. An exploratory target-event failure left an open source
and one target copy without its carryover event; retry created a second copy.
SQLite's red run took1.44s; disposable PostgreSQL16.15 took1.53s.
Those initial hooks refused before insertion; the final expanded event cases
raise after actual insertion and require the same durable before-state.
The minimized trace is retained in
[`carryover-counterexample.json`](../../verification/fixtures/carryover-counterexample.json).

The repair shares the workflow and binds explicit noncommitting creation
participants to one backend-owned transaction. Existing track/item query and
validation cores, Decision admission/consequences, generic-event admission,
canonicalization and standard decided-event payloads are reused. Public CRUD
adapter insertion defaults still commit. No connection-wide commit suppression
or copied terminal-transition logic was introduced.

Final verification:

- **2,601 passed, 4 skipped, 1,260 deselected**: complete local non-PostgreSQL,
  non-performance gate,367.09s.
- **1,246 passed**: complete disposable PostgreSQL16.15 gate,78.18s;
  76 cleanups across four fixture names, all with zero remaining rows.
- **44 passed**: final SQLite carryover contract, including caller-transaction
  refusal,6.32s.
- **48 passed**: final disposable PostgreSQL16.15 focused contract, including
  active lease/awaiting-report consequences and pending, managed and two-phase
  transaction boundaries,1.07s. One cleanup completed with zero remaining rows.
- **285 passed**: affected SQLite core, maintenance, Decision, reservation and
  carryover selection before the last blocked-writer test was added,47.72s.

The full local gate uses `UV_OFFLINE=1` with the prewarmed cache for the existing
auxiliary PyYAML workflow parser. No dependency or test is changed, and this
does not qualify network installation availability.

The shared contract includes28 participant/event fault cases per backend,
crossing new/preexisting target tracks and absent/preexisting source Releases.
It also covers three source states, current stale-source selection outcomes,
planned/active/closed targets, generic-event admission refusal, earlier-item
durability when a later item fails, and competing same/different target writes.
An additional held-source history observes the competing SQLite `BEGIN
IMMEDIATE` attempt or PostgreSQL's actual `pg_blocking_pids` wait before any copy
exists. After the holder carries and commits, the waiter returns no new copy.

PostgreSQL precommits its implicit connection transaction under the existing
committing-public-API convention. Actual tests distinguish caller-pending writes
that become committed before an injected carried-item failure from managed or
two-phase outer transactions refused before any copy, with their pending state
left under caller control. SQLite instead requires an idle connection. This
difference is documented; no general caller-transaction composability or
unrelated-write parity is asserted.

All sources are temporary. No shared database, credential file, production
artifact, schema migration, served endpoint, catalog/IDL, grant, installation or
deployment was changed. Parent2150 remains open for its larger thin-driver and
parity criteria. Tests establish bounded examples and concurrent application
invariants, not crash/power-loss coverage or a distributed consistency proof.
