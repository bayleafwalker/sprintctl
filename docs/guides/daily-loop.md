# Daily Loop

Use this guide when you already know the protocol and want the fastest
repeatable in-flow pattern during active coding.

If your global `sprintctl` install is missing expected flags, run commands via
`.venv/bin/python -m sprintctl ...` from this repository.

Before long sessions, refresh local tools (`sprintctl` + `kctl`) with either
`pipx upgrade sprintctl && pipx upgrade kctl` or
`uv tool upgrade sprintctl kctl`.

## 1. Start of session: load context

```bash
sprintctl usage --context --json
sprintctl next-work --json --explain
sprintctl git-context --json
```

Use these as one bundle so decisions stay tied to current sprint state and
current git state.

## 2. Reservation-aware execution loop

```bash
RESERVATION_JSON=$(sprintctl reservation reserve \
  --item-id 42 \
  --actor codex \
  --role execute \
  --session-id "${SPRINTCTL_RUNTIME_SESSION_ID:-manual-session}" \
  --json)

RESERVATION_ID=$(echo "$RESERVATION_JSON" | jq -r '.id')
```

During work, touch activity when useful:

```bash
sprintctl reservation touch \
  --id "$RESERVATION_ID" \
  --session-id "${SPRINTCTL_RUNTIME_SESSION_ID:-manual-session}"
```

## 3. Capture durable notes while coding

Use notes for information the next session should not rediscover:

```bash
sprintctl item note \
  --id 42 \
  --type decision \
  --summary "Moved stale-reservation cleanup behind maintain sweep --force-close-overdue" \
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
REV=$(sprintctl item show --id 42 --json | jq -r '.item.status_revision')
sprintctl item status --id 42 --status done --actor codex --expected-revision "$REV"
sprintctl reservation release --id "$RESERVATION_ID" --actor codex
```

When work continues in the next session:

```bash
sprintctl reservation reassign \
  --id "$RESERVATION_ID" \
  --actor codex-next \
  --session-id next-session \
  --json

sprintctl handoff --format json --output handoff.json
```

## 5. Working-speed overlays

Use these examples to reduce repetition without changing `sprintctl` semantics:

- [alias-pack.md](../examples/alias-pack.md)
- [agent-prompt-snippets.md](../examples/agent-prompt-snippets.md)
- [editor-and-terminal-integration.md](../examples/editor-and-terminal-integration.md)
