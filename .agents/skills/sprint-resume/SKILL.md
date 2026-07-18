---
name: sprint-resume
description: Use when work already exists in sprintctl and the request is to continue, pick up, or resume an existing sprint item. Covers claim identity checks, handoff behavior, and live-state verification before repo edits.
---

## Goal

Resume an already-registered sprint item from live `sprintctl` state without duplicating work, stealing another session's claim, or losing knowledge that should flow into `kctl` later.

## Inputs

- A request to continue sprint work, pick up the next item, or resume an already-scoped brief.
- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB`.
- The relevant sprint item, claim, and recent event state.

## Steps

1. Confirm the work already exists in `sprintctl`. If it does not, stop and use `sprint-packet` instead.
2. Load the project DB first via `.envrc` or exported `SPRINTCTL_DB`.
3. Inspect live sprint, item, claim, and event state before touching repo files:
   `sprintctl sprint show --json`, `sprintctl item list --sprint-id <id> --json`, `sprintctl item show --id <item-id> --json`, `sprintctl claim list --item-id <item-id> --json`, `sprintctl claim list-sprint --sprint-id <id> --json`.
   If recovering after context loss, use `sprintctl claim resume --instance-id <id>` or `--runtime-session-id <id>` to locate claims that still belong to the current live identity.
4. Check claim identity:
   - If no claim exists, prefer `sprintctl claim start` so claim creation and `pending → active` happen atomically.
   - Record `claim_id`, `claim_token`, `runtime_session_id`, `instance_id`, actor, and workspace metadata immediately.
   - Persist the token to `.sprintctl/claims/claim-<item_id>.token` as a crash-recovery path; keep it in session memory during normal execution.
   - If the current session already holds the active claim's `claim_token` and identity clearly matches, refresh the heartbeat and continue.
   - If reattaching after context loss, read `.sprintctl/claims/claim-<item_id>.token` before deciding ownership.
   - If the claim token is missing, identity is ambiguous, or the claim points to another live workspace, do not heartbeat it and do not edit repo files. Resolve a handoff first.
5. Prove runtime identity, not labels: prefer the harness-provided session id as `runtime_session_id` (for Codex, `CODEX_THREAD_ID`) and mint a stable `instance_id` once per live client or process start. Shared actor labels and workspace metadata alone are not enough to prove ownership. Use `sprintctl agent-protocol --json` when you need the exact create, heartbeat, handoff, or release command shape.
6. Move the item to `active` before implementation when appropriate (already handled by `claim start`).
7. Record structured `sprintctl` events when design choices, resolved blockers, or reusable lessons occur. Use `decision` or `lesson-learned` types with `summary`, `detail`, `tags`, and `confidence` payload keys. The bar is met when any of these occur:
   - A design choice was made between two viable options
   - A blocker was resolved by a non-obvious fix
   - A pattern emerged that applies to other items or future sprints
   - A migration or schema decision was made
   - An integration failure revealed a wrong assumption
   Log immediately — context degrades fast, and retroactive logging at sprint close produces thin candidates.
8. If work pauses or changes hands, use `sprintctl claim handoff` to transfer the claim, then `sprintctl handoff --output <path>` when the next session also needs broader sprint context. Keep handoff artifacts local unless a tracked artifact was explicitly requested.
9. When implementation completes, prefer `sprintctl item done-from-claim` so done + claim release stay tied to ownership proof. Remove `.sprintctl/claims/claim-<item_id>.token` after successful done or release so recovery state matches live ownership.
10. After material sprint-state changes, refresh the shared snapshot with `sprint-snapshot`.

## Output Contract

- Repo edits start only after live ownership is clear.
- Item status, relevant events, and snapshot state stay aligned with the actual execution state.
- Knowledge-worthy lessons are recorded while context is hot.

## Do Not

- Do not pick the next task from docs when existing item state is available in live `sprintctl`.
- Do not heartbeat or reuse another session's exclusive claim because the actor label looks familiar.
- Do not treat matching branch, worktree, commit SHA, or workspace token as sufficient ownership proof.
- Do not start implementation before claim identity or handoff state is clear.
- Do not wait until sprint close to log a lesson that should become an event now.
