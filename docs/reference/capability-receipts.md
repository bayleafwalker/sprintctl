# Capability receipts at sprint close

A capability receipt records what became newly reliable, cheaper, or better
governed across a meaningful project boundary. It is an unpublished workspace artifact meant
to remain an unpublished draft until operator-directed procedural ratification,
not another sprint status or an automatically inferred value claim. Receipt
validation and Sprintctl pointer checks do not authenticate a person or prove
that an actor had authority to ratify or publish it.

## Close-time flow

1. Finish the sprint close gate and capture contemporaneous evidence while the
   sprint is still active.
2. Close the sprint explicitly and name the actor:

   ```bash
   sprintctl sprint status --id <id> --status closed --actor <actor> --json
   ```

   Sprintctl atomically commits the `active -> closed` transition and one local
   `sprint-close-boundary` event. The JSON response includes
   `boundary_event_id` and `boundary_revision: event:<id>`. If the event insert
   fails, the status change rolls back. This is a database-local close-time
   reference, not evidence that capability changed or a content digest. It is
   valid only while that database, event row, and project/sprint mapping are
   preserved; deletion, archive import, or ID-remapping migration can break it.
3. Assess the boundary from the close-gate evidence and contemporaneous refs.
   If no reliable capability changed, append an evidence-backed `decision`
   event referring to the boundary event and stop. A routine close does not
   require a receipt.
4. For a supported delta, run the `capability-receipt` dispatch skill. Bind the
   receipt boundary to the sprint event using revision `event:<id>`. The skill
   drafts a `capability-receipt/v1` JSON receipt at:

   ```text
   /projects/dev/_artifacts/<repo-id>/capability/receipts/<receipt-id>.json
   ```

   `_artifacts` is unpublished workspace storage outside the owning
   repository's Git history. That path is not an access-control guarantee, and
   its durability depends on the workspace backup posture. Do not copy the
   receipt body into the sprint database.
5. After the unpublished file validates, record a `capability-receipt-drafted`
   event. Its complete payload contract is:

   ```json
   {
     "project": "<repo-id>",
     "receipt_id": "<repo-id>.<portable-receipt-id>",
     "receipt_path": "/projects/dev/_artifacts/<repo-id>/capability/receipts/<receipt-id>.json",
     "receipt_sha256": "<64-lowercase-hex>",
     "boundary_summary": "<optional one-line summary>"
   }
   ```

   `project` and `receipt_id` are portable identifiers, and `receipt_id` must
   start with `<project>.`. The path must match the project and id exactly.
   `boundary_summary` is optional, one line, and at most 280 characters. Any
   unknown field is rejected, including a receipt body, model output, prompt,
   or other hidden private prose. Unknown `capability-receipt-*` names are also
   rejected rather than treated as generic payload channels.

   Before committing the pointer, Sprintctl requires the sprint to be closed
   with exactly one local `sprint-close-boundary`, reads the referenced file,
   verifies its exact SHA-256, and checks only the locally provable receipt
   facts: `capability-receipt/v1`, matching id/project, `status: draft`,
   `publication: private`, and a `sprint-event` boundary source/revision matching
   this project, sprint, and close event. Agentops remains responsible for full
   receipt semantics. PostgreSQL always binds project to its repository tenant;
   the normal local CLI binds it when repository identity resolves. Direct
   low-level SQLite callers without `expected_project` prove only internal
   project/id/path/file consistency.
6. Finish the normal snapshot and knowledge-review steps. The operator reviews
   the unpublished draft outside sprintctl and directs any procedural
   ratification. A later ratification is an append-only successor receipt with a
   separately persisted exact decision reference; it never rewrites the draft.
   Neither the validator nor the pointer record authenticates the decision-maker
   or proves their authority. Corrections likewise produce a new receipt and
   decision.

The draft is agent-generated evidence synthesis. It is not proof that value was
created, and agents must not ratify it. Neither sprintctl nor the dispatch skill
automatically infers project value, changes publication state, or publishes a
receipt. Private, candidate, and published remain deliberate operator choices.

## Boundaries that do not ratify a receipt

Claim release, takeup release, and handoff are session or ownership operations,
not capability boundaries. Likewise, `sprintctl maintain sweep --auto-close` is
operational housekeeping: it emits `auto-closed-overdue`, not a
`sprint-close-boundary`, and does not count as a ratified capability boundary.
An auto-closed sprint cannot be retroactively treated as having emitted that
boundary. Any later receipt must be justified by a separate, intentional
project boundary and its own append-only evidence.

## Export and migration authority

Generic event APIs cannot create `sprint-close-boundary` or imported lifecycle
records. A normal `sprintctl export`/`import` is an archive copy: exact typed
payloads are prevalidated and retained as `sprint-close-boundary-imported` and
`capability-receipt-drafted-imported`. Their exact wrapper records the source
event id/type/payload, but they do not authorize new receipt drafts.

`migrate-to-remote` is a separately explicit trusted backend state transfer. It
preserves the typed event names, payloads, and IDs exactly. If local capability
boundary events exist, trusted migration rejects `--remap-ids`, because changing
event IDs would invalidate `event:<id>` receipt references.
