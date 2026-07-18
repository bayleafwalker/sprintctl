---
name: reconcile-project-contracts
description: Find drift between governing documents, sprintctl work items and refs, implementation, tests, verification evidence, generated projections, and handoff or PR artifacts. Use for documentation reconciliation, stale status or revision checks, missing provenance, broken source-of-truth links, backend contract drift, and pre-close or scheduled project health reviews.
---

# Reconcile Project Contracts

Produce a read-only, evidence-backed drift report. Do not repair findings unless a separate build action authorizes the exact mutations.

## Orient

Read the repository instructions, dispatch manifest and overlay, document registry, live sprint state, current Git revision, relevant tests, and generated artifacts.

Use this precedence when sources disagree:

1. State owner for live execution or runtime state.
2. Ratified document revision for intended behavior.
3. Code and migrations for observed implementation.
4. Tests and verification results for established evidence.
5. Generated projections and handoff material.
6. Planning or session notes.

## Check

- Every work item that requires governing scope has a document ref or explicit no-doc rationale.
- Each ref resolves to a stable document ID and immutable revision.
- Document lifecycle uses `draft`, `ratified`, or `superseded`; execution status remains in sprintctl.
- Supersession links are valid and acyclic.
- Active work has not silently switched to a newer document revision.
- Completed work records implementation and verification evidence.
- Contract examples, schema versions, command names, and recovery instructions agree with executable surfaces.
- Generated snapshots and projections identify their source revision and do not claim stronger consistency than their inputs.
- Cross-repository contracts live with the integration-policy owner; executable verification lives with the state owner.

## Classify findings

- `blocker`: work is executing against missing, invalid, or conflicting authority.
- `drift`: authoritative sources disagree but immediate safety is not compromised.
- `missing-evidence`: a claim exists without sufficient verification.
- `stale-projection`: generated or copied material lags its source.
- `advisory`: cleanup or clarity improvement.

## Output

Return findings first, ordered by severity. For each finding include the subject, disagreeing sources, exact evidence, authoritative owner, and smallest repair action. End with checked surfaces, skipped surfaces, and residual unknowns.

Never rewrite authored documents, ratify content, mutate sprint state, or update generated projections from reconciliation mode.
