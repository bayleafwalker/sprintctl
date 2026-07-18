---
name: verify-state-protocols
description: Model, test, and repair stateful software protocols involving lifecycle transitions, queues, claims, leases, retries, idempotency, reconciliation, append-only histories, canonical projections, crash recovery, dual writes, concurrent workers, or backend semantic parity. Also use for PlusCal/TLA+, property-based or stateful testing, concurrent-history analysis, Jepsen-style consistency terminology, and worker-ready verification plans.
---

# Verify State Protocols

Establish what the system promises, express those promises precisely, and use the least elaborate verification technique that can expose relevant failures.

Keep protocol documentation, formal models, executable tests, implementation, and result evidence as separate artifacts that must agree.

## Select the mode

Infer the mode unless the action packet supplies `mode=<mode>`.

| Mode | Purpose | Mutation |
|---|---|---|
| `survey` | Inspect ownership, semantics, risks, and coverage | None |
| `plan` | Produce a decision-complete verification plan and context packets | Requested docs only |
| `verify` | Add models, tests, fixtures, and verification docs | No product-semantic changes |
| `repair` | Reproduce and fix a confirmed protocol defect | Explicitly authorized product changes |

Treat `full` as a meta-dispatch sequence of independent plan, verify, repair, and review actions. Never let one worker silently acquire all mutation authority.

Assessment requests default to `survey`; test implementation defaults to `verify`. Do not enter `repair` without explicit authorization.

## Choose proportionate depth

- **Depth 0 — contract review:** transition table, pre/postconditions, invalid transitions, source-of-truth and projection mapping.
- **Depth 1 — stateful testing:** executable reference model, generated operation sequences, property tests, minimized traces.
- **Depth 2 — concurrent histories:** independent connections or processes, deterministic barriers or fault hooks, invocation/completion histories, consistency oracle, retry and stale-owner scenarios.
- **Depth 3 — formal model:** PlusCal/TLA+ or equivalent for leases, fencing, irreversible transitions, concurrent canonical projections, multi-object atomicity, dual writes, ambiguous crash outcomes, or backend parity.

Use a real Jepsen deployment only for a genuinely distributed system under node, network, clock, or replication faults. Otherwise reuse its history and consistency vocabulary in a smaller application harness.

## Orient from authoritative sources

Read in order:

1. Repository instructions and dispatch overlay.
2. Governing contract document and exact revision.
3. Schemas and migrations.
4. Command or API entry points.
5. State-changing implementation functions.
6. Existing tests.
7. Recovery and operator docs.
8. Planning documents.

Record disagreements. Implementation is evidence, not automatically intent; planning documents are not the execution control plane.

Identify authoritative mutable state, append-only history, projections and caches, external effects, repository ownership boundaries, backend differences, transaction isolation, clock assumptions, and fault boundaries.

## Define each closed protocol boundary

Record:

- subject and authoritative state variables;
- operations and actors;
- preconditions, success effects, and guaranteed failure effects;
- unknown outcomes after interruption;
- linearization point or commit boundary;
- retry and idempotency semantics;
- recovery and reconciliation operation;
- projection semantics;
- safety properties;
- liveness actually promised, including fairness assumptions.

Distinguish command accepted, state committed, external effect completed, history recorded, and projection observed. Never describe a projection as more consistent than its inputs.

## Use precise consistency terms

- **Linearizable:** each completed single-object operation takes effect once between invocation and response, respecting real-time order.
- **Serializable:** committed multi-object transactions are equivalent to a serial execution.
- **Strict serializable:** serializable and compatible with real-time order.
- **Lease:** time-bounded permission.
- **Fencing token or epoch:** monotonically ordered proof that storage uses to reject stale owners.
- **Unknown outcome:** the caller cannot determine whether an interrupted operation committed.

Do not conflate at-most-once effect, at-least-once delivery, and exactly-once processing.

Classify each conclusion as `formally-checked`, `exhaustively-checked-within-bound`, `property-tested`, `concurrency-tested`, `example-tested`, `documented-only`, or `unknown`. Never promote a weaker result into a stronger claim.

## Create versioned packets and evidence

Use data-only packets conforming to:

- `../../schemas/test-context.schema.json` for reusable test intent;
- `../../schemas/verification-result.schema.json` for what actually ran and what it established.

Packets must not contain executable code, secrets, claim tokens, or copies of production validation logic.

Capture the governing document revision, implementation Git SHA, backend, isolation level, tool versions, bounds, seeds, exercised faults, and counterexamples. Use symbol-level implementation anchors and validate that they still resolve.

For formal models, provide an explicit refinement mapping from model operations and variables to implementation operations and storage. A checked model proves the model, not the implementation.

## Verify and report

1. Run the smallest depth that covers the risk.
2. Preserve minimized counterexamples and deterministic replay data.
3. Run backend-parity scenarios against the same reference model when parity is claimed.
4. Emit a verification result packet for every executed context.
5. Report unresolved ambiguity as `unknown`, with the missing evidence needed to strengthen the claim.
6. In `repair`, reproduce before changing product code and route the stable fix to an independent findings-first review.

Do not model ordinary CRUD, formatting, or adapters merely because they exist. Do not add distributed-system machinery to compensate for an undefined product contract.
