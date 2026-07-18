---
name: sprint-maintenance
description: Use to assess sprintctl health, claims, references, and carryover. Start read-only; perform mutations only after the review identifies an approved action.
---

## Goal

Keep sprintctl execution state trustworthy by diagnosing health, claim expiry, takeup state, document references, dependencies, and stale active work before any maintenance mutation is made.

## Inputs

- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB`.
- The sprint ID when more than one active sprint exists.
- Operator authority for any state-changing cleanup, carryover, or backlog request.

## Steps

1. Start with read-only environment and repository diagnostics:
   ```bash
   sprintctl doctor --json
   sprintctl maintain check --sprint-id <sprint-id> --json
   ```
   Treat tool provenance, backend, schema, stale-item, and sprint-health warnings as findings to review, not automatic repair instructions.
2. Inspect claims approaching expiry before they become ambiguous:
   ```bash
   sprintctl claim list-sprint --sprint-id <sprint-id> --expiring-within <seconds> --json
   ```
   Confirm each claim's identity with its holder; do not heartbeat, release, or adopt another session's claim based on labels alone.
3. Inspect takeup state. Review potential stale runtime sessions and the effect of a `takeup sweep` before invoking it, because the sweep releases takeups:
   ```bash
   sprintctl takeup list --sprint-id <sprint-id> --json
   ```
4. Validate execution links:
   - List item document refs with `sprintctl item ref list --id <item-id> --json` and confirm local doc paths resolve to real files.
   - List dependencies with `sprintctl item dep list --id <item-id> --json` and identify dangling, cyclic, or status-inconsistent edges.
   - Compare stale active items and blocked dependencies with the `maintain check` report rather than trusting an old render.
5. Produce a reviewable findings list: diagnosis, affected sprint/item/claim, evidence, proposed action, and whether it mutates state.
6. Only after review and authorization, apply the smallest necessary mutation:
   - `sprintctl takeup sweep --sprint-id <sprint-id> --json` for confirmed stale takeups.
   - `sprintctl maintain sweep --sprint-id <sprint-id> --json` for approved stale-item cleanup.
   - Add `--auto-close` only with explicit operator approval because it can close an overdue sprint.
7. At a real sprint boundary, use `sprintctl maintain carryover --from-sprint <source-id> --to-sprint <target-id> --json`, then verify the result and refresh the repository's shared sprint artifact if appropriate.
8. Run database maintenance when warranted: `sprintctl db integrity` (read-only structural and referential check; nonzero exit on failure) routinely, and `sprintctl db vacuum` after large deletions or sweeps. When recurring maintenance limitations surface, file a clearly scoped tooling-backlog request rather than inventing local workarounds.

## Output contract

- A diagnostic report distinguishes observed warnings from approved state changes.
- Claims, takeups, refs, dependencies, and active-item health have been checked against live state.
- Every mutation has an explicit rationale and result; `--auto-close` has operator approval.
- Carryover and tooling gaps are visible to the appropriate backlog rather than silently deferred.

## Do not

- Do not run `maintain sweep`, `takeup sweep`, or `maintain carryover` before the read-only review.
- Do not use `--auto-close` as a convenience flag; it changes sprint lifecycle state.
- Do not treat a broken document reference or dangling dependency as harmless backlog metadata.
- Do not mutate, adopt, or heartbeat a claim without its matching ownership proof.
- Do not run destructive database maintenance from this skill; use an explicitly approved tool capability when one exists.