# Proposed F13 verification slice under sprintctl#1131

Planning only: no child registration, source/test edits, native mutation, or PostgreSQL execution. Coordinate with native F02 owner before registration. Product source must remain unchanged; discoveries requiring repair return for separate scope and authorization. This is useful existing recovery verification, not a new projection design.

## Governing scope and observed source

Fresh served read of sprintctl#1131 from its real repository succeeded using the existing workstation profile (no credential inspection). Item remains pending, empty description, no active reservation. Original event1008 explicitly requires runnable contexts for all14 ADR failures, assertions for all9 invariants, and detection of seeded invariant violations; product repair/protocol implementation are excluded. Updates5520/5771/5775 retain formal/refinement, snapshot and applicability limits; 2791 and2803 are done, 2807 active. A future child qualifies only the bounded F13 behavior below, with F07/I04 support where actually exercised. Parent remains open.

ADR docs/plans/adr-outbox-sync-model.md SHA256 bbfb30b9d7b7c5802dc2852529464d6ccd79ccf9567f241b5d06778a4ef4fa1d: F13 projection schema upgrade and rebuild. F14 trusted snapshot/base-offset recovery is separate. Whole-business shadow parity I09 is not envelope-cache equivalence.

Observed accepted source HEAD1ec78fa9a2f1dbacc4d5ec832c14b961e09abc12; refresh accepted base, actual imported paths and hashes before any future build. These are observed source pins, not external source authenticity:

- sprintctl/projection.py: 0a267ff5c85b93d033f3d1055ceb21db7598e52582f16c96d7208b0be03ff7b1
- sprintctl/sync.py: f8d5dd2c0cc3c2601822a5912e8cf83987629bade0d8f1e1609debfbf3602fa6
- sprintctl/pg.py: a95472c2b2b5e052880808b7b67407c26d05b972d7978fa8c03b71389b1eb6d9
- tests/test_projection.py: 1180ac72665e34565a62cea032295196a5283e5188c2e6abfedad7ef0700e8d3
- tests/pg/test_outbox.py: 3567f7a90093adfaac01bc1dcb7bf23e443f6dcea31ae9fcdb9a618d220c534d

Current gap: no tests reference public sync.rebuild_ingest_projection. The existing PG test named test_disposable_remote_history_rebuilds_projection only opens an empty cache and applies records. SQLite old-schema test proves refusal but never completes upgrade by rebuild. Existing replacement and suffix-preservation tests use authored in-memory history. Supplement2803 honestly excludes actual rebuild/schema upgrade. This is not a missing-PG-rerun-only claim.

## Fixed interface and ownership

Call untouched sync.rebuild_ingest_projection(real_PgStore, path, batch_size=2), which reads actual pg.get_ingest_high_water and actual pg.list_ingested_records, and delegates to untouched projection.rebuild_cached_projection. Interpose only observational synchronization/fault wrappers around existing PG functions; always call original actual PostgreSQL implementation before returning/raising. Never substitute fabricated remote pages in the primary positive/race cases, fake the high-water, or duplicate rebuild orchestration in tests.

Use registered disposable PostgreSQL scopes and two independent connections where needed, temporary SQLite paths, public producer append/ingest for initial data. No live PG URL, service changes, shared workspace events, migrations, new provider or new API. Limit one repository test scope and one producer with3 initial records (H=3), optional fourth later record; a second registered repo with distinct authored data supplies static tenant exclusion. All local cache connections are closed before replacement; no concurrent reader/rename semantics claim.

The supported fixture is ingest-cache rebuild only. Output must explicitly include current metadata, rebuilt ingest records/watermark, empty cached_authority_decision and authority watermark0/NULL. Do not silently claim preservation of populated authority caches: this wrapper does not fetch authority decisions. Do not seed populated authority state merely to invent a preservation obligation; if governing current documentation promises it, stop for owner review.

## Minimal actual-backend cases

