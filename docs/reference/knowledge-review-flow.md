# Knowledge review flow

sprintctl records structured events during work. The companion tool
[kctl](https://github.com/bayleafwalker/kctl) reads these events and extracts
durable knowledge — decisions, patterns, lessons, and risks — into a separate
reviewed store. It also preserves handoff and ownership events in a separate
coordination review stream. This document describes the full pipeline from event
recording through extraction, review, and use of those artifacts when deciding
later sprintctl actions.

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

kctl recognizes these durable knowledge event types:

| Type | Meaning |
|------|---------|
| `decision` | Architectural or process decision with rationale |
| `pattern-noted` | Reusable pattern identified during work |
| `lesson-learned` | Retrospective insight — what would you do differently? |
| `risk-accepted` | Explicit risk acceptance with reasoning and owner |

These coordination event types are also extracted by kctl, but they stay in a
separate non-publishable review stream:

| Type | Meaning |
|------|---------|
| `claim-handoff` | Claim ownership changed intentionally between sessions |
| `claim-ownership-corrected` | Legacy or ambiguous ownership was repaired |
| `claim-ambiguity-detected` | Ownership proof was unclear or insufficient |
| `coordination-failure` | A claim or ownership rule blocked an attempted action |

Events outside these sets are ignored by kctl's default extraction pipeline
unless kctl is configured with a custom event-type filter.

The `--git-branch`, `--git-sha`, `--evidence-item-id`, and `--evidence-event-id`
fields attach provenance so extracted knowledge entries carry their origin.

---

## Step 2 — Pre-flight check (kctl does this automatically)

Before extraction, kctl follows sprintctl's own `maintain check` semantics as a
pre-flight. This ensures the sprint is not in a degraded state that would
produce misleading knowledge candidates. If check reports problems, resolve them
first:

```bash
sprintctl maintain check [--sprint-id N]
sprintctl maintain sweep [--sprint-id N]   # if stale items need to be blocked
```

---

## Step 3 — Extract knowledge candidates

View candidates directly in sprintctl to understand what kctl will see:

```bash
# List durable knowledge-bearing events in the current sprint
sprintctl event list --sprint-id N --knowledge

# Or via usage context (surfaces the last 5 durable candidates)
sprintctl usage --context --json | jq '.recent_decisions'
```

kctl reads the same events from the sprintctl database in read-only mode and
produces candidates in its own store. Durable and coordination items share the
same extraction path, but are reviewed separately on the kctl side. The
sprintctl side requires no action beyond recording events with the types listed
above.

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

Coordination items are reviewable in kctl too, but they are intentionally not
publishable into the durable knowledge base.

---

## Step 5 — Use kctl artifacts when deciding sprintctl backlog actions

kctl never writes back into sprintctl. sprintctl remains the only tool that owns
backlog and sprint mutation.

After review, agents or operators can use kctl artifacts to decide which
sprintctl actions to run next. There are two common paths:

1. Use kctl's reviewed outputs (`kctl status --json`, `kctl review list --json`,
   `kctl render`) to decide what items or sprint changes to create explicitly in
   sprintctl.
2. Use sprintctl's own raw-event-driven seeding command when you want backlog
   items directly from sprintctl's durable event stream:

```bash
# Seed sprintctl's durable event candidates from sprint 1 into sprint 2
sprintctl sprint backlog-seed \
  --from-sprint-id 1 \
  --to-sprint-id 2 \
  --actor operator

# The operation is idempotent — re-running is safe
sprintctl sprint backlog-seed \
  --from-sprint-id 1 \
  --to-sprint-id 2
```

`sprint backlog-seed` is owned entirely by sprintctl. It seeds from sprintctl's
durable knowledge event set, not from kctl's reviewed store. kctl artifacts can
inform the decision to run this command, but kctl never invokes it or mutates
sprint state on its own.

---

## Step 6 — Render and commit a snapshot

After backlog changes, update the sprint snapshot:

```bash
sprintctl render --sprint-id 2 > docs/sprint-snapshots/backlog.txt
git add docs/sprint-snapshots/backlog.txt
git commit -m "chore: backlog snapshot after knowledge seed"
```

---

## Pipeline summary

```
sprintctl events
  durable + coordination signals
        │
        │  kctl reads (never writes)
        ▼
kctl extract
        │
        ├── durable review -> publish -> render knowledge.md
        │
        └── coordination review -> JSON / audit context
        │
        ▼
agents decide sprintctl commands
        │
        ├── explicit item / sprint mutations
        └── optional sprint backlog-seed
        │
        ▼
sprintctl render -> committed snapshot
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
