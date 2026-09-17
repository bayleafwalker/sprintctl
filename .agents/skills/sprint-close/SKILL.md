---
name: sprint-close
description: Use at the end of a sprint to verify the close gate, preserve evidence, and close the sprint cleanly.
---

## Goal

Encode the full sprint close-out sequence so steps are not repeated ad-hoc across sessions. Produces a confirmed close gate, a committed snapshot, reviewed knowledge candidates, and a closed sprint record.

## Inputs

- The sprint ID to close (confirm with `sprintctl sprint list` if uncertain).
- A loaded project DB via `.envrc` or exported `SPRINTCTL_DB` and `KCTL_DB`.
- Confirmation that all items intended for this sprint are in `done` or explicitly deferred.

## Steps

1. **Run the repo's sprint-close gate.** Use the verification commands from the repo's dispatch manifest or overlay (e.g., targeted tests, contract checks). Report pass/fail. If the gate fails, diagnose and fix before continuing. Do not close a sprint on a failing gate.

2. **Confirm sprint item health.**
   ```bash
   sprintctl maintain check --sprint-id <id>
   ```
   Review stale, blocked, or unclaimed items. Decide whether to defer or carry forward unfinished work before proceeding.

3. **Record any final close rationale.** Add it before the sprint becomes
   terminal:

   ```bash
   sprintctl event add --sprint-id <id> --type decision --actor <actor> \
     --payload '{"summary":"<close rationale>","detail":"<what was deferred and why>"}'
   ```

4. **Close the sprint and retain its local boundary reference.** Explicit close
   atomically commits the status transition and one local boundary event:

   ```bash
   sprintctl sprint status --id <id> --status closed --actor <actor> --json
   ```

   Retain `boundary_event_id` and `boundary_revision` (`event:<id>`) from the
   response. This revision is a local database row reference, not a content
   digest or a migration-stable identity. Preserve the Sprintctl database and
   event to keep this reference valid.

5. **Refresh the sprint snapshot.** Run `sprint-snapshot` to commit the final state. Use a standalone `chore:` commit.

6. **Extract knowledge.** Run `kctl-extract`. Key steps:
   ```bash
   kctl extract --sprint-id <id>
   kctl review list --kind all
   ```
   Review all candidates before completing.

7. **Verify clean state.**
   ```bash
   kctl status --sprint-id <id> --kind all
   ```

## Output Contract

- Sprint close gate passes before close-out proceeds.
- All sprint items are in `done` or explicitly deferred with a recorded reason.
- Sprint status and the local `sprint-close-boundary` event commit atomically.
- Final snapshot committed.
- All knowledge candidates reviewed (approved or rejected).
- Sprint status is `closed` in `sprintctl`.

## Do Not

- Do not skip the close gate.
- Do not treat task completion or sprint closure as proof of capability.
- Do not close the sprint with `candidate` knowledge entries still unreviewed.
- Do not carry implementation work into close-out commits.