1. Old schema to current schema: create the exact legitimate schema-v1 fixture already used by test_schema_v1_cache_fails_closed_until_atomic_rebuild, retaining its original bytes. Verify old open/freshness reports schema-upgrade-required and normal apply refuses. Close it. Ingest3 independently authored observations into actual PG. Call public wrapper, paginated. Assert current schema/repo binding, complete expected rows and watermarkH, full raw state, no sibling remaining, old unsupported metadata replaced. Reopen via normal product helper and independent raw SQLite; both agree. No historical data recovery claim for v1 data not present in remote source.

2. Successful current-cache replacement: start closed v2 live cache with a different legitimate stale manifest. Rebuild from actual contiguous PG history. Exact expected complete state replaces it. Preserve distinction between full replay and business-state migration.

3. Captured-high-water remote advance: independent reader and writer connections each actually measure READ COMMITTED. Record actual get_ingest_high_water resultH=3. At first page wrapper after capture, signal independent writer to append/commit fourth record through public ingest. Wait for actual commit acknowledgment and independently read highwater4, then allow real page reads. With batch2, second actual page contains offsets3 and4; output must contain exactly1..3 and watermark3. Later ordinary catch-up of actual offset4 converges to all4 without duplicate/skip. Assert recorded order capture3 -> writercommit4 -> actual page[3,4] -> rebuildreturns3 -> catchup4. This proves bounded high-water behavior, not a shared PG snapshot or network partition. Finite barrier/thread deadlines and separate connection owners; no synthetic sleeps as ordering proof.

4. Partial sibling then remote-read failure: current live cache closed and byte-hashed. First actual page completes and applies to sibling. On second actual PG page read, capture result and actual sibling first-page rows/watermark via independent bounded read, then inject a named exception before returning that page. Require exact exception, original live file bytes and all persisted state unchanged, sibling removed, no successful result. Remove fault; public wrapper retry converges. Raise after a real second query, not before any rebuild work.

5. Retained-prefix unavailable: register isolated PG fixture, ingest actual1..3 and record unchanged stream/cursor metadata. Deliberately remove offset1 ONLY within that disposable repo as explicitly labelled retention fixture mutation; actual PG highwater remains3 and actual list starts2. Public wrapper must raise ProjectionGapError(expected1,received2), preserve original closed live cache byte-for-byte and full-state-equivalent, remove sibling. This is safe refusal, not trusted snapshot restoration. An empty-page-below-H variant may use deletion of all fixture ledger records with captured cursor retained; label deliberate fixture mutation and exact expected/received failure. No new retention product API implied.

6. Empty source: separately registered empty repo highwater0, old cache closed. Public wrapper returns watermark0/NULL and creates correctly bound current empty ingest-cache schema with explicit empty authority domain. This tests current behavior without pretending an empty source preserves old records.

7. Seeded oracle falsifiers: a valid output must first pass SAME raw invariant checker. On independent copies, separately corrupt record_json without digest update, record_sha256 only, watermark ahead, or retain internally consistent partial new population while watermark saysH. Each must fail its intended assertion, never an unrelated parsing/shape guard. Detect wrong repo binding and non-empty unexpected authority rows separately if existing fixture dimensions make these necessary. Mutations are verifier detection controls, not automatic product detection/repair.

## Independent full persisted oracle

Read all five SQLite tables and schema shapes through independent normal raw SQLite, with finite busy/progress/owner subprocess deadlines. Check cached_ingest_record(repo_id,ingest_offset,record_json,record_sha256,received_at), cached_ingest_watermark(repo_id,ingest_offset,advanced_at), cached_authority_decision, cached_authority_watermark, cached_projection_meta in full. Verify exact row count/offset continuity1..H, exact repo identity, all immutable remote envelope fields, canonical JSON and independently computed SHA256, and absence of unrequested records. Authored inputs are known answers; producer UUID/generated timestamps are pinned to observed initial source identity and validated representation/process interval. Don't use sync._cached_record or projection encoding helpers as oracle. Record actual PG ledger rows and highwater separately so comparing public output to itself is not the only check.

received_at/advanced_at are generated local times: validate canonical UTC representation, actual interval and correct NULL/non-NULL state, not invented fixed equality. For failure cases compare all values exactly to pre-call state and closed main-file digest. Include SQLite integrity_check. Reading must not initialize/migrate the fixture or alter baseline bytes before preservation checks. Use existing raw helpers where suitable, but keep expected manifest authored independently.

