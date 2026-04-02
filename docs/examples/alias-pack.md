# Alias Pack

These aliases/functions speed up common `sprintctl` actions while keeping
explicit protocol semantics and inspectable output.

Use them as local shell glue, not as hidden behavior in the core binary.

## Bash/Zsh functions

```bash
# Context bundle (human + machine friendly)
sctx() {
  sprintctl usage --context
  echo
  sprintctl next-work --explain
  echo
  sprintctl git-context --json
}

# Machine bundle for agent prompts
sctxj() {
  sprintctl usage --context --json
  sprintctl next-work --json --explain
  sprintctl git-context --json
}

# Quick snapshot and checkpoint commit
ssnap() {
  sprintctl render > docs/sprint-snapshots/current.txt
  git add docs/sprint-snapshots/current.txt
  git commit -m "${1:-chore: sprint snapshot}"
}

# Write both handoff formats
shandoff() {
  sprintctl handoff --format json --output handoff.json
  sprintctl handoff --format text
}
```

## Claim helpers (explicit proof retained)

```bash
# Start claim and export proof vars into the current shell
sclaim() {
  local item_id="$1"
  local actor="${2:-codex}"
  local claim_json

  claim_json=$(sprintctl claim start \
    --item-id "$item_id" \
    --actor "$actor" \
    --ttl 900 \
    --instance-id "${SPRINTCTL_INSTANCE_ID:?set SPRINTCTL_INSTANCE_ID}" \
    --runtime-session-id "${SPRINTCTL_RUNTIME_SESSION_ID:-manual}" \
    --json) || return 1

  export CLAIM_ID
  CLAIM_ID=$(echo "$claim_json" | jq -r '.claim_id')
  export CLAIM_TOKEN
  CLAIM_TOKEN=$(echo "$claim_json" | jq -r '.claim_token')

  echo "CLAIM_ID=$CLAIM_ID"
}

# Mark done using current proof vars
sdone() {
  local item_id="$1"
  local actor="${2:-codex}"
  sprintctl item status \
    --id "$item_id" --status done --actor "$actor" \
    --claim-id "${CLAIM_ID:?missing CLAIM_ID}" \
    --claim-token "${CLAIM_TOKEN:?missing CLAIM_TOKEN}"
}

# Release current claim
srelease() {
  local actor="${1:-codex}"
  sprintctl claim release \
    --id "${CLAIM_ID:?missing CLAIM_ID}" \
    --claim-token "${CLAIM_TOKEN:?missing CLAIM_TOKEN}" \
    --actor "$actor"
}
```

## Minimal alias-only mode

```bash
alias sn='sprintctl next-work'
alias snx='sprintctl next-work --explain'
alias su='sprintctl usage --context'
alias suj='sprintctl usage --context --json'
alias sg='sprintctl git-context --json'
```

## Notes

- Keep `CLAIM_TOKEN` private. Do not paste it into chat logs.
- Prefer shell functions over opaque wrapper scripts so behavior stays visible.
- If a global binary is stale, pin aliases to `.venv/bin/python -m sprintctl`.

