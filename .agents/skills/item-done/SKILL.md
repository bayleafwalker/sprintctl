---
name: item-done
description: Use when a sprint item's implementation is complete and verified. Captures knowledge events while context is hot, then marks done and refreshes the snapshot at the right scope boundary.
---

## Goal

Close a sprint item cleanly: confirm verification passes, capture durable knowledge before context cools, update sprint state, and commit at the right scope boundary.

If sprintctl mutation is not allowed in the current session, do not half-complete this workflow; report the blocked closeout steps explicitly instead.

## Inputs

- A completed, verified sprint item with an active reservation.
- A loaded project DB via `.envrc` or exported `SPRINTCTL_DB`.
- The reservation `id` for the current session.

## Steps

1. **Confirm verification is clean.** Run targeted checks for the files changed in this item — blocking, foreground, fast-fail. Use the repo's verification commands from the dispatch packet, manifest, or overlay. For pytest projects, a focused command should normally use `pytest <targeted-tests> -x --tb=short`. Do not proceed if targeted checks fail; use the self-healing loop (diagnose and fix, up to 5 cycles) before escalating.

2. **Reflect — log knowledge events while context is hot.** Before marking done, ask: did any of these happen?
   - A design choice was made between two viable options
   - A blocker was resolved by a non-obvious fix
   - A pattern emerged that applies to other items or future sprints
   - A migration or schema decision was made
   - An integration failure revealed a wrong assumption

   If yes, log it now:
   ```bash
   sprintctl event add --sprint-id <id> --item-id <item-id> \
     --type <decision|lesson-learned> --actor <actor> \
     --payload '{"summary":"<one sentence>","detail":"<reasoning>","tags":["<tag>"],"confidence":"<high|medium|low>"}'
   ```
   Include `summary` and `detail` at minimum. If nothing non-obvious happened, skip this step.

3. **Commit at the right scope boundary.** Use one commit per reviewable scope. Commit when this item closes a tight, related scope. Do not commit mechanically per item; do not bundle unrelated work.

4. **Land it.** A commit that stays on a local branch is not finished work. Merge or fast-forward into `main` — or into the main dev branch where `main` is protected — push, and let CI there catch what targeted checks did not.

   Open a PR only for a **specific, named action**, decided before you open it:

   | Reason | What makes it real |
   |---|---|
   | Escalation to human review | the judgement genuinely needs a person — trust roots, credential scope, destructive migrations |
   | A dispatched review session | the session is dispatched, not merely intended |
   | A CI check on that exact head | the repo's CI does not run on the target branch, so the PR is the only place it runs |

   If none of those applies, merge. A PR opened as a default "pending review" park — no reviewer named, none dispatched — is a defect, not caution: nothing arrives to review it, and the work becomes cross-repository drift that someone else has to reconstruct later. Releases are pinned separately for deployment, so an open PR is never what holds a change back from production.

   Where the repo's CI runs only on `pull_request`, say so when you land: either the merge went unverified, or the PR existed to get it verified. Do not let that gap pass silently.

5. **Mark done, then release the reservation.**
   ```bash
   sprintctl item status --id <id> --status done --expected-revision <revision>
   sprintctl reservation release --id <reservation-id> --actor <actor>
   ```
   Read `<revision>` from `sprintctl item show --id <id> --json`. These are two operations: the transition is guarded by the revision compare-and-swap, and releasing is a separate coordination signal. There is no token file to clean up.

6. **Refresh the snapshot only when it is needed now.** If updated sprint state must be shared immediately (handoff, end-of-batch, review handoff, sprint close), run `sprint-snapshot`. Otherwise stop after the release and batch the refresh at the next natural milestone instead of creating a mechanical per-item snapshot commit.

## Output Contract

- Targeted verification passes before the item closes.
- Knowledge events logged while context is hot.
- Item status and reservation state match live `sprintctl` state.
- Commit made at the scope boundary, not mechanically per item.
- Work landed on the target branch, or a PR open for one named, stated reason.

## Do Not

- Do not mark done without passing verification.
- Do not skip knowledge event logging to save time — log now or it is lost.
- Do not release another session's reservation as part of finishing your own work.
- Do not background a verification command whose exit status gates item closeout.
- Do not manufacture events if nothing non-obvious happened; one honest event beats three thin ones.
- Do not omit `--expected-revision`; a direct transition requires it, and it is what makes a stale basis fail closed.
- Do not silently skip the done transition, the release, or a required snapshot refresh; if state mutation is unavailable, report the block instead.
- Do not leave work on an unlanded local branch, and do not open a PR without naming the action it is open for; both turn finished work into drift nobody is tracking.
- Do not treat "the writer must not mint its own acceptance" as a reason to hold a PR open — CI on the target branch after the merge is an equally independent evaluation.
