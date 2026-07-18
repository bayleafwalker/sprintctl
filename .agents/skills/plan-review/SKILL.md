---
name: plan-review
description: Use to review a plan before implementation or backlog registration. Check decision completeness and reconcile every proposed work item against live sprintctl state.
---

## Goal

Establish whether a plan is ready to direct implementation by making decisions, verification, scope boundaries, dependencies, and its relationship to live backlog work explicit.

## Inputs

- The plan document and any governing decision records it depends on.
- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB` when the repository uses sprintctl.
- Current implementation, tests, architecture contracts, and live sprint/backlog state relevant to the plan.

## Steps

1. Read the plan as a decision artifact, not a prose summary. Identify its goal, in-scope work, out-of-scope work, assumptions, dependencies, rollout or rollback posture, and operator approvals.
2. List every unresolved question. Each must be either resolved in the plan, deferred with an owner and trigger, or explicitly marked as a blocker. Do not accept an implied future decision as implementation-ready scope.
3. Check the verification story for each implementation slice: affected contract, focused check, broader gate when needed, and observable success condition. Flag a plan that says only "test it" without a credible command or behavior signal.
4. Confirm scope boundaries hold across code, data, API, docs, deployment, and operations. Identify adjacent systems that the plan intentionally does not change and the guardrails preventing accidental expansion.
5. Reconcile the plan's work items with live state before registration:
   ```bash
   sprintctl sprint list --include-backlog --json
   sprintctl item list --json
   ```
   For each plan item, record the matching live item, an intentional new-item rationale, or a park/reject decision. Inspect refs and dependencies where matching remains unclear.
6. Detect both directions of drift:
   - A plan item with no live item or explicit decision is an orphan.
   - A live active/backlog item that the plan supersedes, changes, or omits needs a deliberate retain, update, carryover, or closeout decision.
7. Report findings first, grouped as blockers, required revisions, and advisories. Include exact evidence and the smallest next action for each blocker.

## Output contract

- A decision-completeness review with each open question resolved, deferred with owner/trigger, or blocked.
- A verification story and scope boundary for every planned implementation slice.
- A one-to-one mapping between accepted plan items and live sprintctl work, with no unexplained orphan or duplicate.
- A findings-first result that states whether the plan is ready for backlog refinement or implementation.

## Do not

- Do not turn unresolved product or architecture questions into vague implementation bullets.
- Do not rely on a committed sprint snapshot when live sprintctl state is available.
- Do not register duplicate work because a plan uses different wording from an existing item.
- Do not treat a missing verification path as a minor editorial issue when it blocks safe execution.
- Do not mutate plan status, sprint state, or governing document frontmatter unless that authority is explicitly granted.