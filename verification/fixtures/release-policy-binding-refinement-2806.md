# Release ref canonicalizer binding refinement before product extraction

This supplements the unchanged governing Release plan5743f6adffcc407f468fe8670aa852b1da0b27c2733d02fec1335e54ec5a8299 for sprintctl#2806. Source remains accepted1ec78fa and no product edit has occurred.

The original PG expression `canonical_context_refs(cur.fetchall())` binds the pure canonicalizer after the actual ref SELECT executes but before fetchall attribute evaluation and invocation. SQLite first materializes its fetched dict list and binds the canonicalizer afterward. Preloading refs then resolving the canonicalizer in common code would change PG timing.

Add one mandatory private keyword `canonicalize_refs: Callable[[Iterable[Mapping]], list[dict]]` to the planned `_freeze` seam. Backend wrappers pass the function bound at the original point: PG saves `_releases.canonical_context_refs` immediately after SELECT and before fetchall; SQLite saves it after fetch and dict materialization. No import-time default, eager pre-SELECT binding, additional read, new transaction, or other domain policy. Core owns `context_refs = canonicalize_refs(refs)` followed by unchanged digest and once-only insertion-value supplier sequencing. All original eight backend signatures, owner/cursor/normalization and query rules remain fixed. Existing releases.py stays unchanged.

The transparent test-owned oracle executes the real ref SELECT, then rebinds the module canonicalizer at SELECT completion and again during actual fetch. PG must call the SELECT-rebound function; SQLite must call the fetch-rebound function. Both wrappers call the original pure function and produce the independent literal expected digest on valid persisted data. This is method-binding characterization, not a new public arbitrary-proxy contract.

Before product editing, run positive cases on both backends and finite wrong controls for capture before SELECT (both) and capture after fetch (PG). Retain original prior oracle bytes and controls. If earlier controls are reused, independently prove every selected test/helper/fixture AST unchanged and declare their original complete-file hashes; do not relabel old whole-suite counts. Refresh exact final source/oracle pins and repeat combined before qualification on final oracle before native freeze.

The original plan's null-label example was a development setup error: persisted ref.label is NOT NULL. Actual stored ref tests use schema-valid empty/duplicate labels; any None-value/empty-row characterization is explicitly test-owned, not invented legal persisted state. Empty public description edits are refused; historical storage blank fixtures are labeled and use no fictitious edit event. These are oracle corrections, not product repairs or scope changes.

Dependency guard policy and backend-peer imports remain explicit separate parent2150 work. No API/schema/authority/credential/runtime/deployment or parent completion is added.
