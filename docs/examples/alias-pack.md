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

## Reservation helpers (explicit handle retained)

```bash
# Start reservation and export handle var into the current shell
sreserve() {
  local item_id="$1"
  local actor="${2:-codex}"
  local reservation_json

  reservation_json=$(sprintctl reservation reserve \
    --item-id "$item_id" \
    --actor "$actor" \
    --role execution \
    --session-id "${SPRINTCTL_RUNTIME_SESSION_ID:-manual}" \
    --json) || return 1

  export RESERVATION_ID
  RESERVATION_ID=$(echo "$reservation_json" | jq -r '.id')

  echo "RESERVATION_ID=$RESERVATION_ID"
}

# Mark done using current reservation handle
sdone() {
  local item_id="$1"
  local actor="${2:-codex}"
  local rev
  rev=$(sprintctl item show --id "$item_id" --json | jq -r '.item.status_revision')
  sprintctl item status \
    --id "$item_id" --status done --actor "$actor" \
    --expected-revision "$rev"
  sprintctl reservation release \
    --id "${RESERVATION_ID:?missing RESERVATION_ID}" \
    --actor "$actor"
}

# Release current reservation
srelease() {
  local actor="${1:-codex}"
  sprintctl reservation release \
    --id "${RESERVATION_ID:?missing RESERVATION_ID}" \
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

- A reservation id is a handle, not a secret, but keep it scoped to the session.
- Prefer shell functions over opaque wrapper scripts so behavior stays visible.
- If a global binary is stale, pin aliases to `.venv/bin/python -m sprintctl`.
