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
- Live claim/session context supplied by the orchestrator.

## Steps

1. Confirm the action is implementation-ready: scope, allowed paths, acceptance checks, and verification commands are known.
2. Load the repo environment before running repo tools.
3. Read the dispatch packet and manifest. Respect explicit action routing first, then project defaults, action-class defaults, and global fallback.
4. Treat claims as orchestrator-owned. Use item/action context, but do not request or propagate claim tokens unless the orchestrator explicitly owns that operation.
5. Edit only within the allowed scope. If the needed change crosses the scope boundary, stop and report the required expansion.
6. Run targeted verification from the dispatch packet or manifest before broader gates.
7. Record exact verification commands and results for the handoff and for any dispatcher verification hook.
8. Once the scope is stable, route code-bearing work to `dispatch-review` when the manifest or action packet requires review.

## Output Contract

- Implemented scope with changed paths listed.
- Verification commands and pass/fail results.
- Residual risks, skipped checks, or required scope expansions called out.
- Stable diff ready for review, handoff, or PR prep.

## Do Not

- Do not make design decisions that should have been resolved by `dispatch-plan`.
- Do not mutate unrelated files or broaden the scope silently.
- Do not pass claim tokens to subagents or worker prompts.
- Do not mark work complete without reporting verification status.
