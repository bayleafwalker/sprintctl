# sprintctl work loop

The canonical agent work loop: reserve an item, do the work, record notes,
reassign or release the reservation, and commit a snapshot. Every session
follows this shape regardless of how much work gets done.

---

## 1. Orient — read current state

```bash
# Compact one-shot context dump (designed for LLM prompt injection)
sprintctl usage --context [--json]

# Or: see what's unblocked and ready to pick up
sprintctl next-work

# Full sprint snapshot
sprintctl sprint show --detail

# Optional: keep a live status pane open during focused work
sprintctl sprint show --watch --detail --interval 30

# Optional: pick an item quickly with fzf
ITEM_ID=$(sprintctl item list --fzf | fzf | cut -f1 | tr -d '#')
sprintctl item show --id "$ITEM_ID"
```

`usage --context` is the fastest way to answer "where is the sprint right now?"
It surfaces active reservations, conflicts, ready/blocked/stale work, recent
decisions, and one explicit `next_action` in a single call.

### Shape completeness

Before reserving, inspect the selected item's refs. A shaped item has a governing
doc ref or an explicit `No doc:` decision. Read the referenced doc and, for
implementation against a ratified doc, attach a versioned label with the full
Git SHA as described in `docs/reference/doc-refs.md`.

---

## 2. Reserve — make coordination visible before editing files

```bash
# Create an advisory reservation on the item. Save the returned id.
RESERVATION=$(sprintctl reservation reserve \
  --item-id 7 --actor claude-session-1 \
  --role execute \
  --session-id "${CODEX_THREAD_ID:-manual}" \
  --json)

RESERVATION_ID=$(echo "$RESERVATION" | jq -r '.id')
```

`reservation_id` is the stable handle used in subsequent reservation calls.
There is no token or secret. The reservation is advisory: another session can
still create a reservation on the same item, and the overlap will be visible in
`usage --context` and `reservation list`.

### Coordinator + sub-agent pattern

```bash
# Coordinator reserves the item first
COORD=$(sprintctl reservation reserve \
  --item-id 7 --actor orchestrator \
  --role coordinate --json)

COORD_ID=$(echo "$COORD" | jq -r '.id')

# Sub-agents reserve execute roles under the coordinator
sprintctl reservation reserve \
  --item-id 7 --actor worker-a \
  --role execute \
  --session-id worker-a-session \
  --json
```

The coordinator role is metadata only; it does not grant an exclusivity
exception.

---

## 3. Touch — keep activity fresh during long tasks

```bash
# Bump activity on the reservation when useful; there is no lease or heartbeat
sprintctl reservation touch \
  --id "$RESERVATION_ID" \
  --session-id "${CODEX_THREAD_ID:-manual}"
```

Touch updates `last_activity_at`. Staleness is display-only; a long idle
reservation is not automatically invalidated.

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

# Attach the governing doc while shaping
sprintctl item ref add \
  --id 7 --type doc \
  --url docs/plans/auth.md \
  --label auth-plan

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
# Get the current status revision before mutating
REV=$(sprintctl item show --id 7 --json | jq -r '.item.status_revision')

# Mark done using the expected-revision CAS
sprintctl item status \
  --id 7 --status done \
  --actor claude-session-1 \
  --expected-revision "$REV"

# Release the advisory reservation
sprintctl reservation release --id "$RESERVATION_ID" --actor claude-session-1

# Commit a snapshot
sprintctl render > docs/sprint-snapshots/sprint-current.txt
git add docs/sprint-snapshots/sprint-current.txt
git commit -m "chore: sprint snapshot after completing auth item"
```

`item status` applies the transition through expected-revision compare-and-swap.
If the basis is stale, the command rejects without effect. Release the
reservation separately after the status change succeeds.

---

## 5b. Hand off to the next session (work continues)

```bash
# Reassign the advisory reservation to the next session
sprintctl reservation reassign \
  --id "$RESERVATION_ID" \
  --actor claude-session-2 \
  --session-id next-session \
  --json

# Write a sprint handoff bundle for the incoming session
sprintctl handoff --output handoff.json

# Or a human-readable version
sprintctl handoff --output - --format text
```

Pass `handoff.json` (or its text equivalent) as context to the next agent
session. The incoming session reads it as a working-memory snapshot, then calls
`usage --context` for the live view before continuing.

---

## 5c. Context loss recovery

If session state is lost, there is no token to recover:

```bash
# List active reservations
sprintctl reservation list --all --json

# Reassign an existing reservation to the current session
sprintctl reservation reassign \
  --id "$RESERVATION_ID" \
  --actor claude-session-1 \
  --session-id "${CODEX_THREAD_ID:-manual}" \
  --json
```

If the old reservation was released or interrupted, simply create a new one
with `sprintctl reservation reserve`.

---

## 6. Resume — incoming session orientation

```bash
# Read the handoff bundle (if one was written)
cat handoff.json | jq '.summary, .work, .next_action'

# Then get the live view
sprintctl usage --context --json

# Check for stale reservations or conflicted items
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

1. All active reservations: **reassign** (work continues) or **release** (work done)
2. `sprintctl handoff --output handoff.json` — write bundle for next session
3. `sprintctl render > docs/sprint-snapshots/sprint-current.txt` + commit snapshot
4. `sprintctl maintain check` — confirm no stale or conflicted items
5. Confirm the governing doc revision matches the work; use the read-only `reconcile-project-contracts` review for protocol or sprint-close changes
