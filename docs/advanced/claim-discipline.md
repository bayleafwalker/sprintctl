# Claim Discipline

Claims are the ownership mechanism for `sprintctl` items. This guide defines
the minimum operating discipline for reliable multi-session work.

## Ownership Proof

Proof requires both values:

- `claim_id`
- `claim_token`

Metadata fields (`actor`, `instance_id`, branch, worktree, hostname, pid) are
advisory only. They provide traceability, not authorization.

## Startup Sequence

1. read context: `sprintctl usage --context --json`
2. claim item with durable output:

```sh
sprintctl claim start \
  --item-id <id> \
  --actor <name> \
  --ttl 600 \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --runtime-session-id "$SPRINTCTL_RUNTIME_SESSION_ID" \
  --json
```

3. persist `claim_id` and `claim_token` for the full session

## Heartbeat Rule

Heartbeat at approximately half the TTL.

```sh
sprintctl claim heartbeat \
  --id <claim-id> \
  --claim-token <claim-token> \
  --ttl 600 \
  --actor <name>
```

Use shorter heartbeat intervals for high-risk or long test runs.

## Status Transition Rule

Item status updates are proof-gated for active ownership flows:

```sh
sprintctl item status \
  --id <item-id> \
  --status active|done|blocked \
  --actor <name> \
  --claim-id <claim-id> \
  --claim-token <claim-token>
```

Treat status and claim proof as one operation boundary.

## Recovery Rule

If session state is lost:

```sh
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json
```

If token is unavailable, rotate ownership proof:

```sh
sprintctl claim handoff \
  --id <claim-id> \
  --actor <name> \
  --mode rotate \
  --allow-legacy-adopt \
  --json
```

## Shutdown Rule

Before exit, every owned claim must be:

- handed off to the next runtime (`claim handoff`), or
- released (`claim release`)

Then emit a handoff bundle for session resumption:

```sh
sprintctl handoff --format json --output handoff.json
```

## Related

- [Coordinator Mode](coordinator-mode.md)
- [Work Loop](../guides/work-loop.md)
- [Resume Work](../guides/resume-work.md)
