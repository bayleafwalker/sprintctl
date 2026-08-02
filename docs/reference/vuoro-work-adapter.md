# Vuoro work adapter and application core

The sprintctl work adapter exposes sprintctl-owned state semantics through the
Vuoro protocol-v1 catalog. The reusable Vuoro shell supplies transport,
identity, authority checks, schema validation and envelopes; sprintctl keeps
work reads, claim arbitration, lifecycle transitions, evidence ingestion,
batch ordering and project behavior in its own application package.

`sprintctl.application` is Click-independent. `sprintctl.vuoro_adapter` has no
import-time dependency on `vuoro-service`; service composition imports the
pinned domain release and calls `register_work_catalog`. Standalone and marked
recovery SQLite authorities continue using the legacy CLI. Normal shared
service composition uses `WorkApplication.postgres()` only after the service
compatibility gate has passed; importing or registering the adapter performs
no migration or DDL.

## Catalog v1

| Surface | Operations | Idempotency |
| --- | --- | --- |
| Reads | `work.read.sprints`, `work.read.item`, `work.read.context`, `work.read.context-candidates`, `work.read.next-work`, `work.read.records`, `work.read.decisions` | key forbidden |
| Item edit | `work.item.edit` | key forbidden; required `expected_revision` compare-and-swap |
| Claim start | `work.claim.start` | key forbidden; one-shot create plus activation flow |
| Durable claims | `work.claim.arbitrate` | key equals immutable command `event_id` |
| Lifecycle | `work.lifecycle.arbitrate` | key equals immutable command `event_id` |
| Evidence | `work.evidence.ingest` | key equals canonical record-batch digest |
| Batching | `work.batch.apply` | key equals canonical record-batch digest |
| Project | `work.project.context`, `work.project.sprints`, `work.project.items`, `work.project.next-work`, `work.project.batch` | aggregates require a canonical binding and authorization for every member; writes use canonical ordered-project-batch digest |
| Maintenance capability | `work.read.maintenance-capability`, `work.maintenance.prepare`, `work.maintenance.transition` | read forbids a key; mutations require the invocation key to equal the immutable request ID |
| Maintenance recovery evidence | `work.maintenance.recovery-record` | key equals the immutable recovery record ID; the result always declares `authority=none` |
| Cutover evidence | `work.pilot.cutover-evidence` | key forbidden |

Every operation declares JSON Schema 2020-12 input and result contracts,
authority, execution semantics, idempotency behavior and required client
schema features. Record schemas use local `$defs` references only.
`work.pilot.cutover-evidence` is intentionally catalog-described rather than
hard-coded into the client, so an already-installed protocol-v1 client can
refresh discovery and invoke it.

`work.read.item` includes an `edit_revision` derived from the item identity,
the current description digest, and the append-only count of prior
`item-edited` events. `work.item.edit` requires that revision and locks the
item while comparing it. A successful edit updates the description and
appends one `item-edited` event in the same transaction; the event records the
old/new revisions and descriptions. Existing events and item identity are
never rewritten. A stale revision is rejected as `item-edit-conflict`, and an
unchanged description is rejected without creating another revision.

`work.read.context` is the server-side aggregate for `usage --context`. It
returns the ContextContract v1 itself (rather than adding an envelope field),
and PostgreSQL evaluates all of its sprint, claim, item, dependency, stale
work, and decision reads in one repeatable-read, read-only transaction. A
client must not recreate this operation by stitching together raw read calls.

`work.read.context-candidates` is the server-side Tier-1 dispatch packet for
ActionQ and other bounded workers. It uses the established deterministic
ranking function over one repository's ready items and refs. An explicit
pending target is the only claim-eligible result; invocation never claims or
starts work.

## Authority and retry semantics

Claims and lifecycle transitions accept the existing immutable
authority-command producer record. Before arbitration, the application
reparses the nested command and requires its canonical form. The outer record
actor, nested command actor and authenticated identity must match; for
`claim.acquire`, the requested claim agent must match them too. A
single-command invocation's basis revision and idempotency key must match the
canonical record. Claim proof bytes are resolved by service composition and
are never accepted in authority-command invocation arguments or the catalog.
`work.identity.current` returns only the authenticated work actor and
repository scope. Served lifecycle clients use it before minting a durable
command so a local OS username or operator-supplied label cannot create a
permanently unflushable actor-mismatch record; credentials and token material
are never returned.

For `work.batch.apply` only, an already-durable authority command with an
actor or claim-agent mismatch is admitted in its producer order and receives a
durable `command.rejected` decision with reason `actor-mismatch`. This consumes
the immutable origin sequence without applying a domain effect, allowing the
next record to replay. Direct single-command operations still reject the same
mismatch before authority admission; the batch exception exists solely for
recovery of an existing ordered producer log.

