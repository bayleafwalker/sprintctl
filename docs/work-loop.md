# sprintctl work loop

The canonical agent work loop: claim an item, do the work, record notes, hand
off or release the claim, and commit a snapshot.  Every session follows this
shape regardless of how much work gets done.

---

## 1. Orient — read current state

```bash
# Compact one-shot context dump (designed for LLM prompt injection)
sprintctl usage --context [--json]

# Or: see what's unblocked and ready to pick up
sprintctl next-work

# Full sprint snapshot
sprintctl sprint show --detail
```

`usage --context` is the fastest way to answer "where is the sprint right now?"
It surfaces active claims, stale/blocked items, ready-to-start items, and recent
knowledge candidates in a single call.

---

## 2. Claim — establish ownership before editing files

```bash
# Claim an item exclusively; save both values for the entire session
CLAIM=$(sprintctl claim create \
  --item-id 7 --actor claude-session-1 \
  --type execute --ttl 900 \
  --branch feat/auth \
  --runtime-session-id "${CODEX_THREAD_ID:-manual}" \
  --instance-id "${SPRINTCTL_INSTANCE_ID:-proc-1}" \
  --json)

CLAIM_ID=$(echo "$CLAIM" | jq -r '.claim_id')
CLAIM_TOKEN=$(echo "$CLAIM" | jq -r '.claim_token')

# Transition the item to active using the claim as proof
sprintctl item status \
  --id 7 --status active --actor claude-session-1 \
  --claim-id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN"
```

`claim_token` is a secret — store it for the entire session and never share it.
`claim_id` is the stable handle used in every subsequent call.

### Coordinator + sub-agent pattern

```bash
# Coordinator claims the item first
COORD=$(sprintctl claim create \
  --item-id 7 --actor orchestrator \
  --type coordinate --ttl 1800 --json)

COORD_ID=$(echo "$COORD" | jq -r '.claim_id')
COORD_TOKEN=$(echo "$COORD" | jq -r '.claim_token')

# Sub-agents acquire execute claims under the coordinator — no ClaimConflict
sprintctl claim create \
  --item-id 7 --actor worker-a \
  --type execute --ttl 600 \
  --coordinate-claim-id "$COORD_ID" \
  --coordinate-claim-token "$COORD_TOKEN" \
  --json
```

---

## 3. Heartbeat — keep the claim alive during long tasks

```bash
# Refresh the claim every ~half-TTL while work is in progress
sprintctl claim heartbeat \
  --id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN" \
  --ttl 900 --actor claude-session-1

# Update git context on the claim as work progresses
sprintctl claim heartbeat \
  --id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN" \
  --branch feat/auth --commit-sha abc1234 \
  --actor claude-session-1
```

---

## 4. Note — record decisions, blockers, and patterns during work

```bash
# Record a decision (picked up by kctl for knowledge extraction)
sprintctl item note \
  --id 7 --type decision \
  --summary "Using RS256 JWT; symmetric keys ruled out for cross-service use" \
  --detail "HS256 requires shared secret distribution; RS256 allows public-key verification" \
  --tags auth,security \
  --git-branch feat/auth --git-sha abc1234 \
  --actor claude-session-1

# Record a blocker
sprintctl item note \
  --id 7 --type blocker \
  --summary "Blocked on infra team rotating the signing key" \
  --actor claude-session-1

# Attach a PR or issue ref
sprintctl item ref add \
  --id 7 --type pr \
  --url https://github.com/org/repo/pull/42 \
  --label "Auth implementation PR"

# Declare a dependency (item 7 cannot proceed until item 3 is done)
sprintctl item dep add --id 3 --blocks-item-id 7
```

Knowledge-bearing event types (`decision`, `pattern-noted`, `lesson-learned`,
`risk-accepted`) are recognized by kctl for extraction into the knowledge store.

---

## 5a. Complete the item

```bash
# Mark done — requires claim proof
sprintctl item status \
  --id 7 --status done --actor claude-session-1 \
  --claim-id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN"

# Release the claim
sprintctl claim release \
  --id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN" \
  --actor claude-session-1

# Commit a snapshot
sprintctl render > docs/sprint-snapshots/sprint-current.txt
git add docs/sprint-snapshots/sprint-current.txt
git commit -m "chore: sprint snapshot after completing auth item"
```

---

## 5b. Hand off to the next session (work continues)

```bash
# Rotate the claim token to the next session
HANDOFF=$(sprintctl claim handoff \
  --id "$CLAIM_ID" --claim-token "$CLAIM_TOKEN" \
  --actor claude-session-2 \
  --mode rotate \
  --runtime-session-id "${NEXT_SESSION_ID:-next}" \
  --json)

# Save the new token — the old one is now invalid
NEW_TOKEN=$(echo "$HANDOFF" | jq -r '.claim_token')

# Write a sprint handoff bundle for the incoming session
sprintctl handoff --output handoff.json

# Or a human-readable version
sprintctl handoff --output - --format text
```

Pass `handoff.json` (or its text equivalent) as context to the next agent
session.  The incoming session reads it to understand item status, claim
ownership, and recent decisions — then calls `usage --context` for a live view.

---

## 5c. Context loss recovery (token missing after restart)

```bash
# Find claims by advisory identity
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json

# Re-adopt the claim and get a fresh token
sprintctl claim handoff \
  --id "$CLAIM_ID" \
  --actor claude-session-1 \
  --mode rotate \
  --allow-legacy-adopt \
  --json
```

---

## 6. Resume — incoming session orientation

```bash
# Read the handoff bundle (if one was written)
cat handoff.json | jq '.items[] | select(.status == "active")'

# Then get the live view
sprintctl usage --context --json

# Check for stale or expired claims
sprintctl maintain check

# Get git context
sprintctl git-context
```

---

## Snapshot cadence

Commit a `render` output at natural checkpoints:

```bash
sprintctl render > docs/sprint-snapshots/sprint-current.txt
git add docs/sprint-snapshots/sprint-current.txt
git commit -m "chore: sprint snapshot"
```

The committed snapshot is the reviewable, diffable record of sprint state.
The SQLite database is live state only — it belongs in `.gitignore`.

---

## Checklist before session end

1. All owned claims: **handoff** (work continues) or **release** (work done)
2. `sprintctl handoff --output handoff.json` — write bundle for next session
3. `sprintctl render > docs/sprint-snapshots/sprint-current.txt` + commit snapshot
4. `sprintctl maintain check` — confirm no stale or conflicted items
