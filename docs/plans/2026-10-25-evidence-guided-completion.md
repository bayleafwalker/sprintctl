---
doc_id: sprintctl-evidence-guided-completion-2026-10-25
status: operator-requested-implementation-plan
supersedes: null
---

# Completion from accumulated evidence, without a compulsory model gate

**Owner:** Sprintctl work, Release, Decision and evidence contracts. This is
the AE-03 owner packet from the
[Agentops plan](https://github.com/bayleafwalker/agentops/blob/main/docs/plans/agentops/2026-10-25-evidence-guided-agent-workflows.md);
it refines the current [native evidence](s4-native-evidence-intake-2026-10-04.md)
and [portable continuation](2026-10-24-portable-agent-workflows.md) plans.
A source plan is not a released/deployed operation or permission change.

## Existing semantics that must win

Sprintctl owns the authoritative work revision, requirement/acceptance profile,
Release digest and terminal Decision. An execution session, completed PR, passed
pipeline, Auditctl observation, model verdict or protected effect intent is
not itself a terminal work Decision. The S4 evidence-home migration remains
separately gated; current local or hosted evidence must be read through
qualified owner operations, never a new direct database writer.

**Naming boundary:** this plan concerns *work-completion assessment* at the
Sprintctl/Release owner. `packages/vuoro-reconciler` is an existing
trusted-side effect-intent executor. It must not acquire this completion
logic, settle Sprintctl work or become a generic model dispatcher.

**No false review gates.** A complete, currently valid acceptance contract
can be satisfied and accepted automatically through the existing protected
owner operation. A human operator need not be online for a task to finish
where the actual policy permits it. Human review remains required only where
the current named contract calls for accountable judgment or an authorized
policy/authority change. Independent retrospective review may follow an
automatic acceptance without retracting its validity.

## SC-EC1: source-qualified completion assessment (read-only first)

Inspect the live owner schema and existing evidence read/snapshot, Release,
Decision and terminal operation before adding any interface. Produce one
versioned, read-only assessment from:

- exact work identity/revision, requirements and accepted verification profile;
- current Release candidate/artifact digest and prior owner Decision state;
- independently verified checks, method/oracle revision and tested Git SHA;
- eligible run/session evidence and source freshness, including unknown coverage;
- any required independent reviewer/effect-reconciliation condition that the
  *current* acceptance policy actually declares.

Every assertion states owner fact versus model-derived inference. Keep missing,
failed, expired, inaccessible, contradictory and not-applicable separate.
A provider's `succeeded` is never enough. Do not reinterpret previously
recorded acceptance, scorecard or gate results merely because a cheaper judge
is available. New scoring semantics need a separately revisioned owner policy.

**Candidate read-only result** (shape is illustrative, not a public ABI):

```yaml
work_completion_assessment:
  basis: { work_ref: "wi:1842", work_revision: 12, release_digest: "sha256:..." }
  status: sufficient | missing-evidence | conflict | requires-judgment
  requirements:
    - { requirement_id: test-suite, state: satisfied, evidence_ref: "sha:..." }
  current_owner_decision_ref: null
  missing: []
  suggested_followups: []          # never grants or settled work
  source_coverage: { observed: 3, expected: 4, unknown: 1 }
```

If source owners do not expose an adequate read, extend *their* versioned
contract through the EC-00 TypeSpec -> OpenAPI programme. Do not add a
second task database, a synthetic reconciliation writer or a catch-all
`execute(type,payload)` endpoint.

**Acceptance:** replay a frozen case with all required evidence; a missing
evidence case; a stale work revision; a contradictory test result; a hosted
attempt without hooks; an old Release digest; and a genuine owner-required
review. Every output names its exact basis and unknowns.

## SC-EC2: apply an authorized completion, without changing its meaning

When SC-EC1 finds a current valid contract satisfied, call the **existing**
current-revision owner acceptance operation under the actual grant/policy,
with a stable idempotency key. The owner decides accept/refuse; the assessment
does not force acceptance, and a successful model suggestion carries no
authority. Test a competing work revision between assessment and apply:
the owner must refuse the stale attempt, retain its evidence and reread state.
Do not add universal `review_required` behavior to successful work or wait
for an optional scribe/Auditctl network upload.

**Atomic basis precondition (blocking for automatic apply):** the current
served `work.decision.record` contract accepts the item, decision fields, an
optional `release_digest`, and the invocation idempotency key; it does not
expose an expected item revision or expected-current-Release comparison, and
the application handler does not pass an expected revision to the writer
([catalog contract](https://github.com/bayleafwalker/sprintctl/blob/eeedd3cbe29bc758c76108517f3513fd91c21899/sprintctl/vuoro_adapter.py#L2383),
[handler](https://github.com/bayleafwalker/sprintctl/blob/eeedd3cbe29bc758c76108517f3513fd91c21899/sprintctl/work_application.py#L4048)).
For the existing resolver, omitting `release_digest` selects whatever is
current when the Decision is written; supplying one checks that it belongs to
the item, not that it remains current
([resolver](https://github.com/bayleafwalker/sprintctl/blob/eeedd3cbe29bc758c76108517f3513fd91c21899/sprintctl/releasecore.py#L93)).
A read assessment followed by this write therefore cannot promise refusal
when the assessed basis changes between those operations.

Before automatic apply, the owner write must compare the exact assessment basis
with current state in the same transaction as the Decision and terminal status
change. The comparison must cover the current acceptance-relevant work
revision and exact current Release digest; it must also cover a mutable
acceptance-profile revision if the Release digest does not already bind it.
The Decision must bind to that exact Release. Use an already released owner
path only if its wire contract and implementation prove this compare-and-set
behavior end to end. Otherwise, add a versioned CAS input to the existing
Decision-record contract through EC-00 TypeSpec/OpenAPI governance and route it
through the same Decision writer. This is a blocking contract prerequisite,
not authorization for a second terminal-decision writer. Until the CAS-bearing
operation is released and qualified, leave automatic apply disabled; SC-EC1
read-only assessment and nonmutating SC-EC3 recommendations may proceed.

A basis mismatch is a no-write conflict: preserve the evidence and assessment,
then reread and reassess using a new basis and idempotency key. If the write
response is lost, retry the exact original request with its original key and
accept only the owner replay result. Never change the basis under an existing
key.


Record the owner-issued terminal Decision and exact Release digest in the
owner record. Do not mistake `done`, a merged PR, an effect acceptance,
or a provider completion as a verified Release unless the existing
Decision/Release join establishes it.

**Acceptance:** one successful current-basis automatic completion; a revision/edit or current-Release change between assessment and apply is atomically refused with no Decision or terminal-status write; a policy refusal; exact replay after a lost response returns the one owner Decision; changed-basis reuse of the old key conflicts. Turning the optional semantic consumer off changes none of the deterministic owner results.

## SC-EC3: bounded follow-up recommendations

A scribe or decision model may recommend `no-change`, `reverify`,
`inspect-conflict`, `request-independent-review` or a newly described
follow-up. These are suggestions based on immutable source references.
An existing, separately authorized native work command must register any new
work or change an existing work item. Deduplicate recommendations using the
target work ref, source digest, current work revision and action kind.
Rejection/supersession remains inspectable; no repeated periodic scribe should
continually manufacture the same ticket.

A completed requirement and an unrelated later finding are **two separate
facts**: the old work may remain accepted while an optional follow-up is
created. If a truly material contradiction invalidates a Release, use the
existing explicit revision/withdrawal/supersession operation and its
authority. No implicit retroactive erasure.

**Acceptance:** a complete case with optional finding closes; exactly one
follow-up is proposed despite replay; a prior rejected follow-up is not
silently reopened; a model unavailable case keeps the deterministic path
working.

## SC-EC4: adversarial qualification and adoption

Verify at least these independent histories under a disposable native owner:
late event, duplicated event, hook missing, central Auditctl unavailable,
crash after source commit, stale assessment revision, conflicting test report,
contradictory model inference, lost response after owner acceptance, unknown
effect target state and policy-required human attestation.

Use the Agentops AE-04 **30–50 labeled playbook cases** for optional bounded
decision comparison, but do not require the classifier to run for SC-EC2.
Cost per accepted case includes failed attempts and unknown costs. Zero unsafe
closures in a small fixture is a **qualification floor**, not a statistical
claim of universal correctness.

**Rollback:** disable inferred follow-up generation and assessment integration;
preserve append-only evidence and already issued Decisions; reread owner
current state before idempotent retries; fall back to existing owner
verification/acceptance operations. S4 migration/retention changes have their
own owner rollback. Record source merged, versioned release, deployed profile
and real accepted-outcome evidence independently.

## Ready handoff

Before code: bind SC-EC1..4 to existing served Sprintctl items in this repo.
Check current releases and EC-05 work/read contract migration for overlapping
operations. Planning IDs are not Sprintctl ids. No new service or approval
account is approved here.
