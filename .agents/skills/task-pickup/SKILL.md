---
name: task-pickup
description: Use when choosing the next sprintctl task to execute. Consult live state first and claim work before editing when overlap is possible.
---

## Goal

Choose one executable item from live sprintctl state without duplicating work, bypassing an existing claim, or treating stale docs as the execution queue.

## Inputs

- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB`.
- The current actor, `runtime_session_id`, and stable `instance_id`.
- The repository's active-sprint and backlog policy.

## Steps

1. Inspect live state before choosing anything:
   ```bash
   sprintctl sprint list --active --json
   sprintctl sprint list --include-backlog --json
   ```
2. If no active sprint exists, select an eligible backlog sprint or create/promote one only under the repository's sprint policy. Do not invent a replacement sprint from an old snapshot when a live backlog already exists.
3. For the selected sprint, inspect claims first:
   ```bash
   sprintctl claim list-sprint --sprint-id <sprint-id> --json
   ```
   If an open claim belongs to the current live identity, delegate recovery to `sprint-resume` rather than selecting new work.
4. Otherwise, ask sprintctl for an explainable candidate set:
   ```bash
   sprintctl next-work --sprint-id <sprint-id> --json --explain
   ```
5. `next-work` orders ready candidates by the native priority field (`item add --priority N`, `item priority --id N --set N`; 1 = highest, unprioritized last), falling back to the legacy `[pN] ` title prefix when no native priority is set. Trust its order; refine only when two candidates tie.
6. Read the chosen item's details, refs, and dependencies before claiming it. Resolve a blocking dependency or choose another ready item instead of claiming around it.
7. Start the claim with the current identity and retain the returned `claim_id` and `claim_token` in the local recovery location:
   ```bash
   sprintctl claim start --item-id <item-id> --actor <actor> \
     --runtime-session-id <runtime-session-id> --instance-id <instance-id> --json
   ```
8. Continue through `sprint-resume` for the implementation lifecycle.

## Output contract

- One selected item is traceable to live `next-work` output or an explicit repository-approved promotion decision.
- The item's priority is visible in the `PRI` column of `item list` and `next-work` (native `priority` field, or legacy `[pN] ` title prefix as fallback).
- Active work is protected by a claim whose `claim_id` and `claim_token` belong to the current identity.
- Any inability to choose safely is reported as a blocker with the relevant claim, dependency, or sprint state.

## Do not

- Do not choose work from a committed snapshot or plan before inspecting live sprintctl state.
- Do not infer ownership from an actor label, branch, workspace, or hostname; `claim_id` plus `claim_token` is the proof.
- Do not use append-only note tags as a priority queue; `next-work` does not order by them.
- Do not claim an item already held by another live identity without a valid handoff.
- Do not edit implementation files before the item is safely claimed when parallel overlap is possible.