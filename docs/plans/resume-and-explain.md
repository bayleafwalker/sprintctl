---
doc_id: resume-and-explain
status: draft
supersedes: null
---

# Resume and explain: implementation roadmap

Sprintctl keeps work identifiable, resumable, and explainable as people and
agents change. This develops the ratified [roadmap reset](roadmap-reset.md) and
the owner's 2026-10-18 directive against Vuoro's accepted 2026-10-15 direction.
It does not supersede either record or claim that existing capabilities are absent.

The owner requested implementation and development-item registration. During
planning, the deployment session confirmed the shared API was deliberately fenced
for the paired restore/cutover, with no fresh authoritative backlog export.
Existing item matches are therefore **unknown**. The slices below are concrete
registration candidates, not invented live IDs or a second status database.
Reconcile them against live items and refs before registering anything; retain
matching items, record priorities natively, and attach this document's exact Git
revision. Draft editorial status is not agent ratification.

## Boundaries and sequence

Native harnesses own sessions, execution and retries. Protected executors own
effects. Sprintctl supplies authoritative work records through portable owner
contracts. Vuoro owns derived cross-provider interpretation and learning. No
slice adds an execution runtime, parallel evidence store, broad management UI,
credential inheritance, schema migration, or deployment action.

| Slice | Intended priority and posture | Concrete outcome and scope | Depends on |
| --- | --- | --- | --- |
| R1: Resume change briefing | 2, dispatch-build | One bounded CLI owner read presents changed/unchanged/unknown; JSON retains the existing response and cursors. Text handoff preserves predecessor position. Correct positioning and served onboarding. | Existing continuation-delta operation |
| R2: Fresh-reader continuation proof | 2, dispatch-build after live matching | Qualify R1 against three disposable interrupted-task cases; one deliberate release/artifact change, one decision change, one missing baseline/content case. Record correct/incorrect inferences, extra calls, recovery effort and denominator. | R1 |
| E1: Explain an accepted result | 2, dispatch-plan | Specify a derived result explanation joining request/release, attempts, exact artifact, receipts, observations and acceptance. Name missing links and assurance classes; do not promote a Decision into effect evidence. | Owner-fact coverage review; existing acceptance snapshot |
| E2: Independent result-reader proof | 2, dispatch-build after E1 decisions | An independent reader detects changed digest and omitted receipt; qualify a supported result packet only after explicit closure, versioning, scope and export/import guarantees are settled. | E1 |
| H1: Recovery-burden diagnostic | 3, dispatch-plan | Read-only item-scoped missing-context findings with reason codes and check provenance, starting from continuation point, inaccessible/unchecked artifact, stale reference and unresolved effect outcome. Unchecked differs from missing. | R2; bounded observation protocol |
| H2: Interrupted-work recovery trial | 3, dispatch-build after H1 | Compare recovery effort before/after repairing diagnosed gaps on the same bounded task population; retain failed recovery and unknown observations. | H1 |
| L1: Accepted-outcome comparison input | 3, Vuoro-owned dispatch-plan | Map accepted releases to rework, recovery time, review effort, known cost and unknown coverage. Sprintctl supplies records, not a comparison engine. | Acceptance/evidence joins qualified by E2 |

Priorities are recommendations for live refinement, not assertions about the
current execution queue. No new item may be registered solely because this table
uses different wording from an existing item.

## R1 decisions and acceptance

`run brief --run-id <own-run> --item-id <repository-local-id>` uses
`work.run.continuation-delta-v1`, with text default and full owner JSON available.
The complete previous `delta.current` is an optional comparison checkpoint;
`source_checkpoint` is the predecessor's recorded position, not that checkpoint.
The owner validates run binding and checkpoint identity. No credential carries
over from the predecessor. A user-supplied checkpoint is a comparison basis,
not an authenticated record of an earlier observation.

The text must expose changed release and decision records, preserve unknown
comparisons without a baseline or digest, and distinguish unchanged reference
records from unknown source bytes. It shows snapshot identity, page completeness,
next checks, and that the read grants no authority. Cursor input permits explicit
bounded paging; there is no hidden unbounded read or local-state fallback. JSON
retains all owner fields rather than creating a second portable work contract.

Handoff text must show predecessor SHA, branch, worktree, recorded time and dirty
count, separately from caller Git observations. No observed worktree means
unknown cleanliness. The previous handoff timestamp and touched IDs/event count
come directly from the existing bundle; no baseline means unknown changes.

Focused verification:

```sh
uv run --frozen --extra dev --extra served --extra remote pytest tests/test_run_brief.py tests/test_agent_inbox_cli.py tests/test_handoff_checkpoint.py tests/test_handoff_delta_timestamps.py tests/test_served_routes.py tests/test_docs_integrity.py -q -x --tb=short
uv run --frozen --extra dev --extra served --extra remote pytest -q -m "not perf"
```

The pure presentation proof is example-tested, not a fresh-reader user trial or
deployed acceptance. Existing disposable owner-snapshot tests establish separate
owner behavior; a current deployed success requires the deployment owner's gates.

## E1 decisions still required

Inventory existing owner operations and facts before adding a surface. In
particular, `work.effect.acceptance-snapshot-v1` provides exact requested basis,
artifact comparison, bounded decision/reference facts and verification receipt
comparison, while lease settlement and obligation mappings may be unknown. It
does not by itself prove the complete attempt-to-observation chain.

The E1 owner must settle the minimum chain, identity/digest joins, supported
backends, missing/unavailable semantics, completeness and source revision before
E2 is build-ready. Preserve report, receipt, acknowledgment, observation and
acceptance as distinct classes. Reuse the authoritative evidence home. The
existing archival fixture proof does not establish general result export support.

## H1 and L1 decisions still required

H1 first needs a bounded protocol for checking artifacts and references, including
observation age and provenance. Avoid scoring work as healthy merely because it
is young, or as broken because a check could not run. Recovery-burden ranking
needs measured effort and a declared uncertainty policy before implementation.

L1 belongs to Vuoro's learning layer. Before comparing workflows, specify task
grouping, accepted-outcome denominator, minimum sample and capture coverage,
known/unknown cost and review/recovery measurements. Insufficient evidence must
produce an inconclusive result. No comparison automatically expands authority.

## Consolidation and release posture

Fix confirmed handoff snapshot consistency and retry classification defects as
separate bounded items with disposable PostgreSQL verification. Do not attach
broad module-size cleanup, event-ordering changes, or ref batching to R1.

Develop in isolated source scope while service restoration is fenced. The
deployment session owns every service operation and keeps the official 0.20.0
pin. Source merge does not publish a release or qualify the running service.
After restoration, reconcile/register slices, attach verification provenance,
and record acceptance only for scope actually completed. Never mark the entire
roadmap done because R1 lands.
