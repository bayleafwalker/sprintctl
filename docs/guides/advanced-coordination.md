# Advanced Coordination

Use this mode only when one session is explicitly coordinating sub-agents on
the same work item.

## When To Use It

Use coordinator mode when:

- one item needs parallel sub-work
- the coordinator must keep ownership continuity across sub-agents
- the extra ceremony is justified by the amount of overlap

Do not use it for normal solo or solo-plus-one-agent work.

## Coordinator Pattern

Coordinator claims first:

```sh
sprintctl claim create \
  --item-id <id> \
  --actor orchestrator \
  --type coordinate \
  --ttl 1800 \
  --json
```

Sub-agents then claim under the coordinator:

```sh
sprintctl claim create \
  --item-id <id> \
  --actor worker-a \
  --type execute \
  --coordinate-claim-id <coord-id> \
  --coordinate-claim-token <coord-token> \
  --json
```

## Guardrails

- coordinator mode is advanced, not default
- shared branch/worktree metadata is advisory only
- each sub-agent still gets its own proof-backed claim
- handoff discipline matters more than optimization here

## Related

- [Agent-Assisted Work](agent-assisted.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
- [Coordinator Mode](../advanced/coordinator-mode.md)
- [Claim Discipline](../advanced/claim-discipline.md)
- [UX Plan Pack](../plans/ux/00-index.md)
