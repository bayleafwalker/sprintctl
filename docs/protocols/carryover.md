# Per-item carryover transaction

Scope: existing local/direct-administration `maintain carryover`, bounded child
sprintctl#2768 of the broader repository-layer item2150. No served mutation
operation or new lifecycle is defined here.

Each selected source item is one transaction. The backend locks and re-reads
the source before creating a target track or item. Missing items, items moved
out of the source sprint, and items already terminal are skipped without
writes. A still-open source uses its current locked title, description,
assignee, track and status. Existing target sprint status rules and copy fields
are unchanged; the copy starts pending with its own aggregate identity.

The transaction contains a newly needed target track, the copy, its `carryover`
event, the source's ordinary `supersede` Decision naming the copy, the standard
`item-decided` event and the source's `carried-out` event. PostgreSQL's existing
Decision consequences for active leases and awaiting reports participate in
the same transaction. Generic-event admission and canonicalization remain in
force. The returned copy is read before commit.

An exception before this transaction commits rolls back its changes, including
new target objects, source state and timestamps, Decision and audit events.
Preexisting tracks, Releases and reservations remain intact. Earlier items
already committed by the same command remain committed if a later item fails.
Retry enumerates the remaining open sources; it does not undo earlier work.

SQLite acquires `BEGIN IMMEDIATE`; PostgreSQL locks the repository-scoped
source with `FOR UPDATE`. A competing carryover waits before it can create a
copy and re-reads the source after the winner commits. A source superseded by
the winner is then skipped, including when the competitors named different
target sprints. This is a bounded application invariant, not a global
linearizability or distributed scheduling claim.

After a lost response, the outcome may be unknown to the caller. A completed
copy and its Decision remain durable; a subsequent command skips that terminal
source and returns only work it newly carries. There is no idempotency key,
cached result, automatic retry or whole-command atomicity guarantee.

## Connection ownership

These operations own their commits and are not a general composition API for
caller-owned transactions. SQLite refuses an already-active transaction before
its carryover writes and leaves that transaction under caller control.

PostgreSQL retains the backend's existing committing public-mutation
convention. Before opening each owned transaction it commits the connection's
implicit transaction, including any caller-pending writes. Therefore such
pending writes can remain committed even if the subsequent carried item fails.
Ordinary prevalidation reads also open an implicit PostgreSQL transaction.
Psycopg-managed outer transactions and two-phase transactions refuse the
precommit before any carryover write; their pending state is not rolled back by
this boundary. No equivalence of caller-owned-write behavior is claimed between
the two backends.

Tests use temporary SQLite databases and a disposable PostgreSQL cluster. The
shared oracle covers injected failures after actual participant/event writes,
independent durable reads, retries, stale selections, later-item failure,
competing targets and an observed blocked writer. It does not simulate power
loss, failover or arbitrary caller transaction composition.
