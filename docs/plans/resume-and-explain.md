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

The owner approved implementation and native development-item registration.
Authoritative served state was reconciled after service restoration on
2026-10-18. The source scopes and remaining qualifications now map to native
items below. Live state wins over this roadmap; implementation does not ratify
this draft. Existing historical handoff scope, `sprintctl#2082` guidance,
`sprintctl#2150` backend unification, `sprintctl#2127` external onboarding release, and `vuoro#2237`
broader experiment memory remain distinct and were not closed by this work.

## Boundaries and sequence

Native harnesses own sessions, execution and retries. Protected executors own
effects. Sprintctl supplies authoritative work records through portable owner
contracts. Vuoro owns derived cross-provider interpretation and learning. No
slice adds an execution runtime, parallel evidence store, broad management UI,
credential inheritance, schema migration, or deployment action.

The proposed [portable continuation requirements](2026-10-24-portable-continuation-requirements.md)
describe a larger cross-host batch. Any page aggregation remains conditional
on owner-approved scope and preserves the one-read behavior above until that
approval exists.

| Slice | Intended priority and posture | Concrete outcome and scope | Depends on |
| --- | --- | --- | --- |
| R1: Resume change briefing | 2, dispatch-build | One bounded CLI owner read presents changed/unchanged/unknown; JSON retains the existing response and cursors. Text handoff preserves predecessor position. Correct positioning and served onboarding. | Existing continuation-delta operation |
| R2: Fresh-reader continuation proof | 2, software proof implemented; field qualification open | Qualify R1 against three disposable interrupted-task cases; one deliberate release/artifact change, one decision change, one missing baseline/content case. Record correct/incorrect inferences, extra calls, recovery effort and denominator. | R1 |
| E1: Explain effect acceptance facts | 2, dispatch-build | Present the existing acceptance snapshot in one bounded read, name missing chain links and keep evidence classes distinct. Preserve raw JSON and caller-scoped receipt visibility. | Existing acceptance snapshot |
| E2: Independent result-reader proof | 2, software proof implemented; general packet dispatch-plan | An independent reader detects changed digest and omitted receipt; qualify a supported result packet only after explicit closure, versioning, scope and export/import guarantees are settled. | E1 |
| H1: Continuation diagnostic | 3, dispatch-build | One bounded continuation read yields stable reason-coded findings and explicit report-class observation provenance; no artifact fetch or measured-effort score. | R1; bounded observation protocol |
| H2: Interrupted-work recovery qualification | 3, dispatch-build | Three paired native-owner fixtures qualify frozen diagnostic questions and explicit lookup/check counts. The field trial records real fresh-reader effort separately, retaining failures and unknowns. | H1 |
| L1: Accepted-outcome comparison input | 3, Vuoro-owned supplied-record intake | Map accepted releases to rework, recovery time, review effort, known cost and unknown coverage. Sprintctl supplies records, not a comparison engine. | Acceptance/evidence joins qualified by E2 |

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

Before the general result-packet/export portion of E2 is build-ready, settle
complete-chain joins, coverage, source revision,
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

## L1 supplied-record intake decision

