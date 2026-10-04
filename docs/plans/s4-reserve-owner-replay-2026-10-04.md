# S4 reserve intake: native owner transaction and replay prerequisite

Status: proposed design preparation for agentops#2485, 2026-10-04.
This document changes no operation, schema, grant, producer queue or live state.
It precedes a reserve carrier; it does not establish full S4 absorption,
verified import, cutover, soak or legacy retirement. No cutover date is set.

The governing direction is TS-6 and Decision199, with TS-13 still proposed,
and the merged [S4 preparation](https://github.com/bayleafwalker/agentops/blob/49ac361481d185a2b0c3e18899d19756096473d1/docs/plans/2026-10-03-s4-evidence-home-preparation.md).
The first typed run-bound evidence carrier landed in Sprintctl PR121 at
`fa7c3c410379f6ff3be2bae40402db46a240d247`. Reserve and proposal intake remain
separate missing carriers. The earlier missing-gate fixture, draft PR119,
is preparation rather than an actual reserve/evidence/proposal gate.

## Measured owner contract and gap

Source baseline: Sprintctl `fa7c3c410379f6ff3be2bae40402db46a240d247`,
owner version 0.12.1. The relevant source is:

| Anchor | Existing behavior |
| --- | --- |
| `vuoro_adapter.py`, `work.reservation.reserve` contract | `work:write`, envelope idempotency required; closed arguments include item, actor, session, role, correlation, explicit interruption and optional expected revision. |
| `served.py::reservation_operation` | Supplies a fresh key per logical mutation by default; accepts an explicit retry key. Its deduplication claim is not implemented by reserve today. |
| `work_application.py::_reservation_reserve` | Validates authenticated actor when present and expected revision, then calls the backend without the envelope key. |
| `pg.py::reserve` | Acquires the repository advisory transaction lock shared with maintenance activation; checks active/observing, unexpired maintenance; locks Release basis when required; optionally freezes Release and interrupts execution reservations; inserts a fresh reservation and commits. |
| `pg.py::_reservation_event` | Appends interruption and reserved events after the reservation transaction has committed. |
| `work_application.py::_idempotent_write`, `pg.py::idempotent_write` | Existing native run/evidence/proposal pattern commits key claim, caller-cursor effect and stored result atomically; failed effects roll back the claim. Reserve does not use it. |

The catalog's required key is envelope validation, not a durable replay effect.
The inspected Vuoro service dispatch and catalog invocation merely validate and
forward that envelope (`9c02d8f9b7cc42f9b90c4a302b623b66d54b4266`,
`packages/vuoro-service/src/vuoro_service/app.py::_dispatch`,
`catalog.py::CatalogRegistry.invoke`). They do not supply a substitute native
reserve ledger. The generic resource-authority design agentops#2603 also does
not implement this operation.

Repeated same-key reserve calls can therefore create distinct advisory rows.
A lost reply cannot be retried as one logical reservation; deliberate
interruption can also run again. A lifecycle-event failure can leave the row,
Release freeze or interruption committed without the complete event history.
These are source-derived gaps, not a claim that a production incident occurred
or that a new disposable concurrency history has already been executed.

## Proposed native boundary

Repair the owner before storing durable producer reserve requests. Propose a
separate typed `work.reservation.reserve-v1` operation for the new durable
carrier guarantees, using existing work:write authorization with no new grant.
The v1 contract copies the existing reserve closed argument schema, declares
work:write / write / idempotency required, and keeps the closed top-level
repo_id/reservation result with the new snapshot and replay fields nested in
the open reservation object. Missing envelope keys are rejected before invoke.
The catalog already registers work.run.resolve-v1 and work.evidence.append-v1
(vuoro_adapter.py); their executable catalog tests establish that the -v1
operation suffix is supported.
The existing `work.reservation.reserve` operation and direct/local functionality
are not implicitly retired or redirected by this plan; its measured replay gap
remains documented until a separate client-owner migration decision. Implement a
PostgreSQL transaction participant for reserve that accepts the caller cursor
and never commits internally. Its entrypoint must preserve the existing
reservation semantics, while the hosted handler supplies an explicit native
replay scope and digest. Simply wrapping today's committing `pg.reserve` in
`_idempotent_write` would violate the atomic rollback contract.

The one transaction must cover all of:

1. Claim or inspect the native replay key in its authenticated scope.
2. On a new claim, acquire the existing repository maintenance lock and apply
   the existing maintenance admission test under that lock.
3. Validate the requested Release basis under the existing item lock; freeze
   the Release for an execution reservation, retaining its digest and contract.
4. Read advisory overlaps, perform only explicitly requested execution
   interruption, and insert one reservation.
5. Append every affected `reservation.interrupted` event and the one
   `reservation.reserved` event using that same transaction.
6. Store the complete initial receipt and commit once.

The result keeps the existing top-level `repo_id` and `reservation` envelope.
Choose the proposal precedent: `reservation` shows the current owner row after
creation or replay. Current overlaps may differ from admission overlaps when
another reservation commits before the post-commit read. Add a nested `admission_snapshot` carrying the immutable
creation row, initial conflict identities/severity and Release digest, plus a
nested `replayed` boolean. The current reservation object schema is open, so
these additions fit its schema; consumer compatibility and documented field
semantics still require tests and review before code. Retain the outer `conflict`, `conflicting_reservations` and `conflict_severity`
fields: recompute them from current active overlaps in one post-commit read
snapshot, excluding this reservation. An inactive reservation has no current
conflicts, while its initial overlap remains in the admission snapshot. This
preserves first-call overlap visibility and explicitly separates historical
from current conflict annotations. The native ledger stores the
initial snapshot; retain successful replay keys and their snapshots without
TTL/purge for this contract. Any later retention policy requires a new replay
horizon/expired-key contract before deletion; an old key must not become a new
reservation merely because a receipt was discarded. The implementation must
verify event helpers neither commit nor use another connection.
Unkeyed SQLite retains its existing ordering and documented logical event trail;
do not claim equivalent crash atomicity from that trail.
Replay resolves the same reservation ID for current state,
without repeating effects. If that retained row is unavailable, return a clear
terminal reservation-receipt-unavailable (409) refusal rather than recreate it. The reserve-specific ledger wrapper returns its claim/replay outcome as
internal metadata without changing existing evidence/proposal callers. Add
`replayed` only after that wrapper returns; it is never persisted inside the
stored receipt. Compose current state and overlap fields after commit, as for
proposal current-state replay. The snapshot is stable;
the outer current row and replay flag are deliberately not byte-identical
between calls. Owner read operations continue to return current state.

New maintenance admission checks apply only to a new ledger claim. A committed
replay may recover its receipt after maintenance activation without creating a
reservation. Authentication, capability and repository authorization still
precede replay. `invoke` calls `_note_implicit_activity` after handlers;
`reservation.ACTIVITY_OPERATIONS` currently excludes reserve. Keep both legacy reserve and work.reservation.reserve-v1 excluded
and prove that replay changes no activity timestamp, including if future
activity routing is changed.

## Identity and compatibility decisions required before code

Use `InvocationContext.idempotency_key` as the explicit native key source.
Parameterize the native wrapper to accept this key without adding a wire
argument; its current argument-key path for evidence/proposal stays unchanged.
Fix the ledger tool namespace to `reservation.reserve-v1`; it remains distinct
from every other native operation. The request digest includes this operation
namespace and exactly these normalized fields:

| Field | Normalization |
| --- | --- |
| `item_id` | Validated positive integer; booleans refused. |
| `actor`, `session_id` | Validated required text, with exactly the existing handler's text normalization. |
| `role` | Omitted means `execution`; hosted null remains schema-invalid; explicit allowed roles retain their value. |
| `interrupt_existing` | Omitted and false normalize to false; otherwise schema-valid true. |
| `correlation_ref` | Omitted and null normalize to null; a supplied string is preserved. |
| `expected_revision` | Omitted and null normalize to null; otherwise preserve the exact validated basis string returned by `validate_basis`. Plain edit and full Release revision remain distinct intent, despite possibly matching one current basis. |

The key itself is excluded from the digest. `acceptance_contract` is absent
from the hosted closed schema and therefore absent from this hosted digest;
use the existing normalized default contract for hosted execution freezes.
No direct-call acceptance contract may leak into a hosted retry. Original
producer bytes are a separate future carrier digest domain.

Choose fail-closed compatibility for the new typed durable operation only:
`work.reservation.reserve-v1` requires
PostgreSQL and authenticated principal/workspace binding. Check authenticated identity binding first, then backend availability. An unbound identity
receives `identity-unbound` (403); a hosted backend without the native replay
participant receives terminal `reservation-replay-unavailable` (422), after authentication/
capability/repository checks and before writes.
There is no success on this new typed operation that silently ignores the key.
Existing hosted callers are not automatically switched to it: the legacy
operation keeps its admission semantics and no durable replay claim, while
inheriting the direct PostgreSQL atomic-event fix described below. These are proposed
admission changes, not deployed policy. Inventory current supported identities
and agree a migration path with their owners before enrolling clients in this new operation;
affected clients are blocked prerequisites, never silently upgraded or granted.
The native ledger scope is repository/workspace/principal/tool/key, derived
from authenticated identity rather than actor/session. Preserve the existing
authenticated actor equality check. No principal/workspace is inferred.

The unkeyed local SQLite and direct PostgreSQL APIs retain their advisory call
semantics. This slice includes atomic events for direct PostgreSQL reserve:
its wrapper owns one transaction and invokes the same cursor participant,
without a ledger claim. This deliberately fixes its previous commit-then-events
failure boundary and requires compatibility regression. The participant never
commits; the direct wrapper and hosted ledger wrapper each commit only their
own transaction. Touch/reassign/release remain out of scope and their required
keys need separate measured treatment. Correct the served reserve deduplication
docstring to distinguish the legacy gap from the new typed guarantee; keep touch's fresh-key default unchanged.

## Participant writes and lock discipline

Move item existence lookup inside the participant and lock the item row in the
transaction even for observation/verification reservations without a basis.
Preserve the existing Release CAS/freeze behavior only where requested.
`create_event` commits internally today, so it cannot be called by the
participant. Use a transaction-bound event insertion path with the same
canonical payload validation, generic-event guard and transaction connection, and pass the already-locked
item/sprint identity rather than calling `_reservation_event`'s unbound lookup.
A vanished/missing item or required event write fails the transaction; the
current silent missing-item event skip is prohibited in the participant.

The proposed order for a new hosted effect is native ledger claim, repository
advisory lock, item row, ordered active reservation rows, then event/result
inserts. Replay only reads its committed ledger and retained reservation row;
it does not enter this new-admission lock path. Direct reserve omits the ledger
step. Maintenance activation (`PostgresMaintenanceCapabilityStore.transition`)
locks repository then capability row; it does not acquire the reserve ledger.
Lease admission uses repository then item then lease (`pg._lock_repo_for_claims`).
Existing keyed decisions use their separate decision-key advisory lock then
item; native evidence takes its distinct ledger scope then run-chain lock;
proposal takes its distinct ledger scope then item. No inspected path holds
the repository lock and then requests this reserve ledger key.

These author-checked anchors establish the intended partial order, not an
exhaustive deadlock proof. Before implementation, inventory every participant
and wrapper reachable under these locks, including maintenance resource
wrappers, touch/reassign/release, note_session_activity, any reservation expiry
path, foreign-key/event writes and transaction reuse. Refuse a design that
introduces a reverse repository-to-reserve-ledger dependency. Exercise reserve
and activation paused at each lock point with bounded deadlines and inspect
actual lock waits; do not infer safety from single-thread tests. Include the
service dispatch/catalog and Release helper excerpts in the implementation
review so their admission and freeze claims can be independently checked.

## Native refusal and replay semantics

A successful same-scope, same-key, same-digest retry returns the stored receipt
without another row, interruption, freeze, event or activity-clock advance.
A successful key reused with different semantic content is an idempotency
conflict. A different authenticated scope cannot discover another caller's
receipt through its key.

A failed new effect rolls back the claim and every effect write. It leaves no
permanent generic denial record: native corrected retry remains possible after
an invalid basis or other domain refusal. A producer must distinguish a known
owner refusal from a timeout, lost reply or malformed response. Unknown outcome
permits only exact retry/reconciliation, not silent replacement of arguments.
Owner replay alone cannot prove a producer's later correction was safe; the
future carrier needs a separately reviewed correction rule.

Reservations remain advisory. Independent keys for overlapping sessions create
visible overlaps rather than exclusive work leases. `interrupt_existing` is
an explicit takeover of active execution reservations only; verification and
observation reservations survive. Expected-revision CAS, Release freeze,
maintenance lock ordering and expiry evaluation retain their current meanings.
A reservation handle, actor, session, branch or commit never becomes a capability.

## Required disposable owner histories

These are proposed regression obligations, not results from this document.
Run through the real owner application and independent disposable PostgreSQL
connections, with repository scopes cleaned and counted afterwards:

| History | Required invariant |
| --- | --- |
| Uncontended first v1 call and legacy hosted call | Outer overlap fields match legacy annotate_conflicts for the same state; nested snapshot/replay additions are additive; legacy hosted inherits atomic PG events without replay or changed admission. |
| Two concurrent identical bound calls with one key | One reservation, one reserved event, one stored native receipt; both return the same immutable admission snapshot and reservation identity; exactly one reports replayed false. |
| Same key with different semantic intent after success | Conflict; no new row, event, interruption or Release change. |
| Failure after row/freeze/interruption, or during either lifecycle-event append | Entire claim and effect roll back; reservation and Release state match their pre-call state (which may already be frozen); corrected retry can succeed. |
| Commit followed by reply loss, then exact retry | Stored receipt recovered; no repeated event or explicit interruption. |
| Replay after reservation lifecycle changes | Current row returned with the original marked admission snapshot; no historical active state is presented as current. |
| Maintenance activation racing a new reserve | Existing shared lock prevents admission under active maintenance; no duplicate admission path. |
| Maintenance activation before a committed-key replay | Replay has no new effect and preserves authentication, without reevaluating new admission. |
| Stale expected revision and corrected request after failed effect | No freeze or ledger residue on refusal; native corrected retry retained. |
| Different keys and overlapping sessions | Advisory overlap visible; no accidental exclusive claim. |
| Explicit takeover alongside verification/observation | Only active execution rows interrupted, with complete single-transaction events. |
| Same key in distinct authorized principal/workspace scopes | No cross-scope result disclosure; identity migration behavior tested separately. |
| Legacy direct/unbound paths and default normalization | Unkeyed direct calls retain advisory behavior and atomic PG events; new typed hosted unbound/SQLite paths refuse before writes while legacy hosted admission is unchanged and inherits the PG atomic-event fix; equivalent normalized retries agree. |

Add deterministic failpoints around transaction boundaries, event writes and
`_record_idempotent_result`. Inject failure/connection loss during COMMIT and
prove exact retry recovers whichever outcome actually committed. Revoke
work:write or repository authorization before replay and prove no receipt is
disclosed. Interleave maintenance activation at every proposed lock boundary.
Thread/process synchronization must establish the claimed concurrent order.
A transport stub alone is insufficient evidence for this owner change. Use
existing native idempotency conflict mapping; prove response validation and
receipt correlation in the later producer carrier rather than faking authority
with an observation upload.

## Dependency order and remaining S4 acceptance gate

First review and settle the proposed typed-operation boundary, identity
compatibility and atomic owner reserve contract, then
implement and review its disposable histories. Only after that native source
lands may a typed producer reserve carrier capture original bytes, retain
attempt/confirmed states and use exact owner retry without live intake.
The run-bound evidence carrier already exists as bounded source progress; its
blocked unknown-outcome reconciliation and producer head-of-line behavior
remain explicit limits.

Proposal intake is a separate next design. Existing
`work_application.py::_effect_propose` already uses native idempotent write;
`pg.py::propose_effect_intent` also checks its scoped proposal key/digest and can
participate in the caller transaction. Retry preserves immutable intent identity
and canonical content digest while the owner returns current intent state.
A proposal carrier must preserve run/identity binding and original diff bytes,
never accept, apply, merge or sign. The native proposal schema has no evidence
or Release reference argument: causal reserve/evidence/proposal linkage needs
an explicit contract, not invented fields or a generic resource substitute.

The actual isolated stop/reserve/commit-trailer/evidence/propose/restart/sync
comparison with continuously-online effective state is still required before
full agentops#2485 acceptance. Legacy digest/provenance-preserving import,
rotated NDJSON hook transition, coordinated cutover and ninety-day read-only
soak remain separate prerequisites. No production import, authority stop,
classification, grant, hook change, legacy retirement or irreversible followup
is authorized by this preparation.