`work.claim.start` is the transitional Click-free equivalent of the legacy
one-shot command: it creates an exclusive execute claim for the authenticated
actor, moves a non-active item to active with that proof, and releases the new
claim if the transition fails. Its response necessarily returns the new claim
proof to its authenticated caller. Because this composition has no durable
request ledger, its catalog contract forbids idempotency keys and callers must
not retry an unknown outcome. Retry-safe shared-authority clients use an
immutable `claim.acquire` record with `work.claim.arbitrate` instead.

The application delegates arbitration to `sprintctl.authority`. PostgreSQL
records the request and accepted or rejected decision atomically. Repeating an
identical record returns the original decision with `duplicate=true`; reusing
its stream position or event ID for different content is rejected. Stale basis
is a durable domain rejection and never mutates the target.

Evidence ingestion delegates to `sprintctl.pg.ingest_records`. A record batch
keeps producer order: adjacent observations are admitted atomically, each
authority command is decided at its position, then the next observation run
is admitted. Batch keys are content-bound, and record-level admission makes a
retry after a partial or lost response safe. A batch may carry multiple
command basis revisions by omitting the invocation-level basis; each immutable
command retains its own required basis revision.

Project batches follow the project binding's declared member order. Each
member stays repository-scoped and writes only the work domain. The response
retains `origin_repo` and exposes each member result. The operation is
retry-safe at the record level, not a cross-repository transaction.
Repository-local ingestion cursors mean two member results may carry the same
numeric `ingest_offset`; the enclosing member `origin_repo` / `repo_id` is part
of that cursor identity and must be retained by clients.

Concurrency evidence is deliberately bounded: PostgreSQL claim arbitration
locks the authoritative work-item row. Independent connections demonstrate
that two unrelated overlapping exclusive claim commands produce one accepted
and one rejected decision. This is `concurrency-tested` application-invariant
evidence, not a general fencing or cross-operation linearizability claim.

## Maintenance capability boundary

The served maintenance operations expose the lifecycle owned by
`sprintctl.maintenance_capability`; they do not introduce a second state
machine. Preparation accepts the complete frozen `maintenance-envelope/v1`
and derives the operator from the authenticated invocation identity. The
catalog rejects unknown top-level envelope and operation fields, while the
domain validator remains the normative exact validator for every nested
contract object. Client-supplied authority time is not accepted: the
application supplies its trusted current time to the lifecycle store.

`work.maintenance.transition` exposes only the lifecycle actions `attest`,
`activate`, `observe`, `reconcile`, `abort`, and `revoke`. Every mutation binds
the Vuoro invocation idempotency key to its request ID. The semantic request
digest deliberately excludes the server observation time, so retrying the
same request after response loss returns the first receipt as a duplicate;
changed bytes under the same identity are rejected. Expected revisions remain
the capability store's compare-and-swap tokens and stale revisions produce a
stable `maintenance-revision-conflict` response.

The read projection returns lifecycle identity, state, revision, sequencing,
and timestamps only. It does not expose the frozen envelope bytes, receipts,
reconciliation bundle, or recovery records. Recovery callers require the
narrow `work:maintenance-audit` authority. Their `observation` and
`requested-command` records are append-only audit evidence and never grant or
exercise execution authority. Repository scope is inherited from the served
application binding on both SQLite and PostgreSQL.

## Transitional CLI parity inventory

The legacy command surface remains available over the same sprintctl backend,
record contracts and authority handlers during rollout:

| Legacy surface | Served operation |
| --- | --- |
| `sprintctl sprint list --json` | `work.read.sprints` |
| `sprintctl item show --id ID --json` | `work.read.item` |
| `sprintctl item edit --id ID --description TEXT` | `work.item.edit` |
| authenticated durable-command actor discovery | `work.identity.current` |
| `sprintctl next-work --json` | `work.read.next-work` |
| claim start | `work.claim.start` |
| claim heartbeat, handoff and release | `work.claim.arbitrate` |
| item and sprint status transitions | `work.lifecycle.arbitrate` |
| observation upload | `work.evidence.ingest` |
| authority synchronization | `work.batch.apply` |
| project next-work and dispatch ordering | `work.project.next-work`, `work.project.batch` |
| `sprintctl pilot cutover-evidence` | `work.pilot.cutover-evidence` |

The inventory is also machine-readable as
`sprintctl.vuoro_adapter.LEGACY_REMOTE_COMMAND_PARITY`. It is retirement parity
evidence, not authorization to remove direct mode. Endpoint/identity cutover
and backend retirement remain separate governed items.