## Evidence and gates

Before edits: source-owner review, ownership/no-overlap with F02, native child/NoDoc/frozenRelease and accepted base; then isolated worktree/frozen environment. First run existing SQLite rebuild/freshness tests and the actual PG fresh-cache example to retain baseline; no fake RED requirement for a verification-only expansion. New tests must pass unchanged product; a discovered failure is reported with counterexample and separately scoped repair, not weakened expectations.

Proposed files: one shared test oracle/fixture helper if needed, one actual-PG rebuild test module, narrowly scoped local oracle falsifier module, new context/result/trace and evidence document. Preserve immutable23row audit and earlier results; supplementary current mapping only. Context must declare bounds/actors/clock/isolation/fault phases and original1131 limits.

Capture actual runtime/source/import hashes before/after, exact test node sets and commands, page/capture/commit/callback histories, before/after raw row manifests and digests, actual exception and cleanup outcomes. Bound page count (<=4 for positive fixture), records(<=4/repo), actor joins, observer output/timeouts; actual cleanup zero for every registered PG scope. Do not serialize DSNs or credentials. Source-inspected properties remain labelled source-inspected.

Run focused new tests + existing projection/projection-read/sync checks and relevant disposable PG module; appropriate artifact/docs validators. Exact-head canonical CI is publication gate. Final independent review reproduces full oracle/falsifiers/highwater ordering and distinguishes source base from tested files. No deployment. Parent1131, F02, F14, formal native proposal refinement and whole-state I09 applicability remain open.


## V2 execution specification (planning only)

This separate v2 carries the original brief above unchanged, plus exact proposed files and commands. Original file SHA2569c47328ed1b9c4a36a719a6a5605e1718a4f09486ba195ab15c5e89b5747efee is immutable. Engineering owner scope review is CLEAR; native F02 owner reports no overlap. Registration/freeze/build have NOT occurred. No command in this section has been executed as part of this plan.

### Isolated source and environment

Proposed worktree: /tmp/sprintctl-projection-rebuild-f13.
Proposed branch: verify/projection-rebuild-f13.
Dedicated environment: /tmp/sprintctl-projection-rebuild-f13/.venv.
Logs/XML/collection: /tmp/sprintctl-f13-* (outside repository).
Temporary test state: pytest-owned temporary directories and runner-registered disposable PG scopes only.

Wait for accepted2807 AND2806, then refresh origin/main through normal source-owner coordination and confirm their accepted merge ancestry. Register child, record NoDoc and freeze exact accepted base/Release before repository edits. The observed1ec78fa source pins above remain historical; never relabel them as the later accepted base. If the worktree/branch paths already exist, inspect ownership and coordinate rather than reusing/removing another session's state.

Future source setup, after those gates, from /projects/dev/sprintctl:

```bash
git worktree add -b verify/projection-rebuild-f13 /tmp/sprintctl-projection-rebuild-f13 origin/main
```

All following commands use cwd /tmp/sprintctl-projection-rebuild-f13 and normal populated uv cache (no copied virtualenv or fresh empty offline cache):

```bash
uv sync --frozen --extra dev --extra served --extra remote > /tmp/sprintctl-f13-env-sync.txt 2>&1
uv run --frozen --no-sync python -c 'import sys,sqlite3,pytest,psycopg; print(sys.executable); print(sys.version); print(sqlite3.sqlite_version); print(pytest.__version__); print(psycopg.__version__)' > /tmp/sprintctl-f13-environment.txt 2>&1
git rev-parse HEAD > /tmp/sprintctl-f13-source-base.txt
git status --short > /tmp/sprintctl-f13-source-status-before.txt
```

Retain exact uv version, lockfile/project hashes, actual imported product/helper paths+hashes and runtime metadata as well. Frozen dependency setup is not network-install qualification. No package installation/global environment changes beyond the dedicated frozen environment. Any actual DNS/cache/tool failure is retained separately, not treated as a product failure or silently hidden by weakening assertions.

### Proposed repository files and node names

