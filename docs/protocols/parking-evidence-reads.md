---
doc_id: sprintctl.parking-evidence-reads
title: Parking evidence reads
purpose: specification
lifecycle: active
effective: 2026-10-20
applies_to:
  components: [sprintctl]
---

# Parking evidence reads

Agentops#2826 supplies the missing owner reads for E4 (agentops#2472).
No schema migration, release operation, scheduling, model choice or new
authority is introduced. The operation catalogue is the capability signal;
consumers must refuse unsupported reads rather than substituting a tail.

`work.evidence.entry-v1` takes only `run_id` and `chain_seq` (integer
0..9007199254740991). It requires `work:evidence`, forbids idempotency keys,
and applies the same repository, principal, workspace, client and grant
binding as the tail read. It returns the exact immutable entry or null when
that sequence does not exist. Unknown and foreign runs produce the same
404 refusal; the supplied run ID may be echoed. This read does not expand
the delegated-run operation allowlist.

`work.lease.parking-context-v1` takes `item_id` and optional `lease_id`.
It requires `work:read`, forbids idempotency keys and returns `current`,
`previous`, nullable `lease_run` (declared run/model IDs of the named lease),
and owner-clock `evaluated_at`. No caller-supplied run or
evidence reference is accepted. An unknown item or a lease belonging to
another item/repository is refused.

`current` is the newest parking when the latest item parking/release event
is `work.parked`, otherwise null. With a lease, `previous` is the parking
immediately superseded by the last release before that lease began. A
release immediately preceded by another release supplies no previous
parking. Later events cannot replace this pair. Without a lease, `previous`
is null. This is an observation of existing owner records, not new state.
The cutoff is the serialized `lease.acquired` event ID, not a wall-clock
comparison: PostgreSQL event timestamps use transaction-start time, and a
later release may come from an older transaction. A missing acquisition
event is `lease-history-unavailable` (409), not an ordinary claim with no
prior parking.

Each parking contains event/report/lease IDs, reason reference, park time,
the report's run ID and declared model ID, and nullable `rate_limit`.
The previous parking also contains the release event ID and time. The
owner joins the item's cited outcome report and immutable run, and resolves
only `vuoro-evidence:<that-run>/seq/<sequence>` from that report. It never
accepts a run ID as a lookup parameter on this work read.

The rate-limit projection is populated only for a `rate_limit_event` with
a digest matching the canonical UTF-8 JSON observation, exact v1 fields,
`rejected` status, matching registered model ID, valid UTC observation/reset
timestamps and bounded scalar values. Invalid, foreign, missing, warning
or other-kind evidence produces null. The published fields are
`observed_at`, `status`, `limit_scope`, `resets_at`, `reset_source`,
`http_status`, and `overage`. Private refs, collectors, claims, arbitrary
provenance, model-family assertions and the rest of the chain never cross.
The consumer may label the declared model using its static classification
table; the owner never selects or recommends a model.

The explicit parking citation publishes this bounded work fact to readers
of the work item. It does not authorize a reader to call the private exact
entry read on the predecessor run. PostgreSQL tests exercise both sides of
that boundary and compare all owner rows before/after these reads.
Both new reads preserve an existing caller-owned transaction. When an
invocation starts idle, the common application boundary closes only the
read transaction it opened; the backend read neither commits nor rolls
back unrelated pending work.
