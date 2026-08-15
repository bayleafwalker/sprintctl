# Coordinator Mode

Use coordinator mode only when one session must orchestrate sub-agents working
on the same item in parallel.

For normal single-session work, use a direct execute reservation instead.

If you only need sprint-level visibility for operators or cockpit-style views,
use [Sprint Takeup](takeup.md). Takeup does not grant ownership and does not
replace item reservations.

## When It Is Worth It

Coordinator mode is justified when:

- one item has parallelizable sub-work
- coordination visibility must remain continuous while workers rotate
- the extra ceremony is justified by the amount of overlap

If this is not true, avoid coordinator mode.

## Reservation Topology

Coordinator first:

```sh
sprintctl reservation reserve \
  --item-id <id> \
  --actor orchestrator \
  --role observation \
  --session-id orchestrator-session \
  --json
```

Sub-agent execution reservations under the coordinator:

```sh
sprintctl reservation reserve \
  --item-id <id> \
  --actor worker-a \
  --role execution \
  --session-id worker-a-session \
  --json
```

Orchestration is session and project context, not a relationship to the item,
so a coordinator reserves as an `observation`: it is watching work it does not
itself perform. It grants no exclusivity exception, and sub-agents create their
own `execution` reservations beside it. Several sub-agents on one item is
allowed and reported — each `reserve` returns the conflict set, and
execution-beside-execution is flagged `warning` so the coordinator can decide
whether that was intended. Advisory metadata (`instance_id`, branch, hostname,
pid) is never proof.

## Lifecycle Discipline

1. Coordinator reserves and stores `reservation_id`.
2. Workers reserve execute roles on the same item.
3. Workers touch activity when useful.
4. Workers release reservations when their slice is complete.
5. Coordinator transitions item state and performs final reassign or release.

## Failure Handling

Session lost:

```sh
sprintctl reservation list --all --json
sprintctl reservation reassign \
  --id <reservation-id> \
  --actor <same-actor> \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID" \
  --json
```

There is no token to rotate or recover.

## Anti-Patterns

- coordinator and workers sharing one reservation id
- skipping per-worker reservations and relying on branch naming
- using coordinator mode for solo work
- ending session without explicit reassign or release

## Related

- [Advanced Coordination Overview](../guides/advanced-coordination.md)
- [Sprint Takeup](takeup.md)
- [Reservation Discipline](reservation-discipline.md)
- [Agent Integration Example](../examples/AGENTS.sprintctl.md)
