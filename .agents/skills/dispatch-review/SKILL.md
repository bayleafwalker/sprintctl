---
name: dispatch-review
description: Use when a stable code-bearing scope needs findings-first review before handoff or PR prep. Runs read-only review against the diff and repo overlay.
---

## Goal

Produce a severity-ordered, findings-first review of a stable diff using the repo's selected review checklist and specialists.

## Inputs

- The diff or file list under review.
- The dispatch packet, manifest, and repo overlay.
- Relevant requirements, architecture notes, and verification results.
- Repo-selected specialist prompts, when configured.

## Steps

1. Confirm the scope is stable enough to review.
2. Read the repo overlay for review specialists, done criteria, and severity definitions.
3. Assemble the smallest useful review context: diff, acceptance criteria, relevant docs, and verification results.
4. Run review read-only. Specialist fan-out is allowed only when the overlay selects specialists for this repo.
5. Consolidate findings by severity: blockers first, then advisories, then watchlist.
6. Include exact file references and the evidence behind each finding.
7. If blockers exist, route back to `dispatch-build` for remediation.

## Output Contract

- Findings-first markdown with severity ordering.
- Open questions and missing coverage noted explicitly.
- No repo edits.
- Clear statement whether the scope is clear for handoff or blocked.

## Do Not

- Do not review work that is still in active churn.
- Do not suppress findings to create a clean summary.
- Do not make edits from review mode.
- Do not run specialists that the repo overlay has not selected.
