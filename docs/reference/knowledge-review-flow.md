# Knowledge review flow

sprintctl records structured events during work. The companion tool
[kctl](https://github.com/bayleafwalker/kctl) reads these events and extracts
durable knowledge — decisions, patterns, lessons, and risks — into a separate
reviewed store. This document describes the full pipeline from event recording
through extraction, review, and export back to sprintctl.

sprintctl is the write side. kctl is the read/review/publish side. They share
a read path but not a write path — kctl never modifies the sprintctl database.

---

## Step 1 — Record knowledge-bearing events in sprintctl

During work, record events that kctl should pick up:

```bash
sprintctl item note \
  --id <item-id> \
  --type decision \
  --summary "Chose RS256 over HS256 for cross-service JWT verification" \
  --detail "HS256 requires symmetric key distribution; RS256 allows public-key verify" \
  --tags auth,security \
  --git-branch feat/auth --git-sha abc1234 \
  --evidence-item-id <related-item-id> \
  --actor claude-session-1
```

kctl recognizes these event types as knowledge candidates:

| Type | Meaning |
|------|---------|
| `decision` | Architectural or process decision with rationale |
| `pattern-noted` | Reusable pattern identified during work |
| `lesson-learned` | Retrospective insight — what would you do differently? |
| `risk-accepted` | Explicit risk acceptance with reasoning and owner |

Any event not in this set is ignored by kctl's extraction pipeline.

The `--git-branch`, `--git-sha`, `--evidence-item-id`, and `--evidence-event-id`
fields attach provenance so extracted knowledge entries carry their origin.

---

## Step 2 — Pre-flight check (kctl does this automatically)

Before extraction, kctl calls `sprintctl maintain check` as a pre-flight. This
ensures the sprint is not in a degraded state (stale active items, expired
claims) that would produce misleading knowledge candidates. If check reports
problems, resolve them first:

```bash
sprintctl maintain check [--sprint-id N]
sprintctl maintain sweep [--sprint-id N]   # if stale items need to be blocked
```

---

## Step 3 — Extract knowledge candidates

View candidates directly in sprintctl to understand what kctl will see:

```bash
# List knowledge-bearing events in the current sprint
sprintctl event list --sprint-id N --knowledge

# Or via usage context (surfaces the last 5 candidates)
sprintctl usage --context --json | jq '.recent_decisions'
```

kctl reads the same events from the sprintctl database (read-only) and produces
typed knowledge entries in its own store. Refer to the kctl documentation for
extraction invocation; the sprintctl side requires no action beyond recording
events with the types listed above.

---

## Step 4 — Review and approve in kctl

kctl's review workflow presents extracted entries for human or agent review.
Each entry is approved, edited, or discarded. This is entirely within kctl —
sprintctl is not involved.

Typical review actions in kctl:
- Approve with optional annotation
- Edit the summary or detail before approval
- Mark as superseded by a newer entry
- Discard (noisy signal, not durable knowledge)

---

## Step 5 — Seed approved knowledge back into a sprintctl backlog

After review, kctl can propose work derived from approved knowledge. sprintctl
accepts these proposals via `sprint backlog-seed`:

```bash
# Seed knowledge candidates from sprint 1 into a backlog sprint (sprint 2)
sprintctl sprint backlog-seed \
  --from-sprint-id 1 \
  --to-sprint-id 2 \
  --actor kctl-export

# The operation is idempotent — re-running is safe
sprintctl sprint backlog-seed \
  --from-sprint-id 1 \
  --to-sprint-id 2
```

Seeded items land in the `knowledge` track of the target sprint with titles
prefixed `[knowledge]`. Each item is linked to its source event via a
`backlog-seeded` event, making the provenance chain traceable.

---

## Step 6 — Render and commit a snapshot

After seeding, update the backlog sprint snapshot:

```bash
sprintctl render --sprint-id 2 > docs/sprint-snapshots/backlog.txt
git add docs/sprint-snapshots/backlog.txt
git commit -m "chore: backlog snapshot after knowledge seed"
```

---

## Pipeline summary

```
sprintctl events
  (decision, pattern-noted, lesson-learned, risk-accepted)
        │
        │  kctl reads (never writes)
        ▼
kctl extract
        │
        ▼
kctl review  ──── approve / edit / discard
        │
        │  kctl produces proposed items
        ▼
sprintctl sprint backlog-seed
        │
        ▼
sprintctl backlog sprint
  (knowledge track items, traceable to source events)
        │
        ▼
sprintctl render → committed snapshot
```

---

## Current-state vs source-of-truth

The sprintctl database is live state — always current, never committed.

The committed `sprintctl render` output is the reviewable, portable snapshot —
what you share, diff, and review in PRs.

kctl's own store is the reviewed knowledge record — separate from sprintctl,
owned by kctl.

When these sources disagree:

1. `sprintctl` live DB — authoritative for item status, claims, recent events
2. committed `sprintctl render` output — authoritative for sprint snapshots
3. kctl store — authoritative for reviewed, approved knowledge entries
4. repo docs — reference only; may lag behind live state
