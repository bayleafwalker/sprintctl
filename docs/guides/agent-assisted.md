# Agent-Assisted Work

This is the default multi-session mode for `sprintctl`: one operator, one live
agent, explicit reservations only when overlap matters.

## Recommended Flow

1. Operator or agent reads live context:

```sh
sprintctl usage --context --json
```

2. Agent reserves one item:

```sh
sprintctl reservation reserve --item-id <id> --actor codex-session-1 --json
```

3. Agent records durable notes while working:

```sh
sprintctl item note --id <id> --type decision --summary "Pinned contract v1"
```

4. Agent marks the item done and releases the reservation:

```sh
REV=$(sprintctl item show --id <id> --json | jq -r '.item.status_revision')
sprintctl item status --id <id> --status done --actor codex-session-1 --expected-revision "$REV"
sprintctl reservation release --id <reservation-id> --actor codex-session-1
```

5. Or hands the reservation to the next live session:

```sh
sprintctl reservation reassign \
  --id <reservation-id> \
  --actor codex-session-2 \
  --session-id next-session \
  --json
```

6. Write a broader sprint snapshot when the next session needs more than reservation identity:

```sh
sprintctl handoff --output handoff.json
```

## Rules To Keep

- a reservation is an advisory coordination signal, not ownership proof
- `reservation reassign` transfers the visible reservation
- `handoff` transfers context, not the reservation
- `usage --context` remains the live restart surface even if a handoff bundle exists

## Related

- [Resume Work](resume-work.md)
- [Advanced Coordination](advanced-coordination.md)
- [Project Integration](project-integration.md)
- [Interoperability Patterns](interoperability.md)