- tests/_projection_rebuild_contract.py: independently authored expected-record builder, all-five-table raw SQLite oracle, schema-v1 fixture and bounded raw manifests; no copied product conversion/fingerprint implementation as expected-value source.
- tests/test_projection_rebuild_oracle.py: `test_projection_rebuild_oracle_accepts_valid_manifest`; parametrized `test_projection_rebuild_oracle_refuses_seeded_violation` with explicit corruption kinds from the original brief. Exact variants finalized by test author/source review before collect freeze, no guessed count.
- tests/pg/test_projection_rebuild.py: `test_projection_rebuild_upgrades_schema_v1_from_real_remote`; `test_projection_rebuild_replaces_current_cache_from_real_remote`; `test_projection_rebuild_stops_at_captured_high_water_while_remote_advances`; `test_projection_rebuild_remote_page_failure_preserves_live_cache`; `test_projection_rebuild_retained_suffix_preserves_live_cache`; `test_projection_rebuild_empty_remote_creates_bound_empty_cache`. An additional empty-page variant is optional only if it adds a distinct falsifier, and must enter the frozen collected set before qualification. New PG cases use existing pg_test_scope and actual approved runner.
- verification/contexts/projection-rebuild-high-water.json: new bounded F13 context, original14/9 scope and child/Release references.
- verification/fixtures/projection-rebuild-expected-records.json: authored synthetic manifest if useful; include exact raw five-table expectations and variable-field rules.
- verification/results/projection-rebuild-FUTURE_CHILD.json and verification/traces/projection-rebuild-FUTURE_CHILD/: replace FUTURE_CHILD with the actually registered native ID, before publication. Store raw per-case manifests/histories, exact collected nodes, stdout/JUnit, cleanup and runtime identity, deterministic extractor if properties require extraction.
- docs/evidence/projection-rebuild-FUTURE_CHILD.md: observed behavior, gates, limits and full-parent residuals. Product/runtime/schema files remain unchanged. Keep earlier audit/results immutable.

Do not use the optional empty-page case to waive/replace the required real-page failure or actual retained-suffix refusal. Existing API/docs govern whole-file replacement and authority reset; full-state oracle must not assert undescribed preservation of old authority decisions.

### Before-build baseline and collection

First retain the existing focused local behavior and actual PG fresh-cache example before adding new tests; product remains unchanged throughout this verification-only child. Literal baseline commands:

```bash
uv run --frozen --no-sync pytest tests/test_projection.py tests/test_projection_reads.py tests/test_sync.py -q --junitxml=/tmp/sprintctl-f13-before-local.xml > /tmp/sprintctl-f13-before-local.txt 2>&1
PATH="/tmp/sprintctl-projection-rebuild-f13/.venv/bin:/nix/store/lkcp4yn4dmkhmngwa363wwqyhhk5rzs4-postgresql-16.15/bin:$PATH" UV_FROZEN=1 UV_NO_SYNC=1 timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh -k disposable_remote_history_rebuilds_projection --junitxml=/tmp/sprintctl-f13-before-pg.xml > /tmp/sprintctl-f13-before-pg.txt 2>&1
```

The PG runner already supplies pytest `-q -m pg`, all PG files and tests/test_work_application_pg.py; do not append extra `-q` (cleanup-summary parser relies on the normal runner format). It creates its own private cluster and injects its own test URL. Never supply/inherit a live DB URL or bypass registered cleanup. Escalated execution is required for the established owned disposable-server path; inability to execute is could-not-check, not an empty successful campaign. UV_FROZEN=1 and UV_NO_SYNC=1 are the runner's equivalents of uv --frozen --no-sync; uv help confirms both environment controls. Measure actual PostgreSQL major/version at runtime; the cached16.15 path is the known candidate, not proof of execution.

After new tests are authored and reviewed, collect the exact proposed suite without execution, retaining full outputs:

```bash
uv run --frozen --no-sync pytest --collect-only tests/test_projection_rebuild_oracle.py tests/test_projection.py tests/test_projection_reads.py tests/test_sync.py -q > /tmp/sprintctl-f13-collect-local.txt 2>&1
uv run --frozen --no-sync pytest --collect-only -m pg tests/pg/ tests/test_work_application_pg.py -k 'projection_rebuild or disposable_remote_history_rebuilds_projection' -q > /tmp/sprintctl-f13-collect-pg.txt 2>&1
```

