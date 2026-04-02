# Daily Loop

Use this guide when you already know the protocol and want the fastest
repeatable in-flow pattern during active coding.

If your global `sprintctl` install is missing expected flags, run commands via
`.venv/bin/python -m sprintctl ...` from this repository.

## 1. Start of session: load context

```bash
sprintctl usage --context --json
sprintctl next-work --json --explain
sprintctl git-context --json
```

Use these as one bundle so decisions stay tied to current sprint state and
current git state.

## 2. Claim-safe execution loop

```bash
CLAIM_JSON=$(sprintctl claim start \
  --item-id 42 \
  --actor codex \
  --ttl 900 \
  --instance-id "${SPRINTCTL_INSTANCE_ID:-manual-instance}" \
  --runtime-session-id "${SPRINTCTL_RUNTIME_SESSION_ID:-manual-session}" \
  --json)

CLAIM_ID=$(echo "$CLAIM_JSON" | jq -r '.claim_id')
CLAIM_TOKEN=$(echo "$CLAIM_JSON" | jq -r '.claim_token')
```

During work, heartbeat at roughly half-TTL:

```bash
sprintctl claim heartbeat \
  --id "$CLAIM_ID" \
  --claim-token "$CLAIM_TOKEN" \
  --ttl 900 \
  --actor codex
```

## 3. Capture durable notes while coding

Use notes for information the next session should not rediscover:

```bash
sprintctl item note \
  --id 42 \
  --type decision \
  --summary "Moved stale-claim cleanup behind maintain sweep --force-close-overdue" \
  --git-branch "$(git rev-parse --abbrev-ref HEAD)" \
  --git-sha "$(git rev-parse --short HEAD)" \
  --actor codex
```

Recommended `--type` guidance:

- `decision` for architecture, tradeoff, or contract choices
- `blocker` for external dependency or unresolved risk
- `lesson-learned` for implementation pitfalls worth reusing
- `pattern-noted` for repeatable workflow patterns

## 4. Complete or hand off cleanly

When done:

```bash
sprintctl item done-from-claim \
  --id 42 \
  --claim-id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN" \
  --actor codex
```

If release fails, this command exits non-zero and reports `release_error`; the
item may still be marked `done`.

When work continues in the next session:

```bash
sprintctl claim handoff \
  --id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN" \
  --actor codex-next \
  --mode rotate \
  --runtime-session-id next-session \
  --json

sprintctl handoff --format json --output handoff.json
```

## 5. Working-speed overlays

Use these examples to reduce repetition without changing `sprintctl` semantics:

- [alias-pack.md](../examples/alias-pack.md)
- [agent-prompt-snippets.md](../examples/agent-prompt-snippets.md)
- [editor-and-terminal-integration.md](../examples/editor-and-terminal-integration.md)
