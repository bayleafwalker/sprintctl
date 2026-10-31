# Read-only Work currentness

`work.lease.currentness-v1` observes one exact owned run, lease and existing
ContinuationBasis checkpoint. It requires `work:read`, PostgreSQL schema31,
and the actual principal/workspace/client/grant binding of both run and lease.
The request contains only `run_id`, `lease_id` and `checkpoint`; it contains no
identity claim, permission flag, heartbeat, idempotency key or effect target.
The operation and schema are published by the Work catalog. Consumers reuse
that owner schema and a qualified operation pin; they do not copy a competing
checkpoint model or infer support from a package version.

One read-only repeatable-read owner snapshot verifies run supersession, exact
lease holder and item, active item/lease, acquisition goal/Release basis, all
seven continuation fields and complete bounded Decision/reference populations.
Null Decision/reference digests cannot establish currentness. It returns the
actual holder, unchanged checkpoint, hash of the full current requirements,
owner observation/completion times and captured lease expiry. No raw work
requirements are returned. The response says `authorizes_effect: false`.
It neither renews the lease nor writes a report, Decision or idempotency record.

Delegated callers are revalidated against the existing owner delegation row in
the same snapshot. A token issued for only this operation projects only
`work:read`; its operation whitelist does not permit notes, evidence, leases,
effects or nested delegation. Existing issued tokens do not acquire the new
operation implicitly. The owner checks captured lease and delegation expiry
again using owner time at the end of the observation. The read takes no write
or share row locks; existing mutating delegation paths retain their share locks.

The facts linearize at the repeatable snapshot. Revocation or a basis change may
commit after that snapshot, even before its response arrives. The response is
not a reusable grant or a promise that the lease remains valid until its stated
expiry. An installed project guard must use a fresh read at each applicable
admission, bound its total monotonic read/authorization time and native polling
interval, and declare propagation and already-admitted/in-flight limits. A
read timeout, lost reply or unknown population cannot authorize an effect.
Historical observation/cancellation and ready Workspace reader recovery remain
separate from active execution and new model/resource admission.

## Schema31 cutover

Actual PostgreSQL testing disproved the initial no-DDL assumption: schema27's
closed `work_run_delegation_operations_check` rejected the new scope. The
explicit schema31 deployment migration replaces only that CHECK to add exactly
`work.lease.currentness-v1`. It adds no tables, ledger or lease fields and
rewrites no delegation records. It first validates the exact released delegation
catalog fingerprint, refuses foreign/partial definitions, and runs with the
schema-version stamp in the existing migration transaction. Failure rolls back
both DDL and version. Normal service startup remains check-only.

The released schema27 fingerprint remains frozen; schema31 has its own qualified
fingerprint. Old operations, token rows, revocation and TTL rules remain intact.
An older physical schema neither advertises nor admits the new read/scope. The
new runtime requires its exact schema31 compatibility window; release and live
migration are deployment-owner actions, separate from this source change.

The actual PG tests cover read-only state/heartbeat counts, lost read reply,
foreign holder dimensions, stale/unknown basis, superseded/expired/unbound
leases/runs, expiry during an observation, live scoped delegation revocation,
unchanged old tokens and independent edits during a coherent snapshot. Migration
tests exercise real30→31 preservation, repeatability, foreign CHECK refusal and
failure after DDL without a partial version stamp. These are bounded application
histories, not cross-owner/network atomicity or installed paid-service proof.
