# Archival result transfer for selective tenant-exit rehearsal

Status: implementation candidate, fixture qualification only. Related owner work:
agentops#2642 (Sprintctl foundation) and agentops#2630 (Cloud composition).

This contract reconstructs one historical accepted result in a disposable target.
It does not resume execution, restore source permissions, or qualify a production
or customer export. Existing export/import and recovery modes remain unchanged.

## Owner boundary

`pg.export_from_postgres(..., archive_result={workspace_id, item_id})` is an
owner-internal opt-in path. It requires an owned private Unix-socket fixture under
`/tmp/vuoro-archive-*` and a specifically named and marked disposable database.
There is no CLI, HTTP or MCP export/import endpoint and no arbitrary DSN argument.
The marker and path prevent accidental use of an ordinary backend; they are not
a security boundary against the OS owner or database administrator.

The source is read through `repeatable_read_snapshot`, with a sibling connection,
`REPEATABLE READ READ ONLY`, and a five-second statement timeout. The snapshot
has at most 2,000 records and eight MB. It reports its schema, observation time,
record inventory and checked closure. This is an owner report, not attestation.
A later source change makes a before/after comparison inconclusive; it does not
invalidate the internally consistent snapshot or prove export mutated the source.

The source selection is one explicitly bound fixture repository/workspace.
All records for that repository are included, so incomplete run-chain selection
cannot masquerade as a complete export. The checked selected result must have an
accepted owner Decision, its Release, and one settled native outcome report.
Active reservations, claims and leases are refused. Other repositories are absent.

## Records and projections

The existing twelve migration tables are reused, with five native additions:
`run`, `evidence_item`, `work_lease`, `work_outcome_report`, and
`work_effect_intent`. Original result payload, Release, canonical intent and
evidence chain digest domains must validate unchanged. The protected artifact
receipt must join the exact original evidence/run/chain and stored acceptance
binding, including its body and digest. The selected accepted intent always
requires this binding; other accepted/applied intents require it when their
frozen Release requires protected verification. This first protected-result
slice requires the outcome to name its accepted intent ID, canonical intent
digest, Release digest and verification receipt digest. These must join the exact
accepted/applied intent, original receipt, item and outcome run. Removing the
intent or outcome links cannot downgrade this contract into an unprotected
result. Missing links fail the whole transfer.

Effect-attempt authorization/event rows and run-predecessor rows are unsupported
in this first contract; any source rows in these tables cause explicit refusal.
Session notes, operational ingest infrastructure, and local SQLite native intake
outboxes are excluded and are not needed for the selected fixture result. This
is not full estate or active tenant portability.

Raw lease IDs, claim/reserve/replay keys and claim proofs never leave the source.
Fixed table-field projections replace lease references and owner-generated
historical rationale/event references with fresh archival aliases. Descriptive
principal/client/grant identifiers identify past bindings and confer no target
permission. No credential, grant table, or operational replay entry is imported.
An unsupported capability in a canonical content field causes refusal; it is not
redacted while pretending its original content hash still verifies.

Every projected row has an original source-row digest and a separate projected
row digest. Historical Decision identity/kind/Release/evidence links remain;
a rationale containing the source lease handle changes visibly to its alias.
The target does not mint a Decision. Reserved authority events use the existing
ordinary archival import demotion rules. The immutable projection/receipt records
these boundaries; it creates no new business-history writer or truth store.

Bound proposal admission lives in the source idempotency ledger. Only required
historical facts are projected: intent identity, expected revision, Release,
commit, and evidence tail. Required joins are checked. The original source
observation digest is computed before projection and is never recomputed from
the redacted facts. Projection digest and omitted field paths are explicit.
The operational ledger, reserve key, reservation binding and current authority
are excluded. Full native admission reconstruction and current authorization
remain **unknown**.

## Disposable import

`pg.import_ndjson(target, bundle, archive_result={workspace_id, item_id,
expected_bundle_digest})` accepts only a fresh, named, marked owned target with
an idle connection and exact supported owner schema. It cannot replace, remap
IDs, or perform trusted state transfer. The independently supplied expected
bundle digest must match the actual projection.

Before insertion it rejects unknown tables, mixed repositories/workspaces,
wrong digests, incomplete causal links, live ownership and incompatible columns.
All destination owner tables must be empty across all repositories; locks cover
the native and base tables. Existing base import and native insertion share one
transaction. Failed or interrupted insertion rolls back the whole result.

The fixture administrator commissions a fresh SELECT-only reader, closes the
administrator connection, and starts only the loopback evaluation reader shell.
No source identity permission is restored. Actual SQL write denial and native
claim refusal are qualification gates. An OS/database administrator can of
course change this disposable fixture; such an action is outside the import
contract and invalidates the read-only qualification.

Native target reads reconstruct the outcome, Decision/Release and intent.
`intent.get` exposes its exact protected receipt; the existing owner validator
checks it against actual imported evidence/run/chain records. A fresh reader
cannot call `evidence.tail` for another historical principal's run. This limit is
preserved: no impersonation, ownership relaxation or extra evidence grant is
used. The receipt must not claim an independent native evidence-tail read.

## Qualification and limits

`tests/test_archival_result_fixture.py` uses the released optional `vuoro-demo`
consumer's owned PostgreSQL 16 fixture and real native source APIs. It is opt-in
through `ARCHIVE_PG_BIN`; `ARCHIVE_RECEIPT` writes a private qualification receipt.
The consumer is a test dependency only. Production Sprintctl dependencies and
catalogs do not change.

The actual scenario covers source stability, second-repository exclusion,
workspace/predecessor refusal, canonical source-capability refusal without
scrubbing, wrong outcome/intent/evidence/Release/schema/outer digest, truncated
closure, occupied target, and rollback after native insertion interruption.
The target must expose exact checked native joins and deny SQL/native writes.
Owned fixture cleanup records whether PostgreSQL stopped; an uncertain cleanup
must retain its owned directory. Abrupt process death is not successful cleanup
or successful transfer. This module introduces no subprocess supervisor.

Cloud subsequently composes this released owner capability with existing
external artifact receipts. External bytes are not fetched or taken into Cloud
custody. Byte availability and unknown retention obligations remain separate
from successful record reconstruction. No deletion policy or action is implied.
