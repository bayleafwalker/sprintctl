---
name: dispatch-build
description: Use when an approved plan or well-scoped action is ready for implementation. Executes the bounded scope, runs targeted verification, and leaves a reviewable result.
---

## Goal

Implement approved, bounded work using the repo's dispatch packet, manifest, and overlay as the contract.

## Inputs

- The action payload or sprint item.
- The approved implementation brief, when one exists.
- The repo dispatch manifest and overlay.
- Live reservation/session context supplied by the orchestrator.

## Steps

1. Confirm the action is implementation-ready: scope, allowed paths, acceptance checks, and verification commands are known.
2. Load the repo environment before running repo tools.
3. Read the dispatch packet and manifest. Respect explicit action routing first, then project defaults, action-class defaults, and global fallback.
4. Treat reservations as orchestrator-owned. Use item/action context, but do not reserve, reassign, release, or interrupt on your own initiative; the orchestrator owns that operation.
5. Edit only within the allowed scope. If the needed change crosses the scope boundary, stop and report the required expansion.
6. Run the worker-attempt registered falsifier from the packet. Workers must be
   able to execute their exact granted command IDs; inability to run the
   focused gate is a blocker, not a completion claim.
7. Apply adversarial acceptance: negative fixtures, wrong-order inputs,
   mutation-sensitive expectations, real calls through the claimed layer, and
   anti-vacuity checks where filters or parity are involved.
8. Stop rereading unchanged context when the packet's churn limits are reached.
   Once the diff and focused gate are stable, emit the structured candidate
   handoff immediately; cheap cache writes alone are not a reason to stop.
9. Record exact verification commands and results for the handoff and for any dispatcher verification hook.
10. Once the scope is stable, route code-bearing work to `dispatch-review` when the manifest or action packet requires review.

## Output Contract

- Implemented scope with changed paths listed.
- Verification commands and pass/fail results.
- Residual risks, skipped checks, or required scope expansions called out.
- Stable diff ready to land, or ready for review, handoff, or PR prep where one of those is actually called for.

## Do Not

- Do not make design decisions that should have been resolved by `dispatch-plan`.
- Do not mutate unrelated files or broaden the scope silently.
- Do not reserve, reassign, release, or interrupt work from a subagent or worker prompt; reservation changes belong to the orchestrating session.
- Do not mark work complete without reporting verification status.
- Do not treat "diff is stable" as done. A stable diff still has to land -- see `item-done` step 4. Stopping at PR prep by default is what leaves work stranded on branches nobody is tracking.
