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
  --role execution \
  --session-id "${CODEX_THREAD_ID:-manual}" \
  --json)

RESERVATION_ID=$(echo "$RESERVATION" | jq -r '.id')
```

`reservation_id` is the stable handle used in subsequent reservation calls.
There is no token or secret. The reservation is advisory: another session can
still create a reservation on the same item, and the overlap will be visible in
`usage --context` and `reservation list`.

### Mark delivering commits with `Vuoro-Release`

An execution reservation freezes the item's current release. Every commit
that delivers that release carries its digest as a git trailer, on its own
line in the trailer block at the end of the commit message:

```
feat(auth): rotate session keys

Vuoro-Release: <64-hex digest>
```

A `sha256:` prefix on the digest is also accepted. To find the digest:

- `sprintctl reservation reserve` prints `commit trailer: Vuoro-Release: <digest>`
  (JSON: `.release_digest`);
- `sprintctl item show --id 7` lists it as `release=<digest>` under the
  active execution reservation (JSON: `.active_reservations[].release_digest`);
- the served `work.read.release` operation returns the item's current release.

A commit may carry several `Vuoro-Release:` trailers when it delivers several
releases. `sprintctl sync` and `sprintctl authority sync` harvest the trailers
(see [normal synchronization](normal-sync.md#vuoro-release-trailer-harvest)).
Malformed values are reported and skipped, never uploaded.

### Coordinator + sub-agent pattern

```bash
# Coordinator reserves the item first
COORD=$(sprintctl reservation reserve \
  --item-id 7 --actor orchestrator \
  --role observation --json)

COORD_ID=$(echo "$COORD" | jq -r '.id')

# Sub-agents reserve execution roles under the coordinator
sprintctl reservation reserve \
  --item-id 7 --actor worker-a \
  --role execution \
  --session-id worker-a-session \
  --json
```

The coordinator role is metadata only; it does not grant an exclusivity
exception. Nothing does: a second `reserve` on the same item always succeeds
and reports the conflict, and displacing an execution reservation takes an
explicit `--interrupt-existing`.

---

## 3. Touch — keep activity fresh during long tasks

```bash
# Activity advances by itself when your session mutates the item; touch is for
# work happening outside sprintctl. There is no lease or heartbeat.
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
# Closing an item is a decision: accept makes it done (resolution "accepted")
sprintctl item decide \
  --id 7 --kind accept \
  --rationale "Auth flow verified; merged in #123" \
  --actor claude-session-1

# Release the advisory reservation
sprintctl reservation release --id "$RESERVATION_ID" --actor claude-session-1

# Commit a snapshot
sprintctl render > docs/sprint-snapshots/sprint-current.txt
git add docs/sprint-snapshots/sprint-current.txt
git commit -m "chore: sprint snapshot after completing auth item"
```

Release the reservation separately after the decision is recorded. See
[Deciding items](#deciding-items) for the other decision kinds.

### Deciding items

A work item becomes terminal only by recording a decision. `item decide`
records one; the decision row is append-only and the item points at it
(`terminal_decision_id`) with a `resolution` derived from its kind.

| `--kind` | Item afterwards | Notes |
| --- | --- | --- |
| `accept` | `done`, resolution `accepted` | Requires an `active` item. |
| `reject` | `done`, resolution `rejected` | Any open item. |
| `withdraw` | `done`, resolution `withdrawn` | Any open item. |
| `supersede` | `done`, resolution `superseded` | Requires `--superseded-by <item>`. |
| `revise` | unchanged (stays open) | Retires the current release; the next execution reservation freezes a new one. |

```bash
sprintctl item decide --id 7 --kind accept --rationale "Verified" \
  --evidence <sha256-hex> [--evidence ...] \
  [--release <release-digest>] [--json]
sprintctl item decide --id 7 --kind supersede --superseded-by 9 --rationale "Folded into #9"
sprintctl item decide --id 7 --kind revise --rationale "Review asked for an API change"
```

- `--rationale` is required; `--evidence` takes 64-character lowercase hex
  SHA-256 digests and may repeat.
- A decision binds the item's current release unless `--release` names one;
  a digest that is not a release of that item is refused.
- A terminal item takes no further decision.
- An item that was already done before decisions existed (`legacy`, shown as
  `Resolution: - (legacy ...)`) takes exactly one terminal decision that says
  what that closure really was: a *re-mark*. It needs a terminal `--kind`,
  a `--rationale` and at least one `--evidence` digest; the item stays done
  and keeps its original `updated_at`. Once recorded it is immutable like any
  terminal decision.
- `sprintctl item unbound [--sprint-id N] [--category C] [--json]` lists
  what is not bound to a decision: `legacy_done` (legacy done items with no
  decision -- re-mark candidates), `decided_unreleased` (closed by a decision
  that names no release; legacy rows excluded) and `released_undecided`
  (open items whose frozen current release still awaits a decision), and
  counts done items by resolution with legacy done kept apart and re-marked
  legacy items counted as `legacy_remarked`. Served operation:
  `work.read.unbound`, on both backends. Alongside the categories, an
  `unmet_obligations` report (#2446) names every accept Decision on a
  release whose `acceptance_contract` declared an `evidence_obligations`
  list -- labels naming the evidence it owes, e.g. `["test-run", "review"]`
  -- and carries no matching evidence. Evidence digests carry no `kind` in
  the current schema, so any evidence digest on the Decision satisfies every
  declared label; it is not filtered by `--category`, and it only reports --
  nothing consults it to gate or block.
- A generic event or note cannot pose as a decision: `event add` and
  `item note` refuse decision-like types (`item.done`, `item-decided`,
  `accept`, `rejected`, `decision.record`, ...) with
  `decision-like-event-type`. The knowledge note type `decision` stays open.
- In served mode the decision actor is the authenticated identity; `--actor`
  applies to direct backends only and is otherwise ignored with a note. The
  served operation is `work.decision.record`; a retry with the same
  idempotency key returns the first decision instead of recording another.
- `item show` prints the item's resolution and terminal decision
  (`terminal_decision` in `--json`).
- `item status --status done` still works: it is an alias for an `accept`
  decision with no rationale, and still requires `--expected-revision` on a
  direct backend.

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
