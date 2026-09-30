---
doc_id: 2026-09-30-run-continuation
status: proposed
supersedes: none
---

# Run continuation: serving `read_predecessor_context` end to end

agentops#2525 (M1-5, TS-8 second route). Rule source: vuoro
`docs/plans/2026-09-26-e2-e3-shared-contract.md` §4 amendment (2026-09-27)
and `vuoro_mcp_edge.record_tools` / `runs.InMemoryRunRegistry` (the
reference implementation).

## Problem

The Vuoro MCP edge implements continuation (`register_run.predecessor_run_id`
and the read-bucket tool `read_predecessor_context`), but advertises it only
when the record store sets `supports_continuation`. Production composes
`SprintctlRecordStore`, which does not: sprintctl (0.10.x) cannot record a
run's predecessor, and has no operation that reads a run's notes and
evidence back (`work.run.resolve-v1` / `work.evidence.tail-v1` answer only
the run's own binding, and the tail is one item).

## Rules (fixed by the contract; not reopened here)

- Eligibility: the successor shares the predecessor's `workspace_id` AND
  repository. Principal, OAuth client and grant may differ.
- The edge checks `work:read` before calling the registry
  (`authority-required`). The registry checks workspace and repository and
  otherwise refuses with `predecessor-not-eligible`: unknown, malformed,
  other-workspace and other-repository ids share one code and one message.
- Same idempotency key with a different `predecessor_run_id` is
  `idempotency-conflict`.
- Reading: `read_predecessor_context(run_id)`, `run_id` is the caller's own
  run resolved to its exact binding (`run-not-found` otherwise), one hop,
  never through the predecessor's handle. Needs `work:read`.
- Context, not authority: `resolve` is unchanged, so the predecessor's run
  never resolves to the successor.

## sprintctl

- **Schema 20** (additive): `run.predecessor_run_id text NULL`,
  `CHECK (predecessor_run_id IS NULL OR predecessor_run_id <> run_id)`,
  `FOREIGN KEY (repo_id, predecessor_run_id) REFERENCES run(repo_id, run_id)`
  (MATCH SIMPLE: NULL means "no predecessor"; the repo component makes
  cross-repository links impossible at the storage layer). Partial index on
  `(repo_id, predecessor_run_id)`. Idempotent; refuses a pre-existing
  `run.predecessor_run_id` column of another shape. Minimum and maximum
  remote schema become 20 (the coordinated-cutover convention of 17/18/19).
- **`work.run.register-v1`** gains optional `predecessor_run_id`. The
  argument is part of the request digest only when present, so every
  existing replay digest is unchanged; a different predecessor under the
  same key is `idempotency-conflict` through the ledger and the stored row.
  Eligibility is checked inside the idempotent effect (same transaction as
  the insert): predecessor row in this repo with the caller's
  `workspace_id`, else `predecessor-not-eligible` (malformed ids too). The
  run result echoes `predecessor_run_id` (null when none).
- **New `work.run.predecessor-context-v1`** `{run_id}` -> `{repo_id, run_id,
  predecessor_run_id, session_notes[], evidence[]}`. Authority `work:read`,
  read, idempotency not-allowed. Resolves `run_id` to the caller's exact
  binding (`run-not-found`), then reads the recorded predecessor's notes
  (`note_id`, `note`, `created_at`, in `note_id` order) and evidence items
  (the `work.evidence.tail-v1` item shape, in `chain_seq` order). No
  predecessor: `predecessor_run_id` null and empty lists. Listed with the
  admin-shutdown read replays.
- The presence of `work.run.predecessor-context-v1` in the catalog is the
  capability signal: it ships in the same release as the register argument,
  and a consumer must treat it as implying both.
- Version: the package version is left to the release, as for schema 19.
  The next release carries schemas 19 (effect intents, agentops#2541) and
  20 together and is a minor release: **0.11.0**.

## vuoro (vuoro-mcp-edge; vuoro-service pin at release)

- Capability detection, not a hard flip: `SprintctlRecordStore` reads the
  runtime shell's public catalog (`GET /api/catalog/v1`, same client and
  edge proof) and serves continuation only when it advertises
  `work.run.predecessor-context-v1`. A successful read is cached for the
  process (edge and adapter ship in one image); a failed read is not
  cached and answers "not available".
- `ToolSpec` gains an optional async `describe()` that returns the
  definition to list or `None` to hide. `tools/list` and `tools/call` use
  it; a hidden tool is `unknown-tool`. `read_predecessor_context` is hidden
  and `register_run` lists without `predecessor_run_id` when the owner lacks
  the capability; a predecessor sent anyway is `invalid-arguments`, as today.
- Stores that set `supports_continuation = True` (the in-memory reference)
  keep the static path.
- `SprintctlRecordStore.register` sends `predecessor_run_id` only when set;
  `read_predecessor_context` calls the new operation. The edge still
  resolves the caller's own run first (unchanged handler), so "caller's own
  run" is enforced at both the edge and the owner.
- Shared contract §4 updated: the sprintctl store serves continuation from
  sprintctl 0.11.0.
- vuoro-service `composition/adapter-pins.json` repin to sprintctl 0.11.0
  happens in the vuoro-service release (needs the published wheel sha256).

## Tests

- sprintctl (`tests/pg/test_run_continuation.py`, disposable PG): predecessor
  recorded and echoed; cross-principal/client/grant successor allowed;
  other-workspace, unknown and malformed ids refused alike; same key with
  another predecessor is a conflict and the replay without one is
  unchanged; notes and evidence read back in order; only the caller's own
  run can be read (a successor presenting the predecessor's id, or a third
  party presenting the successor's id, is `run-not-found`); the predecessor
  never resolves to the successor; schema 20 shape and migration.
  Catalog-surface tests updated for the new operation.
- vuoro-mcp-edge: against a fake shell whose catalog lacks the operation
  the tool is not listed, `register_run` has no `predecessor_run_id`, and
  calling it is `unknown-tool` (no regression); with the operation, both are
  listed and a round trip through `SprintctlRecordStore` sends/reads the
  right envelopes; a failed catalog read hides the tool and is retried.

## Release sequence

1. sprintctl PR merged -> tag `v0.11.0` ONCE, after this lands (agentops#2568
   step 1 is the pending 0.11.0 cut). origin/main already requires schema 19
   while the shared authority is at 18; cutting 0.11.0 after this PR makes
   18 -> 19 -> 20 one coordinated shared-schema migration instead of two
   cutovers. The authority migration job runs before the new runtime serves.
2. vuoro PR merged -> vuoro-service release (0.1.82): repin work adapter to
   sprintctl 0.11.0 in `adapter-pins.json`, relock, handshake test version.
3. vuoro-cloud, ONE generation: `config/compatibility.json` tenant runtime
   `vuoro-service-v0.1.79` -> `vuoro-service-v0.1.82`, AND a
   `MCP_TOOL_SCOPES` row `"read_predecessor_context": "vuoro:work.read"` in
   `src/vuoro_cloud/oauth_scopes.py`. A row without the runtime is inert; a
   runtime without the row makes the gateway refuse the tool.
   This subsumes the pending 0.1.79 -> 0.1.81 move (Postgres reconnect fix),
   which 0.1.82 contains.
