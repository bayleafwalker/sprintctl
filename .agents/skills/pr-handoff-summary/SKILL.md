---
name: pr-handoff-summary
description: Use when a handoff or PR summary is actually called for -- an escalation, a dispatched review, or a CI check that only runs on a PR. Most completed dispatch work lands instead; see `item-done` step 4.
---

## Goal

Render a compact reviewer-ready summary of what changed, what was verified, and what still needs attention.

**First decide whether a summary is owed at all.** This skill is for work that has a
named reader: an escalation to human judgement, a dispatched review session, or a PR that
exists because the repo's CI runs nowhere else. Completed work with no such reader should
be landed, not summarised -- writing a reviewer-facing summary for a reader who was never
named is how a PR becomes a parking spot instead of a request. If you cannot say who reads
this, land the work and stop.

## Inputs

- Final diff or stable changed path list.
- Verification results.
- Review findings and remediation status.
- Dispatch packet refs: action id, work item, sprint, branch, commit, or PR.

## Steps

1. Summarize the implemented outcome, not a file-by-file changelog.
2. Call out the highest-signal files, contracts, or docs for review.
3. Include exact verification commands already run.
4. Include unresolved risks, follow-ups, or skipped checks.
5. Preserve structured refs so actionq, auditctl, and PR tooling can cross-link the work.

## Output Contract

- Short summary.
- Verification section.
- Review or residual-risk section.
- Structured refs when available.

## Do Not

- Do not omit failed or skipped verification.
- Do not substitute this summary for a findings-first review when review is required.
- Do not bury blockers in a general summary.
- Do not write a summary for an unnamed reader. If no escalation, dispatched review, or PR-only CI check applies, `item-done` step 4 governs: land it.
