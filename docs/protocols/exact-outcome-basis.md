# Exact continuation basis at outcome settlement

`work.lease.report-outcome-v2` adds the existing continuation `checkpoint` to
`report-outcome-v1`. The caller reads `work.run.continuation-delta-v1` through
its own run, retains the complete `delta.current`, and submits it with the
outcome. The schema and digest projection are shared with that read. A null
Decision or reference digest means an incomplete population, including more
than 64 facts; it cannot establish currentness. A null current Release means
there is no current Release and remains a comparable owner fact.

The owner still applies its current lease and verification policy. Passing the
basis check is not an approval and does not lower a human, role or identity
verification requirement. V1 and its deprecated alias preserve their existing
acquisition goal/Release checks; they do not compare live references.

For v2, one transaction obtains bounded table locks in fixed alphabetical order,
then locks the owned work item and lease. It returns an already committed exact
idempotent answer before comparing currentness: the answer's own accept Decision
has changed the checkpoint. Current authenticated run/grant ownership is checked
before that replay. Reusing a key with a changed checkpoint conflicts.

The owner compares all seven checkpoint fields before any settlement Decision.
`work-basis-stale` and `work-basis-unavailable` retain a rejected outcome report,
release the active lease, and create no Decision. A lock or statement timeout
rolls back the transaction, including its idempotency ledger entry; the caller
must reconcile/replay the same durable request rather than infer an outcome.
Lock waits are at most one second and statements at most five seconds, retaining
stricter caller settings. These are server transaction bounds, not a client
network deadline or an exactly-once delivery promise.

## Serialization boundary

Normal description edits lock the item through
`workitemcore.update_work_item_description`. Status, carryover and Decisions use
`_set_work_item_status_locked`, `_carryover_transaction` and
`record_decision_keyed`; Release/reservation admission uses
`_release_basis_locked` through `reserve_in_transaction`. Lease settlement uses
the same item lock. Generic event writes cannot forge `item-edited` events.

Supported `import_ndjson` state transfers can append edit events, Decisions,
Releases and reservations without those normal API locks. Their existing exact
`(repo_id, work_item_id) -> work_item(repo_id, id)` foreign keys require a
conflicting parent key-share lock at insertion or commit. A deferred Decision
insert can remain uncommitted during comparison, but cannot commit until after
the settlement transaction. Ordinary archive imports demote typed events;
trusted state transfer remains an explicitly trusted administrative operation.
Replace import refuses a repository holding Releases or Decisions. No new import
permission or side channel is introduced here.

Reference insertions use their immediate exact composite FK. Existing reference
updates and removals require the locked reference rows. The owner validates all
five population FKs, their enabled constraint triggers and `origin` replication
mode. It requires the reference FK to be immediate. It holds
`SHARE UPDATE EXCLUSIVE` on `event`, `ref`, `reservation`, `work_decision`,
`work_item` and `work_release`, preventing concurrent FK/trigger DDL while
allowing ordinary DML. This serializes v2 compare transactions across the owner
database and can contend with vacuum/DDL. It is an explicit throughput cost of
this bounded extension, not a general cross-operation linearizability claim.
No schema migration or new work ledger is involved.

The compare and resulting Decision commit are the settlement boundary. A
reference mutation may commit afterward, including after the item is done.
This operation does not provide a live revocation fence for already running
external effects or model calls. A commissioned project adapter must separately
use actual current Work authority at each applicable resource admission.

## Response reconciliation

Match the returned report's lease/run, outcome, summary, payload and checks to
the durable submitted request. Use its owner-produced `payload_digest`, which
covers outcome, summary, payload and checks, to match the terminal Decision's
evidence. Hashing only the raw payload does not reproduce that digest.
`work.lease.read-v1` exposes retained outcome reports even after the lease ends;
it permits observing a lost acknowledgement before replaying the exact request.

Detect v2 in the actual catalog. A version string or the presence of bound
lease fields alone does not establish this operation: both released 0.26.0 and
0.27.0 already fence goal/Release changes, while their v1 still accepts a live
reference change between a consumer checkpoint read and settlement.

Tests in `tests/pg/test_outcome_current_basis.py` use a preflighted disposable
PostgreSQL database, real public contracts, independent connections and
transaction barriers. They cover changed facts, incomplete populations, lost
acknowledgement, current caller ownership, malformed FK protection, conflicting
reference/goal/Decision/Release/import writers, DDL, timeout rollback and
unchanged v1 behavior. They do not qualify an installed deployment.

The source-check trigger guard requires the complete four-trigger RI set, with
exact PostgreSQL function identity, source/target relation, constraint ID, row
and event type, and enabled origin/always mode. The ordinary suite filters one
real source-check row through a read-only metadata CTE to model an incomplete
set; it does not corrupt a system catalog. The opt-in
`test_outcome_trigger_faults.py` additionally uses supported `ALTER TABLE` to
disable each source-check trigger in its own newly created disposable database.
Setup alone uses a loopback temporary-instance administrator; actual owner
requests reconnect with a preflighted unprivileged role. Exact temporary data
directory, role restrictions and database lifecycle are guarded explicitly.
These privileged setup tests are excluded from ordinary collection unless
`SPRINTCTL_TEST_FK_FAULT_ADMIN_URL`, `SPRINTCTL_TEST_FK_FAULT_DATA_DIRECTORY`
and `SPRINTCTL_TEST_FK_FAULT_ROLE` are supplied. They establish the local fault
history, not normal production permissions. Destructive removal of a single
internal catalog trigger was not executed; supported whole-FK removal,
not-valid/deferred constraints, trigger disabling and read-only missing-row
metadata cover the refusal without system-catalog corruption.
