# Interoperability Patterns

Use this guide when `sprintctl` has to coexist with another planning or
execution system.

`sprintctl` is not the source of truth for every project-management field. It
is the local execution-memory layer: claims, resumable context, decisions,
handoff state, and the minimum dependency data needed to keep active work safe.

## System Boundaries

Use this split by default:

- issue tracker or backlog tool: long-lived planning, ownership, reporting
- task graph or planner: dependency modeling and prioritization
- orchestrator: session creation and worker delegation
- `sprintctl`: live execution state inside the repo

If two systems disagree about live execution, prefer `sprintctl` for the active
item, claims, and recent decisions because that state is local, proof-aware,
and built for session recovery.

## Minimal Mapping

Mirror only the fields that change execution behavior inside the repo:

- accepted issue or ticket -> `sprintctl item`
- blocking prerequisite -> `sprintctl item dep add`
- external PR, issue, or spec -> `sprintctl item ref add`
- decision that matters on resume -> `sprintctl item note --type decision`
- in-flight ownership -> `sprintctl claim create`
- resume payload for the next session -> `sprintctl handoff --format json`

Do not try to round-trip every external attribute into `sprintctl`. Status
mirrors, labels, story points, and queue metadata usually belong in the
external system unless they directly affect repo execution.

## Pattern: Issue Trackers

Use the issue tracker to decide what should be worked on. Use `sprintctl` once
the work is accepted into an active repo session.

Example:

```sh
sprintctl item add \
  --sprint-id <sprint-id> \
  --track interop \
  --title "Implement typed handoff recovery"

sprintctl item ref add \
  --id <item-id> \
  --type issue \
  --url https://tracker.example.com/PROJ-142 \
  --label "PROJ-142"
```

Recommended rule:

- create `sprintctl` items only for work that can become active in the current repository
- attach the tracker URL as a ref instead of copying the whole ticket into notes
- write decisions in `sprintctl` when they matter for the next coding session, even if the final summary also lands in the tracker

## Pattern: Task Graphs And Dependency Planners

Let the external planner compute the broad graph. Mirror only the blockers that
must prevent unsafe local execution.

Example:

```sh
# Item 3 must finish before item 7 can start
sprintctl item dep add --id 3 --blocks-item-id 7

# Ready suggestions exclude unresolved blockers
sprintctl next-work --json
```

Recommended rule:

- keep external graph depth outside `sprintctl` unless a dependency changes what an agent may safely start
- add local deps for real execution gates, not for every planning relationship
- use `next-work` and `usage --context` as the local safety check before claiming work

This keeps dependency enforcement narrow and useful instead of turning
`sprintctl` into a second planning system.

## Pattern: Orchestrators And Sub-Agents

An orchestrator may decide which item to run next, but `sprintctl` should still
own claim proof and recovery context for the repo session.

Coordinator pattern:

```sh
COORD=$(sprintctl claim create \
  --item-id 7 \
  --actor orchestrator \
  --type coordinate \
  --ttl 1800 \
  --json)

sprintctl claim create \
  --item-id 7 \
  --actor worker-a \
  --type execute \
  --coordinate-claim-id <coord-id> \
  --coordinate-claim-token <coord-token> \
  --json
```

Recommended rule:

- orchestrators choose work; `sprintctl` proves who currently owns execution
- use `claim handoff` to transfer ownership between sessions
- use `handoff --format json` to transfer working memory
- treat `usage --context --json` as the live re-sync call after any orchestrator restart

## Guardrails

- do not expose `claim_token` in tickets, PR comments, or handoff bundles
- do not mirror every external queue or assignee update into local sprint items
- do not add dependency edges unless they should block `next-work`
- do not treat committed snapshots as fresher than live `usage --context`

## Related

- [Start Here](start-here.md)
- [Project Integration](project-integration.md)
- [Advanced Coordination](advanced-coordination.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
