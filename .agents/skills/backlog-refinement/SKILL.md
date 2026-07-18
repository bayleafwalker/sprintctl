---
name: backlog-refinement
description: Use when reconciling plans into sprintctl backlog work. Read live state before creating or changing any sprint or item.
---

## Goal

Turn ratified scope into a non-duplicated, prioritized sprintctl backlog whose items have an execution posture, governing references, dependencies, and testable acceptance criteria.

## Inputs

- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB`.
- The governing plan, decision record, or explicitly accepted scope.
- The intended sprint or backlog destination, if one is already known.

## Steps

1. Start read-only. Reconcile the governing source against live state before proposing additions:
   ```bash
   sprintctl sprint list --include-backlog --json
   sprintctl item list --json
   ```
   Inspect matching sprint names, existing titles, refs, dependencies, status, and recent notes. Live state wins over a stale committed snapshot.
2. Classify each candidate as one of: already represented, must-do-now, should-do-soon, later, or reject. Also record its stability stratum or repository-specific change boundary when that distinction matters.
3. For every candidate, choose a dispatch posture: `dispatch-plan` when a boundary decision remains, `dispatch-build` only when scope and acceptance are ready, or park/reject when it should not enter the execution queue.
4. Set priority with the native field: `item add --priority N` on registration or `item priority --id N --set N` when refining (1 = must-do-now, 2 = should-do-soon, 3 = later; 1 = highest). The legacy `[p1] `/`[p2] `/`[p3] ` title prefix is still recognized as a fallback on items without a native priority, but new refinement should use the field.
5. Add only candidates absent from live state. For a new item, include an outcome-oriented title and a useful description; then enrich it with the relevant commands:
   ```bash
   sprintctl item note --id <item-id> --type decision --summary "<scope and done condition>" --actor <actor>
   sprintctl item ref add --id <item-id> --type doc --url <path-or-url> --label <governing-doc>
   sprintctl item dep add --id <item-id> --blocks-item-id <blocked-item-id>
   sprintctl item edit --id <item-id> <approved-field-updates>
   ```
6. When durable knowledge events should seed a known backlog, use sprintctl's idempotent native path after reviewing the source and target:
   ```bash
   sprintctl sprint backlog-seed --from-sprint-id <source-id> --to-sprint-id <backlog-id> --actor <actor> --json
   ```
   Review the seeded items and refine their priority, refs, acceptance, and dependencies; do not treat raw event summaries as complete scope.
7. Render or otherwise record the changed live state at the repository's normal shared-artifact boundary.

## Output contract

- Every accepted candidate is either linked to one live sprintctl item or explicitly recorded as parked/rejected.
- No duplicate sprint or item was created for work already represented in live state.
- Runnable items have a native priority (visible in the `PRI` column) where applicable, a governing reference or explicit decision note, dependencies, and a dispatch posture.
- The backlog can be read by `item list` and selected by `next-work` without relying on append-only history tags.

## Do not

- Do not register a sprint or item before checking `sprint list --include-backlog --json` and existing work items.
- Do not use `item note --tags` as a mutable priority system; notes are history and `next-work` ignores them.
- Do not create vague activity bullets when the governing source can name an outcome and acceptance condition.
- Do not promote parked or rejected ideas into an active sprint without an explicit new decision.
- Do not mutate live state in a planning-only or read-only session.