Derive a data-only expected node set from actual collection, review it, then freeze before the final campaign. No predicted passing/skipped/cleanup counts. Ensure selection contains the new module plus the intended existing PG example and no unrelated matching nodes; unexpected matches require explicit selection refinement and recollection, not post-hoc exclusion from the result.

### Final targeted execution

```bash
uv run --frozen --no-sync pytest tests/test_projection_rebuild_oracle.py tests/test_projection.py tests/test_projection_reads.py tests/test_sync.py -q --junitxml=/tmp/sprintctl-f13-final-local.xml > /tmp/sprintctl-f13-final-local.txt 2>&1
PATH="/tmp/sprintctl-projection-rebuild-f13/.venv/bin:/nix/store/lkcp4yn4dmkhmngwa363wwqyhhk5rzs4-postgresql-16.15/bin:$PATH" UV_FROZEN=1 UV_NO_SYNC=1 timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh -k 'projection_rebuild or disposable_remote_history_rebuilds_projection' --junitxml=/tmp/sprintctl-f13-final-pg.xml > /tmp/sprintctl-f13-final-pg.txt 2>&1
python /projects/dev/agentops/scripts/validate_verification_artifacts.py --root . > /tmp/sprintctl-f13-validator.txt 2>&1
git diff --check > /tmp/sprintctl-f13-diff-check.txt 2>&1
```

Choose exact existing docs/artifact-manifest tests after reading their accepted current names; freeze those commands before publication rather than guessing stale filenames here. Run active repository hooks normally. Retain terminal exit statuses through the tool/process owner as well as log hashes. JUnit actual node set must equal the frozen collection set; no unexplained missing/extra/skipped/failed nodes. Runner must confirm every registered cleanup completed with zero rows; retain actual report/count, never infer from terminal0 alone. Capture source/import hashes again after the campaign. If source is rebased later, distinguish historical captures and run reviewed combined affected checks; don't relabel old raw histories.

### Why these gates are adequate

This changes verification assets only, not product SQL/schema/lifecycle/authority/runtime. Focused local execution covers full oracle falsifiers, existing schema/current-cache refusal/replacement, visible read fallback/freshness and synchronization semantics. Actual disposable PG execution specifically covers the missing public rebuild wrapper, real ledger/page queries, tenant scoping, captured-high-water remote advancement, fault preservation and cleanup. Full raw-state checks and independent known answers prevent a returning watermark or matching event IDs alone from passing. Frozen collection/JUnit reconciliation establishes the tested scope; canonical CI supplies the broader compatibility gates without redundant serial full-suite runs solely for packaging.

Before merge require all FOUR canonical exact-head CI jobs green (Python3.11, Python3.12, disposable PostgreSQL, kctl producer contract), current hook/artifact/docs checks, independent source/oracle/packet review, merge-tested-tree equality and native child evidence/Release binding. Do not substitute a green historical head. Final child acceptance must describe only actual measured F13/F07/I04 support. F02 ownership, F14 trusted snapshots, generic formal proposal refinement, whole-state I09 disposition and parent1131 completion stay separate.

## Coordinator command refinements before registration

For the eventual isolated-worktree shell, set these task-specific variables before the setup/run commands above so an inherited environment cannot redirect uv or trigger network resolution:

```bash
export UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-projection-rebuild-f13/.venv
export UV_OFFLINE=1
```

The current accepted repository has `tests/test_docs_integrity.py`; include this literal documentation gate, refreshing only if the accepted-source path actually changes before build:

```bash
uv run --frozen --no-sync pytest tests/test_docs_integrity.py -q > /tmp/sprintctl-f13-docs.txt 2>&1
git diff --cached --check > /tmp/sprintctl-f13-staged-diff-check.txt 2>&1
```

The staged check is necessary after adding new files because an unstaged `git diff --check` does not inspect untracked files. Audit every referenced manifest object against indexed bytes, including exact forced additions for intentionally retained synthetic `.db` files if a global ignore otherwise omits them. Scanner exceptions require precise known-hash/path/line scope and independent location controls. These are publication completeness checks, not changes to F13 product behavior.
