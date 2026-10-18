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
| E1: Explain effect acceptance facts | 2, dispatch-build | Present the existing acceptance snapshot in one bounded read, name missing chain links and keep evidence classes distinct. Preserve raw JSON and caller-scoped receipt visibility. | Existing acceptance snapshot |
| E2: Independent result-reader proof | 2, dispatch-build after E1 decisions | An independent reader detects changed digest and omitted receipt; qualify a supported result packet only after explicit closure, versioning, scope and export/import guarantees are settled. | E1 |
| H1: Continuation diagnostic | 3, dispatch-build | One bounded continuation read yields stable reason-coded findings and explicit report-class observation provenance; no artifact fetch or measured-effort score. | R1; bounded observation protocol |
| H2: Interrupted-work recovery qualification | 3, dispatch-build | Three paired native-owner fixtures qualify frozen diagnostic questions and explicit lookup/check counts. The field trial records real fresh-reader effort separately, retaining failures and unknowns. | H1 |
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

R2 and the bounded E2 software reader proof are specified in
[reader qualification](../evidence/resume-result-reader-qualification.md).
Six native-owner cases cross a fresh-process boundary: three continuation cases
and three effect-evidence cases. This qualifies serialization and interpretation
against the recorded assertions. A fresh LLM/human recovery trial, measured
recovery effort and provider/host swap remain separate field qualification;
passing software cases does not manufacture those measurements.

## E1 decisions and acceptance

Inventory existing owner operations and facts before adding a surface. In
particular, `work.effect.acceptance-snapshot-v1` provides exact requested basis,
artifact comparison, bounded decision/reference facts and verification receipt
comparison, while lease settlement and obligation mappings may be unknown. It
does not by itself prove the complete attempt-to-observation chain.

`effect explain` requires an exact intent ID, positive revision, canonical intent
digest and artifact SHA256. An optional reference names the verification run and
evidence item. It performs one existing owner snapshot read in served mode;
text is a bounded presentation and JSON retains the complete owner response.
There is no receipt discovery, implicit artifact fetch or acceptance write.

Mismatches remain prominent even when the current intent is accepted or a receipt
matches. A successful mismatch read exits zero; it is not an acceptance verdict.
Missing receipt input, missing evidence authority and caller-invisible receipt
remain distinct. A missing Release/check revision and truncated collections retain
unknowns. The snapshot identity does not seal the whole response or receipt.

The explanation preserves report, receipt, acknowledgment, observation and
acceptance as distinct classes. It names unavailable terminal Decision binding,
accepting identity/time, attempt history, obligation mapping and target outcome.
An accepted/applied intent or done item cannot fill those gaps. Focused tests
must cover changed basis/artifact, matching/missing/foreign-bound receipt,
missing Release/check revisions, overflow, refusal and complete JSON preservation.

Before E2 is build-ready, settle complete-chain joins, coverage, source revision,
closure/version and supported export/import behavior separately. Reuse the
authoritative evidence home. The existing archival fixture proof does not
establish general result export support. This bounded E1 view is useful without
claiming the full accepted-result reconstruction outcome.

## H1/H2 decisions and acceptance

`run diagnose` reuses one continuation-delta owner read. Its surrounding report
is a derived checklist, with the complete owner response retained unchanged.
Reason codes distinguish missing current continuation, missing baseline,
changed/unknown owner records, incomplete pages, unchecked source/correspondence/
availability, reported access problems and unassessed outcome. It makes no
content requests, writes no state, and never grants authority.

The optional observation file binds to the exact repository, item, snapshot
and visible reference IDs. The protocol is limited to 64 KiB/64 checks, UTC
timestamps and a one-hour eligibility window on the caller clock; stale/future
reports are retained but ignored. Matching reports stay report-class and cannot
upgrade owner unknowns. Unknown fields, duplicate/off-page targets and wrong
scope are refused. See [diagnostic guide](../guides/diagnose-resume.md).

Review order is a declared checklist: reported access problems, changed records,
missing/incomplete context, unchecked/unknown facts, then unassessed outcome.
It is not a recovery-effort score or cross-item priority. H2's three paired
disposable cases freeze questions and record native owner read/check counts.
Actual fresh-human/agent recovery time and improvement remain unmeasured until
the field protocol runs with independent readers, interventions, failed cases
and visible denominators. Missing effort never becomes zero.

## L1 decisions still required

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
