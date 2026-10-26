# Work completion assessment

`work.completion.assess-v1` reads Sprintctl owner facts for one work item at a
single PostgreSQL repeatable-read snapshot. Its source contract requires the
schema 28 owner, `work:read`, a full item revision, and its Release digest. The
request basis is compared with both the current item revision and the selected
Release's frozen revision; an old Release that no longer matches the item is
reported as stale.

The response includes the current terminal Decision, current verification bar,
bounded current-Release, lease, and outcome-report history, unresolved blockers,
source completeness, and a snapshot digest. More than 64 current-revision
Releases, leases, outcome reports, or unresolved blockers makes an otherwise
open assessment `unknown`; truncated history cannot establish completion. Only an owner `accept` Decision bound to
the current Release is reported as `accepted-current`. Missing, stale,
contradictory, blocked, incomplete, and awaiting-verification states remain
distinct. An exact current owner acceptance remains a fact even when unrelated
history is truncated; the follow-up then asks for conflict inspection.

The operation has no write path. It always returns
`authorizes_acceptance: false`. Follow-up recommendations are deterministic
for the same source snapshot, item revision, and action. Exact-snapshot IDs are
not a durable recommendation registry: prior rejection and supersession are
untracked, so callers must not treat a suggestion as a created or reopened
work item.

Schema 28's lease writer is the only existing lease-based completion writer,
but it is not safe for automatic acceptance against a revisioned assessment.
`work_lease` stores verification requirements but no item revision or Release
digest from acquisition. Editing an item leaves its active lease intact.
`work.lease.report-outcome-v1` locks the item and lease, recomputes the union
of verification requirements, checks blockers, then writes a Decision bound
to the Release current at report time. It does not compare that Release or
revision with the lease's acquisition basis.

Therefore an active lease always yields an `inspect-conflict` suggestion with
no `next_operation`; the evaluator never directs a caller to
`work.lease.report-outcome-v1`. A lease acquired against revision R0 can remain
active after an edit to R1 and can still settle through the current writer
against the then-current Release. The owner acceptance race remains blocked
until a coordinated schema/runtime change records the full item revision and
Release digest on each new lease and compares both under the item lock before
writing a Decision. Existing leases have no recoverable acquisition basis and
must remain unknown and unable to settle successfully after that cutover.
Reports continue to be retained as evidence on refusal. The outcome report
must be retried with its original idempotency key after an uncertain response.

This evaluator does not join Auditctl observations, disconnected run evidence,
model judgments, or effect application outcomes into work completion. The
S4 `work.evidence.evaluate-v1` operation remains a separate run-chain
evaluator. The trusted effect-intent reconciler remains separate as well; it
does not accept or settle Sprintctl work.

The read-only assessment needs no database migration. It does not claim a
deployed owner has published the operation; rollout still requires the normal
source, catalog, profile, and deployed-contract gates. Lease-basis-safe
automatic acceptance is a separate schema/runtime cutover and is not enabled
by this assessment.
