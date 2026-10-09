# S4 native reserve prerequisite source slice

## Current integration amendment — 2026-10-09

The prepared October 4 slice is integrated on Sprintctl main
`050a8181101473f312174871858f48e9c9f885c9`. The additive catalog release is
**0.14.0**, with SQLite schema 25 and remote schema 21 unchanged. It retains
the existing immutable adapter-kit 0.2.0 dependency. Current tests import that
actual dependency; the earlier prospective 0.2.1 coordination hold is historical
preparation, not a claim that this slice requires a different kit API.

The baseline oracle is extracted from the actual published Sprintctl 0.13.2
wheel, SHA256 `450b67a54c03c5de1383a2ce3f7cc428f17160eb88f0f77c63a7056bc192c705`.
All 74 existing catalog descriptors must retain their canonical hashes; only
`work.reservation.reserve-v1` is added (75 available / 72 without resource schema).
The MI-1 explicit `acceptance_contract` argument is preserved in both schemas.
Native replay normalizes and binds that contract into its request digest. An
explicit contract requires execution role and an observed expected revision,
as in the published legacy owner; changing a contract under an accepted key
conflicts before any new reservation or Release. Tests check the stored protected
contract and unchanged counts/Release on conflict and invalid inputs.

This integrates an owner prerequisite only. Producer reserve/proposal carriers,
the actual isolated causal absorb gate, import, hook cutover and soak are still
open. No live authority is stopped and no source shard is rewritten.

## Historical October 4 preparation

Status: proposed source preparation for agentops#2485, 2026-10-04. This
implements the bounded owner prerequisite from the [proposed reserve plan](s4-reserve-owner-replay-2026-10-04.md)
at `f7e22258904bc6e04b591f6de01fd683a65ca8ab`, merged as
`1b5ea7dbc1037821c06ba917e9d0ebfc503d0df9`. Neither that merge nor this source
ratifies S4, migrates a client, imports evidence or starts a cutover/soak date.

## Native contract

`work.reservation.reserve-v1` is a separate catalog operation: existing
work:write authority, write semantics, required envelope key, and the same
closed arguments as legacy reserve. It requires authenticated principal and
workspace plus the native PostgreSQL participant. It adds no capability,
credential, producer queue, schema migration or CLI redirect. The existing
served CLI continues invoking legacy `work.reservation.reserve`.

The owner validates the actor, closed arguments, role, key and Release basis;
normalizes omitted defaults/nulls; and uses the validated envelope key in the
repository/workspace/principal/`reservation.reserve-v1` ledger scope. A first
claim commits reservation, Release freeze, explicit execution interruption,
complete lifecycle events and immutable admission snapshot in one transaction.
A failed effect rolls the ledger claim and every write back. It does not create
a permanent generic refusal. A successful different-content key conflicts. Native new admission under
active maintenance refuses with reservations-disabled-maintenance (409);
legacy refusal mapping stays unchanged.

The ledger returns internal claim/replay metadata without changing default
native evidence/proposal callers. After commit, a single SQL statement reads
the retained current reservation and current active overlaps. The result keeps
`repo_id` and `reservation`; nested `admission_snapshot` records creation state
and initial overlaps, while `replayed` describes this invocation. Current
state/overlaps may differ from the snapshot. Missing retained rows refuse
reconciliation rather than recreating a reservation. There is no new ledger
TTL/purge. Revoked work:write refuses before replay. Deployed repository and
HTTP authentication remain the service dispatch's responsibility, not proof
from these application-boundary tests.

Unkeyed direct PostgreSQL reserve and legacy hosted reserve inherit the atomic
event participant while retaining advisory admission, return shapes, supported
roles and backend timestamp representation. SQLite is unchanged. Legacy hosted
reserve still ignores its required envelope key; its old deduplication prose
is corrected, and this operation is not silently migrated to the new guarantee.
Touch, reassign, release and durable work leases retain their existing contracts.

## Measured lock participants and enrollment inventory

The new lock order is native replay claim, repository admission, item/Release,
ordered reservation rows, event/result writes. A committed replay bypasses new
maintenance admission. Event insertion uses the same connection without commit;
item existence is resolved/locked inside the participant. Repository admission
remains advisory coordination, never an exclusive work lease.

