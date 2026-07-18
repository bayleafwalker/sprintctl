---
name: capability-receipt
description: Use at a meaningful release, sprint close, killed experiment, or operating change to draft an evidence-linked capability delta for an operator-directed procedural ratification decision.
---

## Goal

Capture what became newly reliable, cheaper, or better governed without turning
the boundary into an activity log or letting an agent declare its own value.
The output is a private `capability-receipt/v1` draft bound to source evidence
and presented to the operator for a separate procedural ratification decision.

## Inputs

- The project/repository id and boundary kind/reference.
- Contemporaneous expectations when they exist.
- Stable refs to relevant sprint decisions, commits, ADRs, pull requests, and
  verification results.
- The workspace artifact root. In this workspace, private receipts live under
  `/projects/dev/_artifacts/<repo-id>/capability/receipts/`.
- The owning sprint id and actor when this runs as part of `sprint-close`.

## Steps

1. **Qualify the boundary.** Ask whether this work moved a capability frontier,
   reinforced a capability worth preserving, or only instantiated a familiar
   pattern. If no capability delta is supported, stop without creating a
   receipt and record that evidence-backed boundary decision in the owning
   workflow.

2. **Gather sources, not a chronology.** Read only the evidence needed to test
   a before/after claim: the original expectation or plan, relevant decisions,
   immutable commit or document refs, and observable verification results.
   Sprint events and handoff bundles are source indexes; do not copy their
   activity stream into the receipt.

   For a sprint close, use the close event itself as the boundary ref:

   ```json
   {
     "kind": "sprint-event",
     "source": "sprintctl:<project>:sprint:<id>",
     "revision": "event:<positive integer>"
   }
   ```

   This is a local database reference, not a content digest or a
   migration-stable identity. Add an explicit receipt dependency on preserving
   the Sprintctl database, event, and source mapping. If that event is deleted
   or resequenced, do not treat the receipt as independently durable. The
   validator checks syntax, not event existence or content.

3. **Preserve epistemic position.** Distinguish contemporaneous expectations
   from reconstruction. If the record cannot support a counterfactual,
   displaced alternative, transfer, or other field, write an explicit
   `Unknown: ...` statement. Do not fill uncertainty with a plausible story.

4. **Draft the receipt.** Follow the normative semantics enforced by
   `templates/dispatch/scripts/validate_capability_receipts.py`; use
   `templates/dispatch/capability-receipt/capability-receipt.schema.json` as a
   structural/editor aid. Keep `status: draft` plus `publication: private`.
   Classify the primary locus as `embodied`, `delegated`, `governed`, or
   `institutionalised`. Do not add a numerical score. Each evidence entry must
   name a kind-specific exact ref, respect its documented durability boundary,
   and state the observation it supports.

5. **Write outside the repository.** Use a receipt id beginning with the exact
   project id plus a dot, for example `<project>.<date>.<boundary>`, and write
   JSON to:

   ```text
   /projects/dev/_artifacts/<repo-id>/capability/receipts/<receipt-id>.json
   ```

   `publication: private` is not access control. Never put a private receipt in
   a public repository or include secrets in a receipt.

6. **Validate and bind the exact bytes.** Run:

   ```bash
   python /projects/dev/agentops/templates/dispatch/scripts/validate_capability_receipts.py \
     /projects/dev/_artifacts/<repo-id>/capability/receipts/<receipt-id>.json \
     --expected-project <repo-id>
   ```

   Retain the reported SHA-256 digest.

7. **Record only the pointer.** When an owning sprint exists, append a
   `capability-receipt-drafted` event whose payload contains only
   `project`, `receipt_id`, `receipt_path`, and `receipt_sha256`, plus an
   optional short `boundary_summary`. Do not copy the unpublished body into
   sprint state. Sprintctl rejects missing, extra, non-canonical, or malformed
   pointer fields.

8. **Stop at ratification.** Present the proposed delta, evidence, unknowns,
   dependencies, and disconfirmation condition to the operator. An agent must
   not ratify, select for publication, publish, or silently revise the draft.
   A successor with `status: ratified` or `status: superseded` requires a
   separately persisted immutable `decision_ref`; `ratifier` is procedural
   attribution, not authenticated identity. The attestation fields and decision
   ref are procedural assertions; validator success is not proof that a person
   acted or had authority.

9. **Preserve append-only lineage.** If the operator later authorizes a
   successor, give it a new id and path, create it with exclusive,
   non-overwriting semantics, and fail if the path exists. Never truncate or
   replace a receipt. Set `supersedes` to the predecessor's exact id and byte
   digest, then validate both files together. This writer workflow is required,
   but the schema, validator, and filesystem state cannot prove retrospectively
   that exclusive creation occurred.

## Output Contract

- The artifact records a capability delta rather than completed activity.
- Every material claim is bounded by evidence or an explicit unknown.
- The validated draft is private and linked by id, path, and exact digest.
- Drafting, procedural ratification assertion, and publication remain distinct
  states.
- Candidate or published receipts contain the required procedural attestation
  assertions, external decision ref, and validator-resolved predecessor;
  validator success is not identity proof.
- Dependencies and a future disconfirmation condition make depreciation
  review possible.

## Do Not

- Do not create a receipt merely because a sprint closed.
- Do not infer value from repository size, prose quality, or task count.
- Do not reconstruct ex-ante beliefs without labelling the gap.
- Do not score capability numerically.
- Do not expose private receipt contents in sprint or audit events.
- Do not ratify or publish on the operator's behalf.
