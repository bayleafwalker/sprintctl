---
doc_id: sprintctl-outcome-settlement-owner-2026-11-06
status: draft
supersedes: null
---

# Work owner plan for the protected publication proof

The operator requested a strengthened, coordinated outcome-settlement plan on
6 November 2026. This draft supplies Sprintctl's bounded owner work for the
[Vuoro proposal](https://github.com/bayleafwalker/vuoro/blob/main/docs/plans/2026-11-06-outcome-settlement-proposal.md).
Landing the draft does not ratify it or report implementation, release,
deployment or field qualification. Work-item state stays in the served owner;
the labels below are plan slices, not new native item IDs.

## Outcome and existing contracts

A fresh authorized reader must reconstruct which result counted after an agent
publishes an analytics partition, loses its response, and is replaced. The target
must be reconciled before retry. Sprintctl owns current Work basis, attempts,
settlement and their existing evidence relationships. The protected resource
owner and target own effect authorization, execution and reconciliation.

Reuse these current source contracts, inspected at
`05f919e71c01f1294c822cb37b5b0f09d5a71fad`:

| Contract | Reuse and limit |
|---|---|
| [Reservation and durable lease model](../protocols/reservation-model.md) | Advisory reservations remain coordination signals. The separate durable lease fences work settlement; neither is a target write capability. |
| [Exact outcome basis](../protocols/exact-outcome-basis.md) | `work.lease.report-outcome-v2` checks the complete current continuation basis and preserves rejected reports. Discover support in the installed catalog; do not infer it from package version or v1 lease fields. |
| [Work currentness](../protocols/work-currentness.md) | `work.lease.currentness-v1` is a bounded owner snapshot with `authorizes_effect: false`. Its read does not provide an atomic external write fence or revive an expired grant. |
| [Resume and explain](resume-and-explain.md) | Reuse continuation, effect-acceptance snapshots and existing result evidence. The current explanation does not establish all historical joins or general supported export/import. |
| [Qualification handoff](resume-explain-qualification-handoff.md) | Preserve existing parent gates and native references. This operation is a concrete case for qualification, not a second competing roadmap or closure of those gates. |

## Bindings to close

Inventory the existing schema/catalog and qualified export surface before adding
fields or operations. Prefer typed references into the existing authority homes.
The minimum reconstructible case binds:

- Repository/work identity, exact Release and continuation basis; each run and
  lease; supersession/rejection history and the current outcome Decision.
- A durable business-operation identity and immutable intent digest, independent
  of any attempt, request ID or credential. The intent names the dataset/partition,
  source snapshot, transformation revision, candidate artifact, expected target
  revision and verifier profile. A changed intent requires a revised identity or
  revision and fresh evaluation.
- The resource owner's admission decision and its authority/policy revision;
  the target journal receipt with previous/new publication revisions; the
  independently checked artifact and target observation; and their provenance,
  visibility, completeness and retention limits.
- The target installation/incarnation and retained target-configuration revision.
  Evidence from a replacement behind the same logical alias cannot close an
  unknown effect at the original target.
- The accepting authority, current Work basis, verification contract and exact
  evidence used by the outcome Decision. A compensation, if performed, has its
  own authorized operation and receipt; the original history remains intact.

Do not copy target state into an authoritative Sprintctl execution state machine.
The result view may show a target observation and its timestamp, but unknown,
denied, unavailable and conflicting evidence remain distinct. A signed receipt,
successful HTTP response, done item or accepted effect intent cannot fill a
missing target observation. Keep report, receipt, acknowledgment, observation
and acceptance as separate classes.

The target operation identity provides correlation and semantic deduplication,
not authority. Reusing it under a successor attempt requires that attempt's own
current permissions. Target-side duplicate/conflict and resource fencing rules
belong to the [gateway plan](https://github.com/bayleafwalker/sovite/blob/main/docs/plans/2026-11-06-outcome-settlement-gateway.md).
Sprintctl must not claim that rejecting a stale outcome report prevents an
external write by a process holding unrelated credentials.

## Decision and recovery rules

Effect authorization occurs before the target operation under the resource
owner's policy. Outcome acceptance occurs afterward under customer-authorized
Work policy, bound to the exact result and verification profile. Both can be
automatic under standing delegation; no human action is inserted into every
workflow. The performer cannot grant itself accepting authority. Any deliberate
combined role must be an explicit, visible customer policy, not an inference
from a shared vendor, key, job or model name.

The initial reference fixture keeps performer, resource executor, verifier and
Work acceptor credentials distinct. Its successful automatic acceptance therefore
demonstrates a separately authorized evaluator, not performer self-acceptance.

For the reference profile, missing target evidence or a failed independent
verification blocks an accepted-outcome claim. If publication already happened,
that effect remains recorded while recovery or compensation is decided. Do not
represent a failed acceptance as proof that no effect occurred.

After the target commits but the response is lost, the gateway initially reports
unknown. A successor independently reads current Work facts, obtains its own
authority and reconciles the target journal. Sprintctl records the resulting
evidence through existing owner paths. A matching historical receipt may be
attached by the successor without falsely reattributing the original execution.
It does not make an old lease current or settle work against a changed Release.

If the Work settlement response itself is lost, reconcile using the existing
owner report read and exact durable request. Preserve the payload-digest and
idempotency rules in the exact-outcome-basis protocol. Target publication and
Work acceptance have separate commit points; neither supplies a distributed
transaction across owners. Late predecessor reports are retained but cannot
settle the successor's current basis.

Work currentness is an observation at its recorded snapshot. Resource admission
must separately bound check age, revocation propagation and already-admitted
effects. An unavailable or incomplete currentness result cannot authorize new
effects. Observation of historical receipts and safe recovery reads remain
distinct from permission to perform a new write.

## Source slices and acceptance

| Slice | Owner work | Required evidence |
|---|---|---|
| SO-01: Inventory and bind | Map the frozen publication case onto existing owner records and catalog capabilities; identify only missing joins and export guarantees. | Exact schema/API tuple, sample reconstruction with explicit unknowns, no shadow ledger or unregistered work IDs. |
| SO-02: Qualify exact settlement | Exercise current-basis, identity/role, verification and report-replay behavior against the named profile; implement only missing owner semantics. | Stale/foreign/changed-basis reports cannot settle; separately authorized automatic acceptance succeeds; rejected reports persist. |
| SO-03: Reconstruct and continue | Compose bounded Work reads with separately authorized gateway/target evidence and artifact access. | Independent reader reconstructs the full chain without predecessor chat, detects missing or conflicting links and preserves original actor attribution. |
| SO-04: Transfer and recovery | Qualify the selected existing export/import path and owner restore interaction for this bounded record. | Exact relationships, decisions and evidence classes survive supported transfer. Incomplete history, denied bytes and restored older state remain visible; import cannot mint current effect authority. |

Reconcile these slices with existing native work and
[qualification handoff](resume-explain-qualification-handoff.md) before registering
new items. The owner naming and governing-plan revision must be explicit. This
draft does not create a parallel queue or change existing work status.

### Falsification cases

1. **Normal case:** exact target publication and independent verification support
   one customer-authorized Work acceptance under the named profile.
2. **Wrong binding:** another repository, work, run, lease, artifact, operation,
   target or verifier revision cannot produce a matching accepted reconstruction.
3. **Stale basis:** changed Work requirements, references, Release or Decision
   population trigger the applicable existing refusal, including incomplete
   populations. A currentness read does not waive later settlement checks.
4. **Unknown target:** unavailable or insufficient target evidence remains unknown;
   an agent's success report cannot stand in for it.
5. **Lost replies at two boundaries:** target commit/lost response reconciles
   without reapplication; Work acceptance/lost response reconciles the exact
   owner report without a duplicate Decision.
6. **Takeover:** late predecessor evidence is retained with its original actor;
   only a separately authorized successor can settle its current Work basis.
7. **Failed verifier or denied artifact:** no accepted claim under this profile;
   any known target effect remains visible and does not disappear from history.
8. **Compensation and replay:** the original result, conditional reversal and
   current target state remain distinct. Target reversal advances its revision
   while restoring prior content; old preconditions stay stale. Old receipt replay cannot cause a new
   publication or rewrite accepted history into a claim of nonexecution.
9. **Restore/export gaps:** missing history, old authority and incomplete pages
   cannot establish currentness or complete reconstruction. Imported evidence
   remains evidence with declared provenance; it does not grant permissions.

Tests for any implementation change must exercise real owner transactions and
the declared backend scope, not just response-shaped mocks. Source tests do not
qualify a released composition, installed native session, independent operator or
production service. The
[AgentOps qualification profile](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-11-06-outcome-settlement-qualification.md)
owns the installed cross-repo proof and all-attempt measurement.

## Evidence delivery and first action

Retain the exact source/released versions, owner catalog/schema tuple, immutable
input and verifier profile, attempted-case manifest, target receipts, owner
reports/Decisions, independent reader result and validation output. Identify
unknown joins and unsupported transfer guarantees. Private artifacts and
credentials stay in their authorized store; public or hosted projections receive
only the allowed output fields.

First perform SO-01 with the frozen disposable publication case. Pin the actual
governing Git revisions on the reconciled native work. The implementation review
then decides whether an owner change is needed or existing contracts suffice.
No new model routing, execution engine, comparator, audit store, database
migration or deployment is implied by landing this plan.
