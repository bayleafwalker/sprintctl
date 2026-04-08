# Coordinator Mode

Use coordinator mode only when one session must orchestrate sub-agents working
on the same item in parallel.

For normal single-session work, use a direct execute claim instead.

## When It Is Worth It

Coordinator mode is justified when:

- one item has parallelizable sub-work
- ownership must remain continuous while workers rotate
- explicit handoff proof matters more than command simplicity

If this is not true, avoid coordinator mode.

## Claim Topology

Coordinator first:

```sh
sprintctl claim create \
  --item-id <id> \
  --actor orchestrator \
  --type coordinate \
  --ttl 1800 \
  --json
```

Sub-agent execute claims under the coordinator:

```sh
sprintctl claim create \
  --item-id <id> \
  --actor worker-a \
  --type execute \
  --coordinate-claim-id <coord-claim-id> \
  --coordinate-claim-token <coord-claim-token> \
  --json
```

Each worker gets separate proof (`claim_id + claim_token`). Advisory metadata
(`instance_id`, branch, hostname, pid) is never proof.

## Lifecycle Discipline

1. Coordinator starts and stores token securely.
2. Workers create execute claims using coordinator proof.
3. Coordinator heartbeats long-lived claim at half-TTL.
4. Workers release claims when their slice is complete.
5. Coordinator transitions item state and performs final handoff or release.

## Failure Handling

Token lost during session:

```sh
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json
sprintctl claim handoff \
  --id <claim-id> \
  --actor <same-actor> \
  --mode rotate \
  --allow-legacy-adopt \
  --json
```

This rotates proof and invalidates prior token material.

## Anti-Patterns

- coordinator and workers sharing one token
- skipping per-worker claims and relying on branch naming
- using coordinator mode for solo work
- ending session without explicit handoff or release

## Related

- [Advanced Coordination Overview](../guides/advanced-coordination.md)
- [Claim Discipline](claim-discipline.md)
- [Agent Integration Example](../examples/AGENTS.sprintctl.md)
