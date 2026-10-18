# EC-05 Sprintctl reference-provider source inventory

Discovery for sprintctl#2738 at `20e120fb54b51bef001e4d5d57437388e8b8bc58`,
2026-10-18. This inventory records existing owner declarations and source
behavior. It introduces no IDL, API, schema, provider interface, native state,
or deployment change. The companion [JSON](ec05-source-inventory-20261018.json) contains
all 107 complete `WORK_OPERATION_CONTRACTS` declarations, input/result schemas,
normalized declaration/schema hashes, identifier/CAS/paging field evidence,
source locations, and hashes for 98 owner source/configuration files.

## Findings before implementation

1. **Implementation is gated on the selected foundation.** The canonical
   [estate contract rewrite](https://github.com/bayleafwalker/agentops/blob/e61a1f10766912c67d490ce6f4a5e42bf4e31229/docs/plans/agentops/2026-10-18-estate-contract-rewrite.md#ec-05--migrate-work-request-release-and-continuation-provider-boundary)
   assigns EC-05 to sprintctl#2738 and explicitly requires EC-04
   (vuoro#2737). Its prerequisite chain is EC-00 qualified architecture ADR
   (vuoro#2734), EC-02 foundation/ownership (vuoro#2735), EC-03 reproducible
   generation/conformance (agentops#2736), then EC-04 composition/transport.
   Discovery can proceed now; this report does not establish that those gates
   have passed or choose their IDL, transports, package names, or versions.
2. **Source declarations do not qualify a deployed provider.** There are 107
   declarations: 55 read, 48 write, three admin, one enqueue. Three maintenance
   resource operations are conditionally published only when the application
   says resource storage is available. Service pins, installed modules, deployed
   catalog contents, identity-authorized visibility, capability negotiation,
   and alternative-provider conformance were not probed. Current source names
   and version constants are not proof of any deployed catalog.
3. **Envelope and payload idempotency are different.** Seventeen declarations
   require the catalog/envelope key; 90 disallow it. Several of the latter
   require a domain `idempotency_key` inside arguments and use owner persistence.
   Neither field should be renamed or collapsed during discovery. Key scope,
   digest binding, original-result replay, retention/expiry, and unknown-effect
   handling need an operation-specific conformance map, not an inference from
   one metadata flag.
4. **Reads are not one uniformly bounded snapshot/stream contract.** There are
   real repeatable-read snapshot helpers and operation-specific bounded pages,
   but older list/read schemas still allow omitted or null limits without a
   catalog maximum. The metadata dataclass has no explicit universal deadline,
   cancellation, cursor-expiry, consistency, freshness, or stream-reset fields.
   These gaps in declarations are discovery findings; they do not prove every
   runtime read is unbounded or unsafe.
5. **Operator request and continuation acknowledgement remain proposed.**
   Existing run predecessor/continuation reads, approvals, inbox receipts, and
   terminal work decisions are related building blocks, not an implemented
   OA-01 request lifecycle or OA-04 continuation acknowledgement. No operation
   declaring those complete lifecycles appears in this source catalog.
   Ownership and exact acceptance remain foundation/consumer work. A message
   receipt, invocation receipt, or completion report cannot substitute for
   authoritative work acceptance.
6. **Repository inventory remains an explicit unknown.** Legacy sprintctl#985
   asks for remote repository inventory JSON. No owner-declared auth-filtered
   repository-inventory operation appears here. Project aggregates over
   configured members do not prove discoverability of every repository visible
   to an identity. A catalog read is also not an auth-filtered repository list.

## Source provenance and interpretation

`WORK_API_VERSION` is `work-api/v1`; `WORK_SCHEMA_VERSION` is `work-schema/v1`.
These are owner source labels, distinct from PostgreSQL migration version,
SQLite schema version, package release, and a deployed server's capabilities.
`WorkOperationContract` in `sprintctl/vuoro_adapter.py:54` declares the name,
input/result JSON Schemas, required authority, execution semantics, envelope
idempotency, schema features, optional resource result kind, and deprecation.
`catalog_operation_specs()` at line 3203 projects those declarations through
`vuoro_adapter_kit`; `register_work_catalog()` composes actual handlers into
Vuoro. The adapter-kit/service/client are external dependencies and their
installed implementation or deployed release is not hashed as owner source
by this report. Source package constraints remain in the hashed `pyproject.toml`.

JSON hashes are SHA-256 over sorted, compact UTF-8 JSON. File SHA-256 hashes
cover bytes; Git blob IDs are recorded separately. The rewrite itself is
hashed in the JSON so later changes to the discovery authority remain visible.
Field extraction records input versus result provenance and keeps exact
schema constraints; it is a lexical/schema inventory, not generated new types.
The operation family labels below are editorial groupings, not new interfaces.

## IDs, scope, authority, and settlement

Repository-local numeric IDs identify sprints, items, reservations, refs, deps,
and events inside a repository scope. Aggregate UUIDs, version/revision tokens,
Release digests, Decision digests, run/lease/intent/attempt IDs, message sequence
positions, evidence chain positions, and resource cursors have distinct roles.
The JSON retains patterns/minima/requiredness as declared, including structured
checkpoint and expected-snapshot fields. It does not normalize unlike IDs into
a generic string or assume global uniqueness for local integers.

`catalog_operation_specs()` sets `repo_scoped=True` except for `work.project.*`.
The application resolves request repository context and the service is expected
to authorize it; a caller's actor/session metadata is not authority.
Project operations require `work:project-read` or `work:project-write` and use
canonical authorized project bindings/member scope in
`sprintctl/project_application.py`. They do not form an atomic cross-repository
database or a repository discovery API. Exact deployed membership and filtering
are unknown here. Cross-provider work identity, wrong-kind IDs, repository
collisions, and dependency preservation remain ER-004/ER-027 qualification.

`work.run.resolve-v1` and `-v2` declare no catalog authority string. That is not
anonymous data access: the handlers validate owned run identity/delegation
bindings. Those rules must survive provider replacement. The complete authority
labels are retained per operation rather than compressed into a single read/write
permission. Application checks and protected effect capabilities can be stricter
than the catalog label alone.

Advisory reservations allow/report overlaps; explicit interruption displaces
execution reservations. They are distinct from the durable work lease used by
hosted workers. Release freeze, expected revision/CAS, terminal Decision, evidence
receipt, and applied-effect report are different state transitions. The recent
SQLite reservation fix in `20e120f` keeps its row, Release, interruptions, and
lifecycle events atomic; it establishes that bounded operation rather than
universal SQLite/PostgreSQL or alternative-provider parity.

## Current persistence ownership

| Surface | Existing persistence/source owner | Qualification boundary |
|---|---|---|
| Local work authority | `db.py`: SQLite sprint, track, work_item, event, ref, dep, reservation, work_release, work_decision and recovery history | Local authority/recovery remains valid; hosted-only features are not inferred to exist on SQLite. |
| Served work authority | `pg.py`, `pg_migrations.py`: repository-scoped PostgreSQL work rows, immutable Release/Decision/evidence, ingest/recovery ledgers | Deployment owns migration application; client startup does not gain runtime DDL authority. |
| Cross-project views/batches | `project_application.py`: authorized member applications and origin tags | No universal transaction/snapshot across providers or projects is claimed. |
| Runs and correlated invocation receipts | `pg.py`: run, run_predecessor, work_run_invocation_binding, work_run_invocation_attempt | Identity/workspace/client/grant binding and lost-result recovery must remain intact. |
| Evidence and session notes | `pg.py`: evidence_item, session_note, work_idempotency_ledger; ingest_record/stream/cursors for delivery | Sequence admission and replay cursors differ from work acceptance and from message ordering. |
| Hosted durable lease/outcome | `pg.py`: work_lease, work_outcome_report | TTL/heartbeat and fenced outcome ownership remain separate from advisory reservations. |
| Effect intent/acceptance | `pg.py`: work_effect_intent plus effect modules/export snapshot logic | Acceptance is exact revision/digest evidence; preview or report does not authorize a new protected effect. |
| Protected attempts | `effect_attempt_pg.py`, `effect_attempt_schema.py`: work_effect_attempt and work_effect_attempt_event | Open/redeem/seal/report, authorization digest, target binding, and replay are owner protocols. |
| Directory/inbox | `agent_inbox_pg.py`, `agent_inbox_schema.py`: agent_instance, declaration revision, observation, message, receipt, sequence counter | Registration/observation freshness and delivery receipts are not operator resolution or terminal settlement. |
| Structured approval | `approval.py`, `approval_schema.py`: append-only work_approval | Approval of a named transition is not the complete operator-request lifecycle. |
| Run delegation | `delegation_pg.py`, `delegation_schema.py`: work_run_delegation | Scope, operation grant, expiry/revocation, binding and resolver revalidation must survive transport changes. |
| Maintenance | maintenance capability/resource modules and PG/SQLite storage: capability, receipts, resource history, recovery records | Exact-plan activation/admission, CAS, bounded observation, and protected authority remain distinct. |

The JSON's 88 DDL mentions are navigation evidence across migration history,
not an asserted live schema. They include historical/conditional declarations
and objects later removed; use a real qualified schema inspection before any
migration or deletion decision. No database was opened for this inventory.

## CAS, idempotency, errors, and retry

Input schemas carry operation-specific `expected_revision`, item revision,
Release digest, intent revision/canonical digest, causal basis, checkpoint,
attempt authorization, and other expected values. The JSON lists those fields
without claiming each has the same CAS policy. Examples include item edit,
sprint kind, maintenance transition, and attempt open. Lifecycle arbitration
and typed Decisions have their own source record/basis and ledger semantics;
a successful command receipt is not a substitute for a terminal Decision.

`work_application.py:574` determines PostgreSQL connection-loss pure reads by
`work.read.*` or an explicit read set from `application_common.py`. A failed
eligible read may be attempted once on a replacement connection. A small
explicit mutation set is replayed automatically only when context declares
required idempotency and supplies the envelope key. Other mid-flight commands
return unknown-outcome guidance. Domain-keyed mutation handlers retain their
own exact retry semantics; blanket retries cannot be inferred from execution
semantics alone. `cli_runtime.py:394` translates served failures to the CLI and
does not itself define a broad automatic transport retry loop.

The five `work.project.*` reads are not members of that WorkApplication retry
classifier; they use ProjectWorkApplication's separate aggregation path. The
inventory marks this distinction rather than alleging a newly reproduced
retry defect. Per-operation deadlines/cancellation, overload behavior, service
retries and retention are not all declared here and remain qualification work.

The JSON records 234 literal `ApplicationRejection` construction sites with
source lines, plus handler pointers where statically resolvable. Representative
families include validation/invalid arguments, stale basis/revision conflicts,
not-found, unsupported backend, project unavailable, denial/binding refusal,
limit/cursor conditions, and PostgreSQL runtime unavailable. Dynamic domain
exception codes and service-level validation/auth/transport errors are not a
closed error vocabulary in that extract. `vuoro_adapter.py` maps application
rejection code/message/status into the service rejection type. EC-02/04 must
supply the canonical typed-error contract without treating outage or denied
visibility as an empty result.

## Paging, bounded reads, snapshots, and observation

- `work.read.records`, `work.read.decisions`, and `work.read.events` use
  `after_offset` and nullable/optional positive `limit`; `_pagination()` in
  `application_common.py:524` does not impose a universal maximum. Records
  also expose stream high-water evidence. Those are pull/recovery surfaces,
  not proof of a resumable event subscription with reset/compaction semantics.
- `work.run.predecessor-context-v1` declares limit default 100/max 500 and
  separate `after_note_id` / `after_chain_seq` cursors. Continuation delta
  declares limit default 32/max 64, checkpoint and expected snapshot digest,
  bounded reference/decision pages, explicit completion/truncation fields and
  independent successor cursors. Preserve each field's meaning and expired or
  changed snapshot outcomes; do not collapse them into one offset.
- `work.message.read-v1` declares `after_seq` and required limit max 100,
  with `next_after_seq`. That sequence is an inbox delivery scope, not the
  ingest offset, evidence chain, or a global estate event clock.
- `work.read.handoff` requires `events_limit` (runtime max 500) and uses
  the backend snapshot helper when available. Context, explain and selected
  effect reads also have explicit snapshot paths. A consistent read during
  construction is not a durable, permission-scoped cursor across future reads.
- Maintenance resource get/changes/prepare are conditionally published.
  Changes requires cursor and `wait_seconds` in 0–30; registration advertises
  `BoundedLongPollCapability(maximum_wait_seconds=30)` with resource observation
  cursor/terminality. Bounded long poll is not evidence that arbitrary gRPC,
  Connect, WebSocket, or SSE streaming is implemented or selected.
- Other catalog list reads expose varying limit/cursor/none schemas. Full
  fields and bounds are in the JSON. Uniform cursor revocation, tenant binding,
  consistency, freshness, disconnect/restart, compaction/reset and provider
  portability must be qualified under ER-011/ER-014, not assumed from the
  presence of a `limit` field or a schema name.

## Handoff, diagnostics, inventory, and proposed operator flow

`work.read.handoff` is read/not-allowed/work:read. Its handler constructs a
bundle and does not record its generation. `work.handoff.record` is
write/not-allowed/work:write; it validates a v1 handoff bundle and records the
generation event. The CLI's post-output recording step is a separate mutation.
A provider migration must not silently turn the read into a mutation or claim
that a failed recording invalidates a bundle already produced.

Local debug/diagnostic behavior and served diagnostics have different authority
boundaries. `doctor.py` reads a local SQLite schema through a read-only URI,
while served doctor checks facade-required operation names/schema/identity
through the configured service. `work.maintain.check` is a typed server-owned
read. Neither capability grants arbitrary SQL against hosted PostgreSQL or
proves all proposed #918 debug/event-stream helpers are implemented. Local
process output, sensitive storage paths and credentials need their own redaction
rules; a generic debug-mode flag is not a new provider contract here.

`served.catalog_operation_names()` calls `client.catalog()`; its source
explicitly describes discovery as unauthenticated. That validates advertised
names for doctor, not auth-filtered repository enumeration. #985 remains
unknown pending its owner-defined visibility contract and tests. Project
membership, catalog exposure, authenticated repo scope and actual deployed
source version are four distinct facts.

OA-01/OA-04 request/resolution/continuation acceptance is proposed scope in the
canonical EC rewrite. Current `work.approval.*`, `work.message.receipt-v1`,
`work.run.continuation-delta-v1`, and outcome/Decision operations are existing
names with narrower meaning. No new request/ack names or payloads are proposed
by this inventory. Unsupported runtime wake must eventually show the required
continuation-ready state, but this report does not mark that behavior delivered.
EC-09 owns native runtime adapters; do not introduce an estate scheduler or
substitute a message receipt for an affected-work continuation acknowledgement.

## Qualification work retained as explicit unknowns

| Requirement | Existing source evidence | Evidence still needed from qualified owners/providers |
|---|---|---|
| ER-004 identity | Distinct ID patterns, repository scope, run/delegation binding, origin tags | Cross-provider stable work identity, wrong-kind rejection, tenant/repo collisions and continuation preserving work while changing attempt. |
| ER-011 queries | Per-operation cursor/limit fields and selected snapshot helpers | Concurrent mutation, revoked permission, expired/reset cursor, freshness and unavailable-source behavior with no cross-tenant cursor leakage. |
| ER-013 commands | Existing revision checks, canonical digests, owner ledgers and replay paths | Scoped key/retention semantics, exact replay versus mismatched payload, uncertain commit handling and alternative-provider conformance. |
| ER-014 delivery | Ingest offsets/high-water, evidence chain positions, inbox sequence and bounded resource changes | Stable ordering scopes, disconnect/restart/duplicate/out-of-order/reset/compaction proof on selected transports. |
| ER-025 operator | Structured approvals, inbox, run/effect evidence as separate primitives | Settled request ownership and complete OA-01–06 lifecycle across real runtimes, TUI and failure/attestation cases. |
| ER-026 continuation | Predecessor context/delta, delegation and exact invocation receipt reads | Affected-work resolution, duplicate delivery suppression, exact successor evidence, supported native wake or visible continuation-ready fallback, acknowledgement. |
| ER-027 work settlement | Reservations, Releases, CAS, Decisions and dependency storage | Existing plus new provider conformance; protected acceptance cannot be replaced by source receipts or completion reports. |

No live/deployed tests were run for this source inventory. Completeness checks
cover unique declared operation names, all declaration fields, reproducible
schema/declaration hashes, source-file hashes, conditional registrations, and
counts. The companion JSON remains evidence of this exact source revision,
not a certification of all persistence, deployment, auth, transport, or runtime
semantics. Findings are returned for owner review before any source action.

## Full owner-declared operation index

Envelope `key` below is catalog idempotency, not a payload-level domain key.
`repo` means envelope repository scope; `project` uses member application scope.
The JSON carries the complete schemas, deprecation/resource metadata and hashes.

### work lifecycle and repository reads (35)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.identity.current` | read | not-allowed | `work:read` | repo |
| `work.read.sprints` | read | not-allowed | `work:read` | repo |
| `work.read.item` | read | not-allowed | `work:read` | repo |
| `work.read.item-projection` | read | not-allowed | `work:read` | repo |
| `work.validate.item-status-mutation` | read | not-allowed | `work:read` | repo |
| `work.read.items` | read | not-allowed | `work:read` | repo |
| `work.read.context-candidates` | read | not-allowed | `work:read` | repo |
| `work.read.context` | read | not-allowed | `work:read` | repo |
| `work.maintain.check` | read | not-allowed | `work:read` | repo |
| `work.read.next-work` | read | not-allowed | `work:read` | repo |
| `work.read.handoff` | read | not-allowed | `work:read` | repo |
| `work.read.next-work-explain` | read | not-allowed | `work:read` | repo |
| `work.read.records` | read | not-allowed | `work:read` | repo |
| `work.read.decisions` | read | not-allowed | `work:read` | repo |
| `work.read.events` | read | not-allowed | `work:read` | repo |
| `work.read.sprint` | read | not-allowed | `work:read` | repo |
| `work.read.sprint-detail` | read | not-allowed | `work:read` | repo |
| `work.sprint.create` | write | not-allowed | `work:sprint` | repo |
| `work.sprint.kind` | write | not-allowed | `work:sprint` | repo |
| `work.event.add` | write | not-allowed | `work:evidence` | repo |
| `work.item.create` | write | not-allowed | `work:lifecycle` | repo |
| `work.item.edit` | write | not-allowed | `work:lifecycle` | repo |
| `work.item.priority` | write | not-allowed | `work:lifecycle` | repo |
| `work.item.ref.add` | write | not-allowed | `work:lifecycle` | repo |
| `work.item.ref.remove` | write | not-allowed | `work:lifecycle` | repo |
| `work.item.dep.add` | write | not-allowed | `work:lifecycle` | repo |
| `work.item.dep.remove` | write | not-allowed | `work:lifecycle` | repo |
| `work.lifecycle.arbitrate` | write | required | `work:lifecycle` | repo |
| `work.item.note` | write | not-allowed | `work:evidence` | repo |
| `work.read.item-decisions` | read | not-allowed | `work:read` | repo |
| `work.read.release` | read | not-allowed | `work:read` | repo |
| `work.read.unbound` | read | not-allowed | `work:read` | repo |
| `work.batch.apply` | write | required | `work:batch` | repo |
| `work.read.reservations` | read | not-allowed | `work:read` | repo |
| `work.read.reservation` | read | not-allowed | `work:read` | repo |

### public work views (2)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.public.list-v1` | read | not-allowed | `work:read` | repo |
| `work.public.item-v1` | read | not-allowed | `work:read` | repo |

### runs and continuation context (7)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.run.register-v1` | write | not-allowed | `work:evidence` | repo |
| `work.run.register-correlated-v1` | write | required | `work:evidence` | repo |
| `work.run.invocation-receipt-get-v1` | read | not-allowed | `work:read` | repo |
| `work.run.resolve-v1` | read | not-allowed | `None` | repo |
| `work.run.predecessor-context-v1` | read | not-allowed | `work:read` | repo |
| `work.run.continuation-delta-v1` | read | not-allowed | `work:read` | repo |
| `work.run.resolve-v2` | read | not-allowed | `None` | repo |

### evidence and delivery (4)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.evidence.tail-v1` | read | not-allowed | `work:evidence` | repo |
| `work.evidence.evaluate-v1` | read | not-allowed | `work:read` | repo |
| `work.evidence.append-v1` | write | not-allowed | `work:evidence` | repo |
| `work.evidence.ingest` | enqueue | required | `work:evidence` | repo |

### run notes (1)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.session-note.write-v1` | write | not-allowed | `work:evidence` | repo |

### durable work leases (5)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.lease.acquire-v1` | write | not-allowed | `work:claim` | repo |
| `work.lease.heartbeat-v1` | write | not-allowed | `work:claim` | repo |
| `work.lease.report-outcome-v1` | write | not-allowed | `work:claim` | repo |
| `work.lease.complete-v1` | write | not-allowed | `work:claim` | repo |
| `work.lease.read-v1` | read | not-allowed | `work:read` | repo |

### effect intents and acceptance (13)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.effect.propose-v1` | write | not-allowed | `work.effect.propose` | repo |
| `work.effect.propose-bound-v1` | write | not-allowed | `work.effect.propose` | repo |
| `work.effect.get-v1` | read | not-allowed | `work.effect.get` | repo |
| `work.effect.export-proposal-v2` | read | not-allowed | `work.effect.get` | repo |
| `work.effect.export-proposal-v3` | read | not-allowed | `work.effect.get` | repo |
| `work.effect.basis-v1` | read | not-allowed | `work.effect.get` | repo |
| `work.effect.acceptance-snapshot-v1` | read | not-allowed | `work.effect.get` | repo |
| `work.effect.preview-v1` | read | not-allowed | `work.effect.get` | repo |
| `work.effect.list-proposed-v1` | read | not-allowed | `work.effect.list-proposed` | repo |
| `work.effect.list-accepted-v1` | read | not-allowed | `work.effect.list-accepted` | repo |
| `work.effect.accept-v1` | write | not-allowed | `work.effect.accept` | repo |
| `work.effect.reject-v1` | write | not-allowed | `work.effect.reject` | repo |
| `work.effect.mark-applied-v1` | write | not-allowed | `work.effect.mark-applied` | repo |

### handoff recording (1)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.handoff.record` | write | not-allowed | `work:write` | repo |

### terminal work decisions (1)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.decision.record` | write | required | `work:lifecycle` | repo |

### cross-project aggregation (6)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.project.context` | read | not-allowed | `work:project-read` | project |
| `work.project.sprints` | read | not-allowed | `work:project-read` | project |
| `work.project.items` | read | not-allowed | `work:project-read` | project |
| `work.project.next-work` | read | not-allowed | `work:project-read` | project |
| `work.project.next-work-explain` | read | not-allowed | `work:project-read` | project |
| `work.project.batch` | write | required | `work:project-write` | project |

### advisory reservations (5)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.reservation.reserve` | write | required | `work:write` | repo |
| `work.reservation.reserve-v1` | write | required | `work:write` | repo |
| `work.reservation.touch` | write | required | `work:write` | repo |
| `work.reservation.reassign` | write | required | `work:write` | repo |
| `work.reservation.release` | write | required | `work:write` | repo |

### maintenance authority and resources (7)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.read.maintenance-capability` | read | not-allowed | `work:maintenance` | repo |
| `work.maintenance.prepare` | admin | required | `work:maintenance` | repo |
| `work.maintenance.resource.get` | read | not-allowed | `work:maintenance` | repo |
| `work.maintenance.resource.changes` | read | not-allowed | `work:maintenance` | repo |
| `work.maintenance.resource.prepare` | admin | required | `work:maintenance` | repo |
| `work.maintenance.transition` | admin | required | `work:maintenance` | repo |
| `work.maintenance.recovery-record` | write | required | `work:maintenance-audit` | repo |

### protected effect attempts (5)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.effect.attempt-open-v1` | write | not-allowed | `work.effect.mark-applied` | repo |
| `work.effect.attempt-redeem-v1` | write | not-allowed | `work.effect.mark-applied` | repo |
| `work.effect.attempt-seal-unused-v1` | write | not-allowed | `work.effect.mark-applied` | repo |
| `work.effect.attempt-report-applied-v1` | write | not-allowed | `work.effect.mark-applied` | repo |
| `work.effect.attempt-get-v1` | read | not-allowed | `work.effect.mark-applied` | repo |

### agent directory (5)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.agent.register-v1` | write | not-allowed | `work:evidence` | repo |
| `work.agent.observe-v1` | write | not-allowed | `work:evidence` | repo |
| `work.agent.retire-v1` | write | not-allowed | `work:evidence` | repo |
| `work.agent.list-v1` | read | not-allowed | `work:read` | repo |
| `work.agent.describe-v1` | read | not-allowed | `work:read` | repo |

### agent inbox (5)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.message.send-v1` | write | not-allowed | `work:evidence` | repo |
| `work.message.read-v1` | read | not-allowed | `work:read` | repo |
| `work.message.status-v1` | read | not-allowed | `work:read` | repo |
| `work.message.receipt-v1` | write | not-allowed | `work:evidence` | repo |
| `work.message.cancel-v1` | write | not-allowed | `work:evidence` | repo |

### structured approvals (2)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.approval.record-v1` | write | not-allowed | `work:approve` | repo |
| `work.approval.get-v1` | read | not-allowed | `work:read` | repo |

### run delegation (3)

| Operation | Semantics | Envelope key | Authority | Scope |
|---|---|---|---|---|
| `work.run.delegate-v1` | write | required | `work:evidence` | repo |
| `work.run.delegation-revoke-v1` | write | required | `work:evidence` | repo |
| `work.run.delegation-get-v1` | read | not-allowed | `work:read` | repo |


## Readiness delivery and retained decisions

Sprintctl#2769 retains this source survey for parent Sprintctl#2738. Its
completion means an inspectable inventory, not completion of EC-05. Current
production interfaces remain unchanged; the selected foundation and qualified
composition must precede their migration.

Two later observations remain separate from this frozen source capture:

- Dependency admission currently uses terminal `status=done` in legacy paths.
  Terminal `resolution` distinguishes acceptance, rejection, withdrawal and
  supersession. Whether an accepted result is required for a particular
  dependency is a foundation/owner policy decision, not an inferred defect or
  a license to change current admission. Preserve the distinction during
  #2095/#2215 and EC-05 qualification.
- The deployment owner's 2026-10-18 numeric-only session-note observation
  (native #2596/event5501) covered 41 notes, maximum 577 bytes and zero above
  16,384 bytes. That bounded observation supplies no future input contract,
  estate-wide maximum or serialized-page budget. Input owner, edge, replay and
  cursor progress remain qualification work; no raw notes are retained here.

## Check retained integrity offline

With the captured Git objects present, run from a Sprintctl checkout. This
checks original source bytes and stored declaration/schema hashes; it neither
executes owner operations nor authenticates historical evidence. It deliberately
uses the captured revision rather than comparing against a moving working tree.

```python
import hashlib
import json
from pathlib import Path
import subprocess

packet = json.loads(Path(
    "docs/evidence/ec05-source-inventory-20261018.json"
).read_text())

def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()

names = [operation["name"] for operation in packet["operations"]]
assert len(names) == len(set(names)) == packet["operation_count"]
for source in packet["source_files"]:
    raw = subprocess.check_output([
        "git", "show", packet["revision"] + ":" + source["path"]
    ])
    assert hashlib.sha256(raw).hexdigest() == source["sha256"]
    blob = subprocess.check_output([
        "git", "rev-parse", packet["revision"] + ":" + source["path"]
    ], text=True).strip()
    assert blob == source["git_blob"]
fields = ("name", "input_schema", "result_schema", "required_authority",
          "execution_semantics", "idempotency", "required_client_schema_features",
          "result_contract_resource_kind", "deprecation")
for operation in packet["operations"]:
    assert digest({key: operation[key] for key in fields}) == operation["declaration_sha256"]
    for kind in ("input", "result"):
        assert digest(operation[kind + "_schema"]) == operation[kind + "_schema_sha256"]
print("source and catalog inventory integrity verified")
```

The external governing document has a separate immutable URL, revision and byte
digest in `canonical_rewrite`. Validate it against that repository's original
bytes when available. A successful integrity check is not service discovery,
provider conformance, authorization, evidence acceptance or a new schema.
