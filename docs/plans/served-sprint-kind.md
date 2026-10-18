---
doc_id: served-sprint-kind
status: draft
supersedes: null
---

# Served sprint classification

Native item `sprintctl#2369` governs this approved repair. The new
`work.sprint.kind` operation requires authenticated `work:sprint` authority and
normal repository authorization. Its arguments are exactly `sprint_id`, `kind`
and `expected_revision`. Sprint IDs are positive signed 64-bit integers; the
CLI and owner refuse out-of-range IDs before a basis read or mutation. It returns the changed sprint inside the locked
transaction, including its `kind_revision`; it does not discover another
repository, change sprint status, or grant execution authority.

The basis has form
`sprint:<aggregate_uuid>@kind:<active_sprint|backlog|archive>@status:<planned|active|closed>`.
Both identity and observed kind/status must match. SQLite uses `BEGIN IMMEDIATE`;
PostgreSQL locks the repository-scoped sprint row before comparison and update.
A same-kind request is a successful no-op only with a matching basis. A stale
request returns `sprint-kind-conflict` (409). The existing status revision is
unchanged. This compares state; it is not a monotonic counter or protection
against state returning to an earlier value.

The CLI reads the current basis before its mutation unless the caller supplies
`--expected-revision`. Both served and direct CLI paths send that basis. Existing
Python backend callers retain their optional basis for compatibility. A served
command never opens the local database. `sprint show --json` exposes the basis;
`--detail` includes it too.

```sh
sprintctl sprint kind --id sprintctl#123 --kind backlog --json
sprintctl sprint kind --id sprintctl#123 --kind archive \
  --expected-revision 'sprint:<uuid>@kind:backlog@status:planned' --json
```

An archived sprint cannot be activated through the SQL activation handler,
direct status mutation or served lifecycle arbiter. Classification of an already
active sprint as archive remains permitted, matching existing kind semantics.
If classification races with activation, archive-first makes activation refuse;
activation-first invalidates the earlier kind/status basis. No collection
filtering, bulk reclassification, external effect or schema migration is added.

The write is not automatically replayed. A lost response means its outcome may
be unknown: read the current sprint before deciding what to do next. A matching
kind does not identify which request produced it. Atomic history publication and
request-id deduplication are outside this operation's guarantees.

Native PostgreSQL, independent SQLite/PostgreSQL connection races, actual generic
HTTP client authorization and CLI no-store tests cover this contract. The
published catalog adds one operation; the doctor probes it. Prior descriptor
bytes remain unchanged when the named additive extensions are subtracted. Local
verification does not qualify a deployed catalog: the separate deployment owner
controls release, pin and service operations.

## Estate contract migration

This repairs the current owner catalog; it does not select an estate-wide IDL,
transport or generated client. The accepted estate rewrite retains the functional
outcome under `sprintctl#2738` (EC-05), after the EC-00 architecture qualification
and EC-02/03 foundation gates. This operation remains an inventoried internal
boundary to migrate or retire with its consumers. Its current non-replay policy
and state-basis revision are not proof of the destination's scoped idempotency,
retention or mixed-version requirements. No competing contract foundation is
introduced by this repair.
