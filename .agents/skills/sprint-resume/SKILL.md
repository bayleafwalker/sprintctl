---
name: sprint-resume
description: Use when work already exists in sprintctl and the request is to continue, pick up, or resume an existing sprint item. Covers reservation identity checks, handoff behavior, and live-state verification before repo edits.
---

## Goal

Resume an already-registered sprint item from live `sprintctl` state without duplicating work, cutting across another session's reservation, or losing knowledge that should flow into `kctl` later.

## Inputs

- A request to continue sprint work, pick up the next item, or resume an already-scoped brief.
- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB`.
- The relevant sprint item, reservation, and recent event state.

## Steps

1. Confirm the work already exists in `sprintctl`. If it does not, stop and use `sprint-packet` instead.
2. Load the project DB first via `.envrc` or exported `SPRINTCTL_DB`.
3. If you are recovering after context loss, start with the combined resume surface rather than reassembling it by hand:
   ```bash
   sprintctl session resume --json
   ```
   It returns context, `next-work` explain output, and the live reservation picture in one call.
4. Otherwise inspect live sprint, item, reservation, and event state before touching repo files:
   `sprintctl sprint show --json`, `sprintctl item list --sprint-id <id> --json`, `sprintctl item show --id <item-id> --json`, `sprintctl reservation list --item-id <item-id> --all --json`.
5. Check the reservation picture:
   - If no reservation exists, register one with `sprintctl reservation reserve --item-id <id> --actor <actor> --session-id <runtime-session-id> --role execution --json`, then move the item to `active` with `sprintctl item status`.
   - Record the returned reservation id, `runtime_session_id`, `instance_id`, actor, and workspace metadata immediately.
   - If a live reservation already belongs to the current identity, refresh it with `sprintctl reservation touch` and continue.
   - If an execution reservation points to another live identity, do not touch it and do not edit repo files. Resolve a handoff first, with `sprintctl reservation reassign`.
6. Understand what a reservation is and is not. Overlapping reservations are allowed and reported, not refused: a reservation is a coordination signal, not a lease, and there is no token that proves exclusive possession. So the reservation tells you whom to coordinate with; it never by itself tells you that nobody else is working. Prove runtime identity rather than inferring it: prefer the harness-provided session id as `runtime_session_id` (for Codex, `CODEX_THREAD_ID`) and mint a stable `instance_id` once per live client or process start. Use `sprintctl agent-protocol --json` when you need the exact reserve, touch, reassign, or release command shape.
7. Record structured `sprintctl` events when design choices, resolved blockers, or reusable lessons occur. Use `decision` or `lesson-learned` types with `summary`, `detail`, `tags`, and `confidence` payload keys. The bar is met when any of these occur:
   - A design choice was made between two viable options
   - A blocker was resolved by a non-obvious fix
   - A pattern emerged that applies to other items or future sprints
   - A migration or schema decision was made
   - An integration failure revealed a wrong assumption
   Log immediately — context degrades fast, and retroactive logging at sprint close produces thin candidates.
8. If work pauses or changes hands, use `sprintctl reservation reassign` to transfer the reservation, then `sprintctl handoff --output <path>` when the next session also needs broader sprint context. Keep handoff artifacts local unless a tracked artifact was explicitly requested.
9. When implementation completes, move the item with `sprintctl item status` and release the reservation with `sprintctl reservation release` so live state matches reality.
10. After material sprint-state changes, refresh the shared snapshot with `sprint-snapshot`.

## Output Contract

- Repo edits start only after the live reservation picture is clear.
- Item status, relevant events, and snapshot state stay aligned with the actual execution state.
- Knowledge-worthy lessons are recorded while context is hot.

## Do Not

- Do not pick the next task from docs when existing item state is available in live `sprintctl`.
- Do not treat a reservation as exclusive ownership, and do not read the absence of one as permission — sprintctl permits overlap by design and only reports it.
- Do not touch, reassign, or work through another session's execution reservation because the actor label looks familiar.
- Do not treat matching branch, worktree, commit SHA, or workspace token as sufficient identity proof.
- Do not start implementation before the reservation picture or handoff state is clear.
- Do not wait until sprint close to log a lesson that should become an event now.
