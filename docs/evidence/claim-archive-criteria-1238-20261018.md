# Live claim removal and archival schema: sprintctl#1238

This records bounded verification at Sprintctl
`e31124de724e4716ffbe71ca021394f742b3ec39`. **#1238 stays pending** with its
#1237/#1164/#1244 prerequisites preserved. No archive/drop policy, schema,
dependency, status or terminal decision is changed by this evidence.

The [current reservation protocol](../protocols/reservation-model.md) is
referenced by repository guidance and supersedes the historical v3 plan;
its status remains **draft**, not ratified by this audit. Current reservations
are advisory, while protected owner work leases are a separate protocol.

## Native criterion reconciliation

| Native criterion | Observed current implementation | Remaining disposition |
|---|---|---|
| Remove the live claim relation and bearer-proof machinery | Commit `9e99625204cd99da865bc1a4a733768cddc8b6e2` drops the live claim relation (SQLite 20 / PG 10). Fresh current SQLite and PG schemas have no `claim` table. Current catalog inspection found no declared work-claim proof fields or `work.claim.*` operations. | Absence of a live claim relation does not mean all historical claim columns or evidence are deleted. Authentication and separate protected leases remain. |
| Remove `claim_token` and `lease_epoch` from both schema dumps | Both backends retain those columns in `claim_history`. PG additionally retains `terminal_recovery_ledger.lease_epoch`, with its linked `terminal_recovery_audit`. The [observed layout](claim-archive-schema-observation-20261018.json) records exact table/column names. | The literal all-storage absence criterion is **not met**. Retaining or removing archival evidence needs an explicit owner/history decision; do not drop it to make a grep empty. |
| Retain rows through migration | The existing SQLite archive/drop migration example was executed with the local verification cohort. Current archive and recovery handling clears old bearer tokens while retaining history. | This run did not qualify a live-shaped PG version-9→10 migration with nonempty historical recovery tables. Fresh PG layout is not a data-preserving upgrade campaign. |
| Use `last_activity_at` and interrupted coordination state | Current advisory reservation rows/handlers and tests track session activity, explicit interruption, release and reassignment. Recovery interrupts active reservations rather than pretending time expired them. | Historical `claim_history` retains its original schema vocabulary; that is not a live heartbeat API. Protected work leases still have their own heartbeat/expiry contract. |
| Refuse conflicting exclusive reservations unless overridden | Current local and real PG examples allow overlapping reservations and report conflicts. Explicit interruption displaces execution reservations; it is not an ownership secret. | The historical refusal/exclusive-admission criterion conflicts with the current advisory contract. Do not restore it or silently claim it passed. |
| Respect retirement sequencing | Native #1238 still has #1237, #1164 and #1244 blockers, and blocks #1234/#1239/#1240. | No dependency is removed to force activation/closure. Existing runtime-delivered notes do not complete the archive decision or broader retirement gates. |

Source references to `terminal_recovery_ledger` and `terminal_recovery_audit`
were found in their historical DDL and test cleanup support, rather than a live
recovery command implementation. That source observation does not authorize
deletion or establish that production history tables are empty. Only fresh
disposable layouts were queried.

## Fresh schema observation

The one-off probe created a temporary SQLite database and used the disposable
PG fixture. It queried table/column metadata and the two historical recovery
trigger names, with no archive row mutation. It observed:

- No live `claim` relation on either backend.
- `claim_history.claim_token` and `.lease_epoch` on both backends.
- PG `terminal_recovery_ledger.lease_epoch` and the ledger/audit guard triggers.

The source guards reject UPDATE/DELETE. Their presence is not a proof against
privileged DDL/TRUNCATE or a new permanent archive-retention requirement.
The probe's temporary test file was removed after execution so it cannot turn
today's storage observation into a regression oracle that preempts the owner
decision. Probe source SHA-256:
`711d828be80bf136bc56308b7daa393c4190b9ef8a0151c5502d3e86e08a7e46`.
Its observed metadata is retained in the linked JSON, not as a schema contract.

## Executed evidence and limits

- **83 local tests passed in 2.05 seconds**, covering existing authority
  contracts, native reserve metadata, advisory reservations and the SQLite
  archive/drop migration example.
- **50 disposable PG 16.15 cases passed in 5.50 seconds**: 28 native owner,
  21 native intake, and one metadata-only schema probe. Three fixture cleanups
  left zero rows. This does not prove an unexecuted historical PG migration or
  complete storage retirement.
- The separately executed **6 Vuoro source-envelope tests** concern the
  retired transient proof transport, not removal of database archives.

Owner review cleared evidence publication only. The proposed disposition is
to preserve the pending item and its prerequisites while the archive policy
and obsolete exclusivity acceptance are explicitly reconciled. No live
database, secret values, deployment or product behavior was changed.
