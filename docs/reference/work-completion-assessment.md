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

When a current lease has reported work, the suggested
`work.lease.report-outcome-v1` path remains the only lease-based completion
writer. That operation locks the item and lease, recomputes the union of the
lease-pinned and current Release verification requirements, checks blockers,
and records any terminal Decision in its transaction. It does not consume or
trust an earlier assessment. The report operation must be retried with its
original idempotency key after an uncertain response.

This evaluator does not join Auditctl observations, disconnected run evidence,
model judgments, or effect application outcomes into work completion. The
S4 `work.evidence.evaluate-v1` operation remains a separate run-chain
evaluator. The trusted effect-intent reconciler remains separate as well; it
does not accept or settle Sprintctl work.

This source change needs no database migration. It does not claim a deployed
owner has published the operation; rollout still requires the normal source,
catalog, profile, and deployed-contract gates.
