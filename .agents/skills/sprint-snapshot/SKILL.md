---
name: sprint-snapshot
description: Use to render and commit the current sprint state as a reviewable snapshot. Use after significant status updates or at sprint milestones, not as a way to choose the next item.
---

## Goal

Render the current `sprintctl` sprint state to a committed plaintext snapshot that serves as the shared, diffable record of sprint progress.

## Inputs

- The target sprint ID if the active sprint is not the one to snapshot.
- A loaded project DB environment via `.envrc` or exported `SPRINTCTL_DB`.
- Confirmation that the relevant live sprint state has already been updated in `sprintctl`.

## Steps

1. Load the project DB first via `.envrc` or exported `SPRINTCTL_DB`.
2. Run `sprintctl render` to produce the current sprint document. Pass `--sprint-id <id>` when the target is not the active sprint.
3. Write output to the repo's sprint snapshot path (check the repo overlay; default is `docs/sprint-snapshots/sprint-current.txt`).
4. Review the diff to confirm the snapshot reflects actual `sprintctl` state rather than manual edits.
5. If the workflow calls for a snapshot commit, stage and commit it separately: `chore: update sprint snapshot`.

## Output Contract

- The snapshot file matches `sprintctl render` output for the intended sprint.
- Reviewers can diff sprint-state changes without consulting the local DB.
- Snapshot commits are standalone `chore:` commits, never mixed into feature work.

## Do Not

- Do not edit the snapshot file manually — always generate it via `sprintctl render`.
- Do not run against an unscoped home-directory DB by accident.
- Do not commit snapshot updates as part of feature commits.
- Do not use the snapshot as the primary source for choosing active work while the local DB is available.