Maintenance activation takes repository then capability; lease paths take
repository then item then lease; keyed decisions take their own key then item;
evidence takes its distinct native key then chain; proposals take their native
key then item. Touch/reassign/release/activity and the explicit stale sweep
update reservation rows, then commit before their later event/item reads.
They have no held reservation-to-item/repository edge in those paths. No
reservation expiry thread exists. A capability-expiry sweep locks its capability
row but does not acquire reservation/item rows or this reserve ledger.
MaintenanceResourceStore.record_current_in_transaction/record_if_registered
run within activation/expiry transactions and lock their maintenance_resource
row before appending/pruning resource events; they touch no reservation, work
item or native replay ledger rows, so add no reverse edge here.

A concrete reverse-edge limit remains: trusted direct
`import_ndjson(replace=True)` holds the identity-sequence lock and deletes
reservations before work items. Concurrent destructive replace can therefore
invert item/reservation order. It is outside normal hosted reserve admission,
and this slice does not repair/import through it. Any future actual import must
prove the isolated maintenance/no-concurrent-authority gate first; this source
must not be represented as a general migration deadlock proof.

Existing supported identity types include actor-only/unbound static callers
and principal/workspace-bound callers. Legacy reserve supports the former;
new-v1 refuses them. The public work.identity.current operation only exposes
actor, so it cannot prove a current workstation caller's principal/workspace
binding. No current client has been enrolled by this child. Actual owner binding
and composition proof is a deployment prerequisite, rather than an inferred
property of a profile path. Disposable tests use explicit trusted bound and
unbound context fixtures, not newly commissioned grants or production login.

## Actual published release comparison

The actual published Sprintctl 0.12.1 wheel has SHA256
`b5e769b5e31dbdbede635186924b86185d2840e72ffdf16e9cb9e7d19ffcf9ea`
and pins immutable adapter-kit 0.2.0. Importing its extracted packaged source,
rather than a Git fixture, yields 74 owner descriptors with resource schema
available and 71 without it. This candidate yields 75 and 72 respectively.
Only `work.reservation.reserve-v1` is added; every previous descriptor's
canonical JSON bytes/hash is unchanged. The 74-operation source golden also
matches that actual published wheel exactly. Every top-level adapter function
and class is byte-for-byte unchanged; the contract tuple gains the new entry.

The release's packaged-source delta also includes the already merged PR121
run-bound evidence producer carrier, which was not in published 0.12.1:
`evidence_intake.py`, its native transport helper, CLI/routes and owner support.
Thus owner 0.12.2 carries both bounded source slices, while reserve-v1 is the
only catalog addition. The proposed PR122 plan is documentation, not a new
runtime operation. The pending immutable kit 0.2.1 pin must preserve these old
catalog bytes; this comparison must be repeated after that pin before freezing
the source or publishing. Neither source slice establishes the complete S4
reserve/evidence/propose absorption pipeline or a production cutover.

## Verification and remaining gates

New real PostgreSQL histories cover same-key contention with observed database
lock waits, conflict, normalized retry, retained current state, explicit takeover,
principal/workspace isolation, stale-basis correction, rollback during each event
or stored-result write, commit-boundary fault injection and missing retained row.
Maintenance activation interleaves before repository acquisition and after the
repository, item, reservation and result boundaries. These tests use independent
connections and bounded synchronization against disposable PostgreSQL16.

Commit-boundary loss is injected around a real database commit; it is not an
actual network partition or process-kill proof. Application invocation tests do
not replace deployed catalog/HTTP authentication, current client binding or
continuously-online versus offline effective-state comparison. Direct legacy
and SQLite regressions preserve compatibility; exact-head CI and independent
review must complete before root landing. Immutable adapter-kit dependency
compatibility must be coordinated before owner 0.12.2 is frozen or published.

Reserve producer capture/attempt/receipt transport is still absent. Proposal
transport, causal reserve/evidence/propose ordering, the actual isolated S4
absorb gate, digest-preserving legacy import, rotated hooks, coordinated cutover
and ninety-day read-only soak remain open. No production import, grant,
classification, authority stop, retirement or live reserve invocation is part
of this source preparation.
