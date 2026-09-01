---
name: task-pickup
description: Use when choosing the next sprintctl task to execute. Consult live state first and reserve work before editing when overlap is possible.
---

## Goal

Choose one executable item from live sprintctl state without duplicating work, ignoring an existing reservation, or treating stale docs as the execution queue.

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
3. For the selected sprint, inspect existing reservations first:
   ```bash
   sprintctl reservation list --all --json
   ```
   If an open reservation belongs to the current live identity, delegate recovery to `sprint-resume` rather than selecting new work.
4. Otherwise, ask sprintctl for an explainable candidate set:
   ```bash
   sprintctl next-work --sprint-id <sprint-id> --json --explain
   ```
5. `next-work` orders ready candidates by the native priority field (`item add --priority N`, `item priority --id N --set N`; 1 = highest, unprioritized last), falling back to the legacy `[pN] ` title prefix when no native priority is set. Trust its order; refine only when two candidates tie.
6. Read the chosen item's details, refs, and dependencies before reserving it. Resolve a blocking dependency or choose another ready item instead of reserving around it.
7. Register the reservation with the current identity:
   ```bash
   sprintctl reservation reserve --item-id <item-id> --actor <actor> \
     --session-id <runtime-session-id> --role execution --json
   ```
   Overlapping reservations are allowed and reported, not refused. If the response reports an existing execution reservation held by another live identity, treat that as a coordination signal and choose different work rather than passing `--interrupt-existing`, which is a deliberate interruption of someone else's run.
8. Continue through `sprint-resume` for the implementation lifecycle.

## Output contract

- One selected item is traceable to live `next-work` output or an explicit repository-approved promotion decision.
- The item's priority is visible in the `PRI` column of `item list` and `next-work` (native `priority` field, or legacy `[pN] ` title prefix as fallback).
- Active work carries an execution reservation registered under the current identity.
- Any inability to choose safely is reported as a blocker with the relevant reservation, dependency, or sprint state.

## Do not

- Do not choose work from a committed snapshot or plan before inspecting live sprintctl state.
- Do not treat a reservation as ownership. It is a coordination signal, not a lease: sprintctl allows overlap and reports it, and there is no token that proves exclusive possession. Coordinate on the reported overlap instead of assuming it cannot happen.
- Do not use append-only note tags as a priority queue; `next-work` does not order by them.
- Do not interrupt an item already reserved for execution by another live identity without an explicit handoff.
- Do not edit implementation files before the item is reserved when parallel overlap is possible.
