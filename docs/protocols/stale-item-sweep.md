# Per-item stale blocking and audit history

This contract covers the existing local/direct-administration
`maintain.sweep_stale_items` path, bounded child sprintctl#2771 of #2150.
Served `maintain sweep` remains unavailable. It does not make the full
maintenance sweep, reservation interruption or optional sprint closure one
transaction.

The initial read selects active items. The existing explicit `now` and
`threshold` determine initial staleness; initially fresh rows retain their
read-only fast path. Each selected stale item then enters its own writer
transaction. SQLite takes `BEGIN IMMEDIATE`; PostgreSQL locks the scoped
work-item row with `FOR UPDATE`. Under that lock, the workflow rechecks
existence, sprint membership, active status and current timestamp staleness.
Deleted, moved, nonactive or refreshed items are skipped without item/event
writes. It does not replace the existing time policy with a new wall clock.

The existing locked status-transition implementation validates and applies
`active -> blocked`. Its existing side effects, database timestamp policy and
protected work-lease behavior remain unchanged. Generic-event admission and
canonicalization remain in force. The status/timestamp and one
`auto-blocked-stale` event commit in the same per-item transaction. The event
retains the existing actor, source type, idle time and threshold fields.

A failure before commit rolls back that item's transition and event. Earlier
items committed by the same command remain committed if a later one fails.
Retry selects remaining active stale work; it does not return a cached result
or repeat an already committed item's event. A lost commit response can still
leave the caller uncertain; this is not a new retry/idempotency protocol.

Affected output retains the public list-row shape, including `track_name`, and
reports the current **pretransition** item projection. It does not return the
raw lock row or silently change affected statuses to posttransition values.
Concurrent candidates recheck state after waiting: only a writer that still
finds active stale work applies a transition/event. This is a bounded
application invariant, not distributed scheduling or a global snapshot claim.

## Connection ownership

When a selected stale item enters the write boundary, SQLite refuses an active
caller transaction before its rollback handler. Caller state remains under the
caller’s control. PostgreSQL follows the existing committing public-mutation
convention: it precommits the implicit connection transaction, including pending
caller writes, before opening its owned item transaction. Managed outer and
two-phase contexts refuse that precommit before any owned item write, without
this handler rolling back caller state.

Initially fresh rows and an empty selection do not enter that boundary.
However, an initially stale candidate can become fresh, moved or terminal
before the locked recheck; PostgreSQL's precommit has already happened when
that candidate is skipped. These operations are not a composition API for
caller-owned transactions, and backend equivalence for unrelated pending writes
is not promised.

Verification uses temporary SQLite files and disposable PostgreSQL only.
Fault/retry, independent durable reads, stale selections, competing sweeps,
observed waiting writers, caller boundaries and protected-lease preservation
are tested within stated bounds. No power-loss/failover, whole-maintenance
atomicity, production qualification, schema/API change or deployment follows.
