# Recovery provenance criterion review: sprintctl#1234

This is evidence for an existing implementation, not a new schema proposal or
a terminal Decision. Native #1234 remains pending with its #1238 dependency
until owner review. Its original [recovery-export plan](../plans/1219-recovery-export-plan.md)
is preserved. The superseded v3 plan is implementation history, not authority
to reopen a retired claim protocol.

Native events **2741** and **2752** already record delivered provenance and
withheld closure. This review refreshes that evidence while preserving the
existing sequencing decision and #1238 dependency.

Implementation landed in
[`c2a36d27d3e54215c635110ac8bb9634abb5938a`](https://github.com/bayleafwalker/sprintctl/commit/c2a36d27d3e54215c635110ac8bb9634abb5938a),
“add the repo-level recovery record (V3-7 / #1234)”. Current local verification
includes source `20e120fb54b51bef001e4d5d57437388e8b8bc58`; the disposable
PostgreSQL recovery run and final-insert fault probe used
`4a00d182a2ac96a844c2518ac1e7823e7f53d321`. The recovery implementation is
unchanged between those baselines; the intervening repair concerns reservation
transactions.

## Native criteria and current evidence

| Criterion | Implemented evidence | Qualification or remaining limit |
|---|---|---|
| One repository-level provenance row rather than per-sprint synthetic events | SQLite migration 21 and PostgreSQL migration 11 add `recovery_record`. `write_recovery_snapshot` inserts one row when provenance is supplied; the production recovery CLI always supplies it. | Direct helper calls may omit provenance for compatibility; those calls deliberately create no record. The record is this destination database's provenance, so source recovery records are not copied into it. |
| Time, source identity, version and per-table counts | Fields include `recovered_at`, `source_repo_id`, `schema_version`, `source_row_counts`, `reservations_interrupted`, `claims_closed` and `created_at`. | Source identity here is the configured repository ID, not a credential, principal or endpoint attestation. `schema_version` is the destination local schema version. Counts describe recovered source rows and exclude the destination provenance row. |
| Provenance commits with restored data | `write_recovery_snapshot` uses one SQLite `BEGIN IMMEDIATE` transaction and commits after both snapshot rows and the provenance insert. Exceptions roll it back. Existing schema-drift rollback and a final-provenance-insert fault both leave no data or record. | This is one destination SQLite transaction. PostgreSQL source reads and destination SQLite are not one distributed atomic transaction. No physical power-loss or filesystem durability campaign ran. |
| New recoveries stop generating per-sprint `recovery.completed` markers | Local and real PostgreSQL-to-SQLite tests assert zero newly synthesized markers, exactly one destination record and one-for-one source event counts. | Existing historical `recovery.completed` events are preserved, not rewritten. |
| Recovery invalidates coordination ownership | Active advisory reservations recover as `interrupted`, with count recorded in `reservations_interrupted`; they are not expired by a clock. Archived active claims are closed and their legacy tokens cleared. | Archive-only claim columns still exist. The native description's later token-stripping deletion is conditional on column removal; that historical cleanup precondition is not met. Do not delete defensive archive handling or close #1238 from this audit. |
| Doctor reports provenance | `doctor._local_schema_probe` exposes `recovered_from`, including source and counts; tests cover both structured and human output and an older database without the table. | This qualifies local recovery provenance reporting, not connectivity, authority or deployment readiness. |
| Drift fails closed and PostgreSQL stays read-only | Recovery checks exact per-table column sets; real disposable recovery tests preserve IDs, refs/dependencies and integrity, and use the existing explicit recovery entry point. | Normal CLI resolution still refuses retired direct-remote mode. The explicit recovery helper is not a fallback for served writes and does not restore live mutation authority or secrets. |

The production source boundary is `sprintctl/commands/db.py` → read-only
`pg.recover_repo_snapshot` → local `db.write_recovery_snapshot`. There is no
second production caller that silently omits provenance. PostgreSQL's matching
relation supports the schema/layout boundary; this recovery command does not
write a provenance row into the source authority.

## Separate maintenance recovery observations

`work.maintenance.recovery-record` calls the maintenance capability store's
`append_recovery_record`, retaining an observation/requested-command reference
under `maintenance_capability_recovery`. It reports no authority and cannot
advance the capability. It is **not** the repository `recovery_record`, not a
replacement for the recovered database's provenance and not evidence that a
recovery restored live authority. Similar names must not merge these contracts.

## Executed checks

- Recovery and doctor local tests: **43 passed in 3.97 seconds** at `4a00d182`.
  The same 43 checks also passed in the current `20e120f` combined cohort of
  188 local tests; the other 145 checks belong to #913's coverage audit.
- Disposable PostgreSQL **16.15** recovery cases:
  `scripts/pg-disposable-tests.sh -k TestRecoverFromRemote` — **2 passed in
  1.23 seconds**, 1 fixture cleanup, zero remaining rows. This is a private
  localhost cluster and temporary destination files, not the shared authority.
- Existing tests include
  `tests/test_db_recover.py::TestProvenance`,
  `tests/test_doctor.py::test_local_schema_probe_reports_recovery_provenance`,
  `tests/test_doctor.py::test_recovery_provenance_reaches_the_text_report_not_only_json`
  and `tests/pg/test_remote_recovery.py::TestRecoverFromRemote`.

An additional local probe used `tests.test_db_recover._snapshot()` in a fresh
temporary database and called `write_recovery_snapshot` with a valid
`recovered_at` but `source_repo_id=None`. The injected fault was the final
provenance insert's `NOT NULL` constraint, after snapshot inserts. It raised
`sqlite3.IntegrityError`; subsequent counts were:

```json
{"sprint": 0, "track": 0, "work_item": 0, "event": 0,
 "recovery_record": 0, "legacy_import_gate": 0}
```

This probe is an executed example, not a newly committed test or an exhaustive
crash proof. No new product, schema, API, grant, deployment, live recovery or
credential inspection occurred. Owner review must distinguish the delivered
provenance behavior from the unresolved sequencing/archival-cleanup dependency
before any native terminal decision.
