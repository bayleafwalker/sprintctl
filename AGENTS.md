# sprintctl — Agent Integration Guide

sprintctl is a local sprint coordination CLI backed by a SQLite database.
It uses a **claim system** to give agents exclusive, time-limited ownership of
work items.  Read this file before touching any sprint item.

---

## Quick reference

```
sprintctl agent-protocol          # print full lifecycle protocol (human-readable)
sprintctl agent-protocol --json   # machine-readable JSON version
```

---

## Claim lifecycle (summary)

### 1. Startup — claim the item

```bash
sprintctl claim create \
  --item-id <id> --actor <your-name> \
  --type execute \
  --ttl 600 \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --json
```

Save **both** `claim_id` and `claim_token` from the response.
`claim_token` is a secret — store it for the entire session.

**Coordinators** (orchestrators spawning sub-agents): claim with `--type coordinate`.
Sub-agents then call `claim create` with `--coordinate-claim-id` and `--coordinate-claim-token`
to acquire their own `execute` claim without triggering a conflict.

### 2. Heartbeat — keep claim alive

```bash
sprintctl claim heartbeat \
  --id <claim_id> --claim-token <token> \
  --ttl 600 --actor <your-name>
```

Heartbeat every ~half-TTL.  The response includes `expires_at` and a warning
if the TTL is within the expiry-warn window.

### 3. Transition item status

```bash
sprintctl item status \
  --id <item_id> --status active \
  --actor <your-name> \
  --claim-id <claim_id> --claim-token <token>
```

Status transitions are **blocked** unless you provide valid claim proof.

### 4. Handoff — required before session end if work continues

```bash
sprintctl claim handoff \
  --id <claim_id> --claim-token <token> \
  --actor <next-agent-name> --mode rotate \
  --runtime-session-id <next-session-id> \
  --json
```

The response contains the new `claim_token` for the incoming agent.
The old token is immediately invalidated.

### 5. Release — when work is done

```bash
sprintctl claim release \
  --id <claim_id> --claim-token <token> --actor <your-name>
```

---

## Session resumption (context loss recovery)

If you restart and no longer have the `claim_token`:

```bash
# Find your claims by identity
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json

# If you still have the token, re-display it
sprintctl claim show --id <claim_id> --claim-token <token>

# If the token is gone, adopt the claim (mints a fresh proof)
sprintctl claim handoff \
  --id <claim_id> --actor <your-name> --mode rotate --allow-legacy-adopt --json
```

---

## Shutdown checklist

Before terminating:

1. For each owned claim: **handoff** to the next agent _or_ **release** it.
2. Run `sprintctl handoff` to write a bundle for the incoming session.
3. The bundle's `agent_shutdown_protocol` field repeats these instructions.

---

## Ownership model

- Proof = `claim_id` **+** `claim_token` (both required)
- `instance_id`, `hostname`, `pid`, `actor` name are advisory metadata only — never proof
- Default TTL: 300 s.  Use `--ttl` to increase for long-running tasks
- `coordinate` claims allow sub-agent `execute` claims; all other exclusive claim types block each other

---

## Environment variables

| Variable | Purpose |
|---|---|
| `SPRINTCTL_INSTANCE_ID` | Stable per-process UUID — set once and reuse across every claim call |
| `SPRINTCTL_RUNTIME_SESSION_ID` | Runtime session ID (auto-detected from `CODEX_THREAD_ID`) |
| `SPRINTCTL_DB` | Override the database path |
