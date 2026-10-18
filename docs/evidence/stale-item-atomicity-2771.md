# Stale-item audit transaction evidence

Bounded child sprintctl#2771 of unfinished repository-layer item #2150.
Execution scope is native No doc5534 and Release
`1797d625fa6e0134f8a5462df039e52dec67b2a05ac40c0aaea91e55d94e7322`.
The candidate is based on carryover merge
`bbb35db081532b5c973f32f0bf9e672765005ca9` and implements the
[per-item contract](../protocols/stale-item-sweep.md).

## Reproduction and repair

Before source edits at `e816d907fc1a803cbcfc4c112133a2f3c955e7d7`, the retained
before/after-event-INSERT failure oracle failed **two cases on SQLite**
(0.99s) and **two on disposable PostgreSQL16.15** (0.40s). Independent readers
observed `blocked` with no `auto-blocked-stale` event after the operation failed.
An ordinary retry selected no active item. The minimized symbolic history is
`verification/fixtures/stale-item-counterexample.json`.
An earlier exploratory PG fixture used an invalid connection constructor;
that run is excluded from the product counterexample evidence.

The repair extracts the existing locked status bodies without changing their
validation or effects. Independent review compared their ASTs with the originals
on both backends. Public status wrappers retain their own validation and
commit/rollback behavior. The shared maintenance workflow instead uses explicit
noncommitting participants inside one owned item transaction, rechecking current
state, membership and staleness after locking. It retains the existing explicit
clock/threshold, generic-event admission/canonicalization and pretransition
public output including `track_name`. The unchanged projection oracle caught
that field's omission in the first draft; the existing track read restores it.

Initially fresh rows keep the old read-only fast path. Selected stale rows that
change before their lock are skipped after the current check. PostgreSQL still
precommits before that owned boundary, even if such a selected row is later
skipped. SQLite idle refusal and PostgreSQL implicit/managed/two-phase behavior
remain explicit; there is no caller-transaction composition or unrelated-write
parity promise.

## Executed bounded checks

- Final focused SQLite: **14 passed in 2.70s**.
- Final focused disposable PostgreSQL16.15: **18 passed in 0.59s**, one cleanup,
  zero remaining rows.
- Complete disposable PostgreSQL16.15: **1,264 passed in 81.10s**, 77 fixture
  cleanups across four fixture names, all with zero remaining rows.
- Complete local non-PG/nonperformance: **2,615 passed, 4 skipped,
  1,278 deselected in 290.35s**, using cached dependencies with `UV_OFFLINE=1`.
- Documentation/install checks: **18 passed in 0.03s**; packet validation passed.

After the full gates, independent review strengthened the existing explicit-clock
assertion to exactly **315,662,400 seconds**, rather than a loose age bound.
The final focused gates above reran with that stronger assertion. Product source
was unchanged; offline execution does not qualify network installation. An
isolated clock mutation was caught by the exact assertion; the normal clock
case passed again after that separate process.

The shared 13 cases per backend cover before/after-event faults, ordinary retry,
exact pretransition projection, five stale-selection changes, later-item failure,
generic-admission refusal, competing sweeps, and held-writer refresh/terminal
changes. There are three concurrent histories per backend with independent
connections. SQLite observes the attempted `BEGIN IMMEDIATE` while the holder
owns the writer lock and no result has returned; PostgreSQL observes the actual
`pg_blocking_pids` wait. The waiter rechecks after the holder commits.

Backend-specific cases preserve SQLite caller state on refusal, PostgreSQL
pending-write/precommit, initially-fresh no-op, managed-outer and two-phase
boundaries, and the unchanged active hosted lease across both an event fault
and successful blocking. These are bounded examples/concurrency evidence,
not formal, physical-crash, failover or distributed qualification.

All data is temporary. No shared events, credentials, schemas, catalog/IDL,
served mutation operation, installation or deployment were changed. Whole
`maintain sweep`, reservation interruption, optional sprint closure and broader
#2150 parity remain separate. Reusable intent and results live in
`verification/contexts/stale-item-atomicity.json` and
`verification/results/stale-item-atomicity.json`.
