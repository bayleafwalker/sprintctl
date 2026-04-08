# sprintctl — Agent Integration Guide

> **Environment reference:** `/projects/dev/AGENTS.md` — devbox vs workstation context, tool install persistence, cluster access, direnv, PATH, cost logging, and mid-session switching.


## Tech Stack

Primary language: Python. Use `pytest` for testing. Markdown for documentation. Package manager: `uv` / `pipx`.

## Environment setup

### Required environment variables

| Variable | Purpose |
|---|---|
| `SPRINTCTL_INSTANCE_ID` | Stable per-process UUID — set once and reuse across every claim call |
| `SPRINTCTL_RUNTIME_SESSION_ID` | Runtime session ID (auto-detected from `CODEX_THREAD_ID`) |
| `SPRINTCTL_DB` | Override the database path (default: `~/.sprintctl/sprintctl.db`) |

**Validate before use:**
```bash
echo $SPRINTCTL_DB   # for project-scoped work, must contain the project path, not ~/
```

> Using the home-directory default (`~/`) silently operates on the wrong database when working within a project that has its own `.sprintctl/` directory.

No cluster context — sprintctl is a local-first CLI tool.

## Development workflow

- Run `pytest` after making changes. Report pass/fail count before committing.
- **Never commit with failing tests.**
- **Commit after each sprint item completes — not at the end of a session.** One item = one commit. Run tests before each commit.
- Behavior changes must include updated or new tests in the same commit.

### Self-healing test loop

If tests fail after a change, diagnose the root cause, fix, and re-run — up to **5 cycles** — before escalating. Only escalate if still failing after 5 attempts or if a design decision is required.

---

sprintctl is a local sprint coordination CLI backed by a SQLite database.
It uses a **claim system** to give agents exclusive, time-limited ownership of
work items.  Read this file before touching any sprint item.

---

## Quick reference

```
sprintctl agent-protocol          # print full lifecycle protocol (human-readable)
sprintctl agent-protocol --json   # machine-readable JSON version
```

If your global `sprintctl` binary is stale and missing commands documented in
this file, run the repo-local source entrypoint instead:

```bash
.venv/bin/python -m sprintctl <command> ...
```

Keep global tool installs fresh before longer sessions:

```bash
# Preferred when pipx is available
pipx upgrade sprintctl && pipx upgrade kctl

# Equivalent uv tool flow
uv tool upgrade sprintctl kctl
```

---

## Claim lifecycle (summary)

### 1. Startup — claim the item

```bash
sprintctl claim start \
  --item-id <id> --actor <your-name> \
  --ttl 600 \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --json
```

Save **both** `claim_id` and `claim_token` from the response.
`claim_token` is a secret — store it for the entire session.
sprintctl also writes a local recovery token file next to the active database
so `claim recover` can restore the secret after context loss.

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

### 3. Transition item status (done/blocked, or active when using `claim create`)

```bash
sprintctl item status \
  --id <item_id> --status active|done|blocked \
  --actor <your-name> \
  --claim-id <claim_id> --claim-token <token>
```

Status transitions are **blocked** unless you provide valid claim proof.
`claim start` already performs the `pending -> active` transition.

### 4. Handoff — required before session end if work continues

```bash
# Transfer claim ownership to next session (token rotates)
sprintctl claim handoff \
  --id <claim_id> --claim-token <token> \
  --actor <next-agent-name> --mode rotate \
  --runtime-session-id <next-session-id> \
  --json

# Produce a sprint handoff bundle for the incoming session
sprintctl handoff [--sprint-id N] [--output path] [--format json|text]
```

The claim handoff response contains the new `claim_token` for the incoming agent.
The old token is immediately invalidated.

`--format text` produces a human-readable bundle (status groups, active claims,
shutdown protocol). `--format json` (default) produces the machine-parseable
bundle for agent session resumption.

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

# Recover the locally persisted token that sprintctl wrote when the claim was created
sprintctl claim recover --id <claim_id> --json

# If no local recovery file exists and the token is gone, adopt the claim (mints a fresh proof)
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
- sprintctl can restore the locally persisted token via `claim recover`, but the recovered secret is still the proof used by claim operations
- `instance_id`, `hostname`, `pid`, `actor` name are advisory metadata only — never proof
- Default TTL: 300 s.  Use `--ttl` to increase for long-running tasks
- `coordinate` claims allow sub-agent `execute` claims; all other exclusive claim types block each other

---

## Reading current sprint context

Before picking up work, read the current state in one call:

```bash
sprintctl usage --context [--sprint-id N] [--json]
```

This emits: sprint summary, active claims (who owns what), stale/blocked items,
ready-to-start items (no unresolved deps), and recent knowledge candidates.

Use `--json` for machine-readable output — compact enough to paste into a prompt
without summarisation.

```bash
# See what's ready to pick up
sprintctl next-work [--sprint-id N] [--json] [--explain]

# See your current git context (branch, sha, worktree)
sprintctl git-context [--json]
```

---

## Refs and deps

After creating or claiming an item you can attach external references:

```bash
# Attach a PR, issue, doc, or other URL
sprintctl item ref add --id <item-id> --type pr --url <url> [--label <text>]
sprintctl item ref list --id <item-id> [--json]
sprintctl item ref remove --id <item-id> --ref-id <ref-id>
```

Record blocking dependencies between items:

```bash
# item-A must finish before item-B can start
sprintctl item dep add --id <item-A-id> --blocks-item-id <item-B-id>
sprintctl item dep list --id <item-id> [--json]
sprintctl item dep remove --id <item-id> --dep-id <dep-id>
```

Items with unresolved blockers are excluded from `next-work` output.

---

## Recording git context on notes and claims

`item note` accepts git provenance fields so knowledge candidates carry their origin:

```bash
sprintctl item note --id <item-id> --type decision \
  --summary "Chose RSA over ECDSA for compatibility" \
  --git-branch feat/auth --git-sha abc1234 \
  --evidence-item-id <related-item-id> \
  --actor <your-name>
```

`claim create` and `claim heartbeat` accept `--branch`, `--commit-sha`,
`--worktree`, and `--pr-ref` to keep the claim record current as work progresses.

---

## Environment variables

| Variable | Purpose |
|---|---|
| `SPRINTCTL_INSTANCE_ID` | Stable per-process UUID — set once and reuse across every claim call |
| `SPRINTCTL_RUNTIME_SESSION_ID` | Runtime session ID (auto-detected from `CODEX_THREAD_ID`) |
| `SPRINTCTL_DB` | Override the database path |
