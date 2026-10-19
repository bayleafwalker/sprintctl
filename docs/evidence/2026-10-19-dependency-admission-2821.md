# Dependency admission extraction — Sprintctl#2821

The SQLite and PostgreSQL `add_dep` wrappers now share the private
`depcore._validate_add_dep` guard sequence. Backend forwarding lambdas resolve
`get_work_item` separately at each lookup. Each wrapper retains its original
signature, adapter construction after admission, and exact storage return
expression. Existing adapters and shared storage functions are unchanged.

The governing native Release is
`de6449769abbb7870843651c4f6f184998cdc08065908fa31b0d8631be78ca51`.
The child embeds the approved scope whose SHA256 is
`c691b74b002ad5eb5b3c42899d839dd5660388afe02c228af9aa3d00421c5e32`,
following parent2150 notes5809,5834 and5848. This is a bounded current-provider
refactor. Parent2150 and EC05 remain incomplete.

## Frozen before and after evidence

The accepted source before extraction is
`4a59bd6afddb6f6a6fe86ab80a27fb4d28ea11c9`. Ten existing source/helper files were
hashed before writing the oracle. An independent reviewer found missing idle-entry
transaction coverage during development; it was added before the final freeze.
A development mutation-control assertion compared pytest's entire rewritten
message too strictly; it was corrected before the freeze. Those runs remain
historical development evidence and are not the frozen baseline.

[The frozen packet](../../verification/fixtures/dep-admission-before-freeze-2821.json)
pins the reviewed three oracle files, ten source/helper files, runtime imports,
commands and exact test nodes. Its SHA256 is
`548b17671b87ef46db99aae87c40a59d40bcc06841e8892144c32b03086793b6`.
The coordinator verified the successful before campaign and explicitly authorized
product edits only afterward.

Both campaigns executed the same **35 local and 21 actual PostgreSQL tests**,
with zero failures, errors or skips. Each PostgreSQL campaign registered one
fixture cleanup with zero rows remaining. The local count includes 19 actual
SQLite cases, four synthetic raw-value wrapper cases and twelve pure mutation
controls. PostgreSQL has the same 19 actual cases plus two foreign-endpoint cases.

The [before receipt](../../verification/traces/dep-admission-2821/sprintctl-2821-before-receipt.json)
and [after receipt](../../verification/traces/dep-admission-2821/sprintctl-2821-after-receipt.json)
retain exact commands, raw log/XML hashes, observed rows and transaction states.
Adjacent raw artifacts preserve their original bytes. Historical absolute paths
identify where they ran; the repository copies retain the corresponding evidence.
Each run compares actual complete rows to explicitly expected effects within that
run. Separately generated UUIDs, timestamps and IDs are not asserted equal between
campaigns.

Six seeded wrong wrapper behaviors are rejected for both backend wrappers:
self-check ordering, endpoint ordering, truthiness in place of `is None`, eager
lookup binding, ID coercion and premature adapter construction. Actual database
controls on each backend also reject suppressed commit and caller rollback on
refusal using the same durable-edge and pending-caller assertions as the baseline.

## Preserved boundary

Self equality occurs before lookups. Missing endpoints retain their original
error strings and first-before-second precedence. Falsey non-None values and raw
IDs are explicitly synthetic wrapper characterization. Valid persisted examples
cover reverse and cross-sprint edges, active endpoints and duplicate insertion.

Refusals preserve pending caller writes and savepoints. Idle missing-item reads
leave SQLite idle and PostgreSQL in its existing read transaction. Success and
duplicate lookup commit prior caller writes before the final dependency-ID read.
A real scoped INSERT trigger failure leaves SQLite's transaction usable and
PostgreSQL's transaction failed until caller rollback. An exception after the
actual final read leaves the already committed edge and caller writes durable.
PostgreSQL retains repository-scoped endpoint lookup.

These are finite examples, not concurrent histories or an atomicity repair. They
do not establish crash durability, a new provider contract, deployment readiness,
or completion of backend-unification or EC05. The
[context](../../verification/contexts/dep-admission.json),
[result](../../verification/results/dep-admission-2821.json) and
[source comparison](../../verification/traces/dep-admission-2821/sprintctl-2821-after-source-comparison.json)
separate the tested claims from those limits. Full regression receipts and final
review are recorded in the result packet before landing.

## Local runner environment correction

The first full local run inherited `SPRINTCTL_BACKEND=served` and a shared profile.
It finished with 21 CLI startup/scope failures, 3,242 passes and four skips. The
same code passed all 52 cases in the two affected files after clearing inherited
`SPRINTCTL_*` variables. Independent review corroborated the outer CLI guard and
test-double interactions. The full corrected run is recorded separately; the
original failure evidence is retained in the
[environment diagnostic](../../verification/traces/dep-admission-2821/local-environment-diagnostic.json).

The original failure traces show no actual HTTP attempt. They do not contain
complete transport instrumentation, so absence of HTTP requests or shared-state
effects throughout that run is unknown. The corrected local command clears the
inherited backend selection before running the same suite. The disposable
PostgreSQL runner already clears these variables itself.
