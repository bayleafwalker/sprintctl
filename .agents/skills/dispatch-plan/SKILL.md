---
name: dispatch-plan
description: Use when a request needs architecture decisions, scope shaping, or cross-repo sequencing before implementation. Produces a decision-complete brief without repo mutations.
---

## Goal

Produce an implementation brief that is complete enough for a build worker to execute without making new product, architecture, or routing decisions.

## Inputs

- The user request or accepted scope description.
- The repo dispatch manifest and any selected overlay.
- Relevant sprint, actionq, docs, and architecture context.
- The repo's planning guide, when one exists.

## Steps

1. Confirm the request needs planning: unclear boundaries, new architecture, ambiguous verification, cross-repo sequencing, or missing acceptance criteria.
2. Load the repo environment before reading sprint, cluster, or queue state.
3. Read the dispatch manifest first. Treat model and harness assignment as structured routing data, not prose.
4. Read the repo overlay for domain constraints, affected paths, verification commands, and escalation rules.
5. Gather only the sprint/action/doc context needed to decide the scope.
6. Produce a brief with goal, allowed scope, out-of-scope, expected file areas, acceptance checks, verification, audit/review expectations, and unresolved questions.
7. Stop before implementation. If new sprint/action scope is needed, hand off to the repo's sprint or action creation workflow.

## Output Contract

- A concise, decision-complete implementation brief.
- Explicit scope boundaries and verification commands.
- No repo edits.
- Open questions separated from decisions.

## Do Not

- Do not implement changes in this skill.
- Do not choose a model from AGENTS prose when the manifest or action payload has routing data.
- Do not invent repo-specific rules that belong in an overlay.
