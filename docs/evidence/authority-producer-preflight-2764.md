# Bounded authority producer preflight

Delivery: sprintctl#2764, a child of sprintctl#2096. Base:
`072d0338c77848252d4986a0e6e15b9432a2a085`. Original work authorization is
parent No doc event5399/reservation339; the scoped child is recorded by
event5469/reservation351. Parent update5470 retains the remaining requirement.

`authority doctor --served` now composes existing authenticated owner reads
with a bounded local SQLite read transaction. It reports selected permission,
stream, quarantine and immutable-prefix checks as pass/fail/unknown. It never
initializes a missing outbox, repairs state, allocates a sequence, submits a
pending command, or recreates retired claim/credential paths. Strict diagnostic
`0700` checks leave the existing runtime's deliberate `02700` tolerance intact.
SQLite WAL/SHM reader bookkeeping can occur despite the data transaction being
read-only.

Cursor equality alone is insufficient: the diagnostic compares the selected
stream's full available committed prefix using the owner's portable-record
validator. Missing records/cursors, limits, unavailable sources and changes
during the reads remain unknown. Content divergence fails. Required columns,
stream cells, stored bytes, pages, receipt reads and SQLite VM work are bounded;
unknown extension columns are not materialized. Duplicate JSON keys and decoder
recursion cannot conceal a quarantine or produce a successful diagnostic.
Legacy non-UUID observation identities remain valid without becoming paths.

An explicit reservation ID enables one ordinary activity touch after clean
selected checks and advisory actor/session validation. The result is checked
again. A lost response or observed actor/session mismatch leaves the binding
unknown; same-session reassignment can race these advisory observations. The
report names the selected ID and whether the write was attempted; there is no
automatic retry. It creates no synthetic work and grants no authority.

Completion is limited to this usable aid. A successful touch proves only that
specific invocation, not permission for `work.lifecycle.arbitrate`,
`work.batch.apply`, or other operations. The existing owner API has no read-only
per-operation authorization introspection. Parent2096 remains open for its
required command-authorization criterion. Neither an unimplemented exact-replay
idea nor catalog presence closes that requirement. The diagnostic is not a
migration/cutover gate.

Verification:

- Baseline CLI regression failed with exit2: `No such command 'doctor'`.
- Final rebased focused diagnostic, producer, recovery, routing, documentation
  and install checks: **131 passed** (Python3.13.16/pytest9.0.2).
- Separate Python3.11.16 environment: **79 passed, 11 skipped**. Skips preserve
  the existing Python3.12 minimum for actual served CLI configuration.
- Final disposable PostgreSQL16.15 roundtrip: **1 passed**, one fixture cleanup
  with **zero remaining rows**. It compares a real ingested owner record through
  WorkApplication with the local read-only snapshot, without live HTTP or grants.
- Versioned context/result and repository dispatch validation passed.

One deterministic two-connection SQLite history commits an append between the
reader's stream and record queries. The reader returns the old complete prefix;
the writer observes its newer committed sequence. Injected record-INSERT and
counter-UPDATE failures leave both count and counter unchanged. These are
bounded application-invariant checks, not distributed linearizability proof.

Independent optimistic review caught cursor-only comparison, nullable corrupt
metadata, unbounded extension columns and ambiguous JSON in early drafts. The
final implementation and tests address those findings. A test synchronization
hook was narrowed after a new SQL size guard moved its earlier match before the
intended overlap; the final retained history asserts the exact overlap point.
Deep invalid receipts may fail validation or exceed decoder recursion; both
remain controlled non-success results.

No runtime schema, catalog operation, grant, global installation or deployment
was changed. Tests use temporary data and disposable PostgreSQL only. Live
operation-specific authorization and future contract migration are unqualified
by this delivery.
