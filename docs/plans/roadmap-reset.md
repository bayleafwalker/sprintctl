# Roadmap Reset: sprintctl as Agent-Native Execution Memory

## Summary

The roadmap is reset around what `sprintctl` is uniquely good at:

- deterministic context reconstruction
- resumable handoff
- structured execution history
- low-friction claim ergonomics

The product should remain interoperable with external planners and
orchestrators, but it should not compete on task-management surface area.

## Track Set

The active roadmap should use these tracks:

- `context-contract`
- `handoff-resume`
- `product-surface`
- `contract-hardening`
- `release-integrity`
- `memory-semantics`
- `interop`

Legacy tracks such as `execution-context`, large parts of `refs-and-artifacts`,
`dependencies`, and `worktree-awareness` should be treated as shipped or
partially shipped capability, not as greenfield design space.

## Iteration 1

Focus:

- freeze `usage --context --json` as the primary resume contract
- make text and JSON context output reflect the same section order
- upgrade `handoff` from bundle dump to working-memory snapshot
- verify the command surface in source, tests, and packaging metadata
- rewrite top-level docs around operating journeys instead of historical phases

Acceptance:

- context exposes `conflicts` and `next_action`
- handoff exposes grouped work, freshness, delta, and resume instructions
- roadmap and docs no longer describe shipped context/dep/ref features as pending

## Iteration 2

Focus:

- improve handoff quality and recovery ergonomics
- refine delta/evidence/freshness semantics
- tighten claim visibility and recovery guidance
- expand release-integrity checks around the packaged CLI surface

## Iteration 3

Focus:

- introduce a thin stdlib-only typed contract layer for context/handoff payloads
- canonicalize decision and coordination event payloads
- version JSON contracts explicitly

Constraints:

- database remains the source of truth
- CLI remains the write surface
- no ORM and no distributed coordination layer

## Iteration 4

Focus:

- document generic interoperability with task graphs, issue trackers, and orchestrators
- tighten dependency enforcement only where it improves execution safety
- expand failure-mode and performance coverage

Deferred:

- watch mode
- TUI work
- fzf-specific output
- any effort to turn sprintctl into a broader task manager

## Defaults

- `usage --context --json` is the live resume surface
- `handoff --format json` is the serialized working-memory surface
- `decision` is a first-class knowledge-bearing event
- snapshot files are archived records, not the roadmap source