L1 belongs to Vuoro's learning layer. Its existing `ExperimentRecord` already
retains explicit case populations, accepted/capture denominators, unknown effort
and cost, separate currencies and reviewer-supplied review thresholds. The
[learning architecture](https://github.com/bayleafwalker/vuoro/blob/main/docs/architecture/accepted-outcome-learning.md)
defines the supplied-record intake into that existing model; Sprintctl does not
add a comparison engine or authoritative evidence store.

The pure intake checks matching repository/item identities, terminal Decision
and Release digests in supplied projections. Consistent accepted joins remain
externally supplied acceptance, with source verification explicitly not performed
by the package. Missing or conflicting joins become unknown observations while
retaining case/run identity, independent capture and qualified population-burden
measurements. Partial coverage or missing provenance/windows never becomes zero
or a known scalar. Measurement sidecars remain separate from the derived summary.
Representative samples, source authentication and causal workflow conclusions
remain unqualified; insufficient evidence produces an inconclusive summary.
No comparison expands authority.

## Implementation and qualification evidence

This table describes source evidence, not native tracker status. Original product
outcomes remain open wherever the qualification column calls for evidence not
yet obtained. Native scope was matched before registration; the references below
identify actual repository-scoped items, with priorities and dependencies stored
in the live ledger.

| Slice | Source outcome | Remaining qualification |
| --- | --- | --- |
| R1 | Briefing, handoff evidence and consistent owner snapshot implemented | Deployed acceptance is owned by the deployment session |
| R2 | Three native software cases plus six actual fresh-agent paired readings passed | Cross-host/model qualification, audited calls and measured recovery effort |
| E1 | Bounded effect explanation implemented | Complete request, actor, attempt, target and terminal-acceptance reconstruction |
| E2 | Native software cases, three actual fresh-agent result readings and bounded offline read packets passed | Full historical closure, source authenticity and supported owner export/import |
| H1 | Diagnostic checklist and bounded caller-observation protocol implemented | External observation quality remains caller responsibility; no measured burden ranking |
| H2 | Three paired native fixtures passed with explicit read/check counts | Independent-reader recovery effort and demonstrated improvement |
| L1 | Existing Vuoro learning model reused with supplied-record intake | Source authentication and representative accepted-outcome samples |

See [reader qualification](../evidence/resume-result-reader-qualification.md),
[diagnostic qualification and field protocol](../guides/diagnose-resume.md),
and the versioned `verification/contexts` and `verification/results` packets.
Software fixtures supply example evidence; they cannot stand in for field trials,
general archival portability or an authenticated chain of external effects.

### Native work references and landed source

| Native item | Bounded scope |
| --- | --- |
| `sprintctl#2655` | R1 bounded briefing and handoff presentation |
| `sprintctl#2656` | Consistent owner handoff snapshot repair |
| `sprintctl#2657` | R2/E2 six-case fresh-process software proof |
| `sprintctl#2658` | E1 bounded effect explanation |
| `sprintctl#2659` | H1 diagnostic and H2 paired software fixtures |
| `sprintctl#2660` | Ten audited repository reads classified for bounded reconnect |
| `sprintctl#2661` | R2 independent-reader field qualification |
| `sprintctl#2662` | E2 supported general packet guarantees and design |
| `sprintctl#2663` | H2 measured recovery-effort field comparison |
| `sprintctl#2664` | Executable scoped reservation commands and runtime-session detection |
| `vuoro#2665` | L1 supplied-record intake into the existing learning model |
| `sprintctl#2714` | Bounded offline owner-read packet creation and inspection |
| `sprintctl#2715` | Frozen nine-reader trial:108/108 correct, effort unknown |

See the [retained reader results](../evidence/independent-reader-trial-results.md)
and [remaining qualification handoff](resume-explain-qualification-handoff.md).
The trial does not close the broader parent qualifications.

Landed source:
[R1](https://github.com/bayleafwalker/sprintctl/commit/48f95090dd85587bbce062e4b537b661bd601e97),
[handoff snapshot](https://github.com/bayleafwalker/sprintctl/commit/894035c92058cab274b71ad7fddd440caac86f95),
[reader proof](https://github.com/bayleafwalker/sprintctl/commit/2e001841e9786e7e77f7dad2d23a67ccebeeec0d),
[E1](https://github.com/bayleafwalker/sprintctl/commit/d729b27b91abb5bf3c537640f4a5f110f5dced8c),
[diagnostic](https://github.com/bayleafwalker/sprintctl/commit/94fe7b932e18b22376300473b88b26d559dd6420),
[read recovery](https://github.com/bayleafwalker/sprintctl/commit/6f8b5870381236ac3da78a6cce8cdd871147ed0d),
[Vuoro intake](https://github.com/bayleafwalker/vuoro/commit/aa5af4de351f011c1d4f795eaa72b692c5dfcb47),
and [scoped reservation commands](https://github.com/bayleafwalker/sprintctl/commit/c7dc64d5aafd941cac97f0e37077e1151bccb395).
Native items retain the corresponding main CI and committed evidence references.

Source closeout Decisions retain evidence digests and Git/CI references. The
observation-reservation closeout records have no bound Release; that missing
link stays explicit. Their bounded source acceptance is not a qualified general
result packet or an authenticated L1 accepted-outcome sample. Current source
registration and reservations were not backdated into the service fence.

The served catalog currently refuses `render`; the required generated sprint
snapshot could not be refreshed. Existing snapshots were preserved, without a
local-state fallback or manual replacement. Native reads and writes succeeded.

## Consolidation and release posture

Fix confirmed handoff snapshot consistency and retry classification defects as
separate bounded items with disposable PostgreSQL verification. The subsequent
reservation CLI repair reuses existing scope/session/configuration resolvers; its
protocol boundary and pre-I/O refusals are example-tested without changing owner
semantics. Do not attach
broad module-size cleanup, event-ordering changes, or ref batching to R1.

Source development used isolated scopes during the service-restoration fence.
The deployment session owns every service operation and keeps the official
0.20.0 pin. Source merge does not publish a release or qualify the running service.
After restoration, native reconciliation and bounded-source acceptance were
recorded with verification provenance. Field and general-packet qualifications
remain separate open scope. No source merge marks the entire roadmap done.
