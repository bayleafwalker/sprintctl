# Advanced Coordination

Use this mode only when one session is explicitly coordinating sub-agents on
the same work item.

## When To Use It

Use coordinator mode when:

- one item needs parallel sub-work
- the coordinator must keep visibility continuous across sub-agents
- the extra ceremony is justified by the amount of overlap

Do not use it for normal solo or solo-plus-one-agent work.

## Coordinator Pattern

Coordinator reserves first:

```sh
sprintctl reservation reserve \
  --item-id <id> \
  --actor orchestrator \
  --role coordinate \
  --session-id orchestrator-session \
  --json
```

Sub-agents then reserve execute roles:

```sh
sprintctl reservation reserve \
  --item-id <id> \
  --actor worker-a \
  --role execute \
  --session-id worker-a-session \
  --json
```

The coordinator role is informational metadata only; it does not grant an
exclusivity exception.

## Guardrails

- coordinator mode is advanced, not default
- shared branch/worktree metadata is advisory only
- each sub-agent still creates its own reservation
- reassignment discipline matters more than optimization here

## Related

- [Agent-Assisted Work](agent-assisted.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
- [Coordinator Mode](../advanced/coordinator-mode.md)
- [Reservation Discipline](../advanced/reservation-discipline.md)
- [UX Plan Pack](../plans/ux/00-index.md)
