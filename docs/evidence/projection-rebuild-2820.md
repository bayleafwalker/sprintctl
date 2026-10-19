# Actual PostgreSQL projection rebuild and captured high-water

Verification-only child `sprintctl#2820` covers bounded ADR F13 examples under
unfinished `sprintctl#1131`. The unchanged implementation is accepted commit
`32bc4e6b3b1fa1c369fcd139605d0dff6795488e`; Release
`828e25d93e243de91cc73ec3eeb1a61258326f0a8d2655fa7fdf5b4f312284b8`
binds the item. Native note5893 records the reviewed harness freeze before this
final campaign. Earlier development captures retain their original attribution.

The tests call the actual public `sync.rebuild_ingest_projection` wrapper using
runner-owned disposable PostgreSQL scopes and closed temporary SQLite caches.
They cover schema-v1 upgrade, replacement of a stale current cache, an empty
primary with a distinct foreign tenant, captured high-water under an independent
writer, second-page failure and retained-prefix refusal.

In the concurrent example, the reader captures3, another process commits4, and
the original reader observes4 before fetching actual pages `[1,2]` and `[3,4]`.
The rebuild returns and persists exactly1..3. Ordinary later catchup applies4.
Both PostgreSQL actors measure READ COMMITTED; this is not a shared MVCC snapshot.
A separate control proves the writer reaches its nonce/PID-bound connected stall,
is killed and reaped by its owner, and leaves no backend or additional record.

An independent bounded raw SQLite reader checks all five table column/type/PK
shapes, repository binding, complete canonical15-field envelopes and hashes,
record population, and separate ingest and authority watermarks. Nine persisted
falsifiers fail for their intended reasons after valid fixtures pass the same
checker. Boolean schema versions and floating-point sequences with recomputed
hashes are rejected by exact canonical JSON comparison.

The second-page fault runs after a real query and after the sibling contains the
first page. It preserves the original live bytes and full state, removes the
sibling, and an unmodified retry converges. The retained-suffix case deletes only
its registered disposable offset1 inside an explicitly guarded transaction,
restoring the immutable trigger before commit. The actual rebuild refuses
expected1/received2 while retaining the original cache. This is a safe-refusal
example, not successful trusted-snapshot recovery or a product retention API.

The final campaign passed55 local tests and8 PostgreSQL tests, with two registered
fixture cleanup scopes empty. Exact collected node sets equal the executed JUnit
sets, with no failed, errored or skipped cases. The52 retained raw objects include
seven PostgreSQL captures and nine falsifier captures. The context, frozen source
and import hashes, literal commands, logs/XML, object hashes and result are linked
below. Source and the five previously reviewed files stayed byte-identical through
qualification. Documentation checks and publication gates are separate receipts.

Authority caches are intentionally empty after this whole-file ingest rebuild.
These tests do not establish preservation of old authority decisions, complete
DDL/index/trigger conformance, whole-business shadow parity, concurrent-open-reader
replacement, physical durability, F14 trusted snapshot recovery, or completion of
the original14-scenario/9-invariant parent. No product, schema, provider, service
or deployment changes are included. Four exact-head CI gates, independent final
packet review and tested/merged-tree equality precede native child acceptance.

- [Context](../../verification/contexts/projection-rebuild-high-water.json)
- [Result](../../verification/results/projection-rebuild-2820.json)
- [Harness freeze](../../verification/fixtures/projection-rebuild-harness-freeze-2820.json)
- [Provenance and hashes](../../verification/fixtures/projection-rebuild-provenance-2820.json)
- [Raw capture manifest](../../verification/traces/projection-rebuild-2820/manifest.json)
