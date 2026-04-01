# Agent-Assisted Work

This is the default multi-session mode for `sprintctl`: one operator, one live
agent, explicit claims only when overlap matters.

## Recommended Flow

1. Operator or agent reads live context:

```sh
sprintctl usage --context --json
```

2. Agent claims one item:

```sh
sprintctl claim create --item-id <id> --actor codex-session-1 --ttl 900 --json
```

3. Agent records durable notes while working:

```sh
sprintctl item note --id <id> --type decision --summary "Pinned contract v1"
```

4. Agent either releases the claim:

```sh
sprintctl claim release --id <claim-id> --claim-token <token> --actor codex-session-1
```

5. Or hands ownership to the next live session:

```sh
sprintctl claim handoff \
  --id <claim-id> \
  --claim-token <token> \
  --actor codex-session-2 \
  --mode rotate \
  --json
```

6. Write a broader sprint snapshot when the next session needs more than claim identity:

```sh
sprintctl handoff --output handoff.json
```

## Rules To Keep

- `claim_id + claim_token` is the only ownership proof
- `claim handoff` transfers ownership
- `handoff` transfers context, not proof
- `usage --context` remains the live restart surface even if a handoff bundle exists

## Related

- [Resume Work](resume-work.md)
- [Advanced Coordination](advanced-coordination.md)
- [Project Integration](project-integration.md)
