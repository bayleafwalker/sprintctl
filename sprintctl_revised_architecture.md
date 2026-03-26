# sprintctl — Revised Architecture

## Summary of changes from original spec

| Original                          | Revised                                                        |
|-----------------------------------|----------------------------------------------------------------|
| Phase 2 daemon (background process) | **Calculate-on-call**: staleness, derived state computed at read time |
| Claims as Phase 2 active scope    | **Claims retained in schema, inactive** — tables present, enforcement deferred |
| Phase 3 knowledge promotion in-tree | **Separate companion tool** (`kctl` or similar) that reads sprintctl DB |
| Sweeper as daemon responsibility  | **Explicit maintenance commands** (`sprintctl maintain`) callable ad-hoc or via cron |

---

## 1. Calculate-on-call model

### Principle

No background process. All derived state — staleness, track health, stale claims — is computed when requested. The database stores facts (timestamps, statuses). Interpretation of those facts happens at query time.

### What gets computed

| Computed property        | Inputs                              | Where it surfaces                  |
|--------------------------|-------------------------------------|------------------------------------|
| Item staleness           | `work_item.updated_at` vs now, configurable threshold | `item list`, `render`, `maintain check` |
| Track health             | Distribution of item statuses per track | `render`, `sprint show --detail`   |
| Sprint overrun risk      | `sprint.end_date` vs now, active item count | `sprint show`, `render`            |
| Claim expiry (future)    | `claim.expires_at` vs now           | `item show`, `render`              |

### Implementation approach

Add a `sprintctl/calc.py` module — pure functions that accept DB state and `now` as arguments (no side effects, fully testable like `render.py`).

```python
# calc.py — all functions are pure, receive `now` explicitly

from datetime import datetime, timedelta

DEFAULT_STALE_THRESHOLD = timedelta(hours=4)

def item_staleness(item: dict, now: datetime, threshold: timedelta = DEFAULT_STALE_THRESHOLD) -> dict:
    """Returns staleness info for a single work item."""
    updated = datetime.fromisoformat(item["updated_at"])
    delta = now - updated
    is_stale = item["status"] in ("pending", "active") and delta > threshold
    return {
        "item_id": item["id"],
        "idle_seconds": int(delta.total_seconds()),
        "is_stale": is_stale,
        "status": item["status"],
    }

def track_health(items: list[dict]) -> dict:
    """Summarise status distribution for a track."""
    counts = {"pending": 0, "active": 0, "done": 0, "blocked": 0}
    for it in items:
        counts[it["status"]] += 1
    total = len(items)
    return {
        "total": total,
        "counts": counts,
        "blocked_ratio": counts["blocked"] / total if total else 0.0,
        "done_ratio": counts["done"] / total if total else 0.0,
    }

def sprint_overrun_risk(sprint: dict, active_items: int, now: datetime) -> dict:
    """Flag if sprint is approaching end with significant open work."""
    end = datetime.fromisoformat(sprint["end_date"])
    remaining = end - now
    return {
        "days_remaining": remaining.days,
        "active_items": active_items,
        "at_risk": remaining.days <= 2 and active_items > 0,
        "overdue": remaining.days < 0 and sprint["status"] == "active",
    }
```

These functions are called by the CLI layer and the render layer — never by db.py. The render output gains staleness annotations and health summaries without any schema change.

---

## 2. Maintenance commands (`sprintctl maintain`)

### Purpose

Batch operations that mutate state based on computed conditions. Unlike calculate-on-call (which is read-only), maintenance commands *write*. They are the replacement for daemon sweep loops.

### Command surface

```
sprintctl maintain check     — dry-run: report what would change (stale items, expired claims, overdue sprints)
sprintctl maintain sweep      — execute: transition stale items, expire claims, emit system events
sprintctl maintain carryover  — at sprint close: move incomplete items to next sprint
```

### `maintain check` (read-only)

Runs all calc functions against the active sprint, prints a diagnostic report. Safe to run anytime. Example output:

```
Sprint #1: "Sprint 1" — 2 days remaining, 4 active items (AT RISK)

Stale items (threshold: 4h):
  #3  [active  ] Implement auth — idle 6h12m (track: backend)
  #7  [pending ] Write tests    — idle 11h3m (track: backend)

Track health:
  backend:  4 items — 1 done, 2 active, 1 blocked (25% blocked)
  frontend: 3 items — 2 done, 1 active (67% done)

No expired claims.
```

### `maintain sweep` (mutating)

Actions performed:

1. **Stale active items → blocked**: Items in `active` status idle beyond threshold are transitioned to `blocked` with a system event recording the reason.
2. **Expire claims** (future, when claims are activated): Claims past `expires_at` are released, system event emitted.
3. **Auto-close overdue sprints** (optional, behind `--auto-close` flag): Sprints past `end_date` with no active items are transitioned to `closed`.

Every mutation emits an event with `source_type = 'system'` and `actor = 'maintain-sweep'`. This preserves the event sourcing model.

```python
# In db.py or a new maintain.py that calls db functions

def sweep_stale_items(conn, sprint_id: int, now: datetime, threshold: timedelta) -> list[dict]:
    """Transition stale active items to blocked. Returns list of affected items."""
    items = list_work_items(conn, sprint_id=sprint_id, status="active")
    affected = []
    for item in items:
        info = calc.item_staleness(item, now, threshold)
        if info["is_stale"]:
            set_work_item_status(conn, item["id"], "blocked")
            create_event(
                conn, sprint_id,
                actor="maintain-sweep",
                event_type="auto-blocked-stale",
                source_type="system",
                work_item_id=item["id"],
                payload={"idle_seconds": info["idle_seconds"], "threshold_seconds": int(threshold.total_seconds())},
            )
            affected.append(item)
    return affected
```

### `maintain carryover`

Runs at sprint boundary. Requires a target sprint to exist.

```
sprintctl maintain carryover --from-sprint 1 --to-sprint 2
```

Moves items in `pending`, `active`, or `blocked` status from source sprint to target sprint. Emits `carryover` events on both sprints. Original items are marked `done` with a payload noting the carryover destination. New items are created in the target sprint preserving track and title.

### Scheduling

These are regular CLI commands. Users can schedule them externally:

```sh
# cron example
*/30 * * * * sprintctl maintain sweep --threshold 4h 2>&1 | logger -t sprintctl
```

No PID files, no daemon lifecycle, no crash recovery. If a run fails, the next cron invocation retries. Idempotent by design (items already blocked won't re-block).

---

## 3. Claims — inactive scope, schema present

### Rationale

The claim model is useful for autonomous agent swarms, but premature to enforce now. The schema should exist so that:
- Migration numbering isn't disrupted later
- Tools reading the DB can discover the table
- The companion tool can reference claims if needed

### Schema addition (migration 2, inactive)

```sql
CREATE TABLE IF NOT EXISTS claim (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
    agent       TEXT    NOT NULL,
    claim_type  TEXT    NOT NULL DEFAULT 'execute'
                        CHECK (claim_type IN ('inspect', 'execute', 'review', 'coordinate')),
    exclusive   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at  TEXT    NOT NULL,
    heartbeat   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
```

### What "inactive" means

- Table is created by migration but no CLI commands expose it
- No claim enforcement in `item status` transitions
- `calc.py` includes claim expiry logic (ready for when it's wired up)
- `maintain sweep` includes claim expiry path (no-op while table is empty)
- `render` does not surface claims yet

### Activation path (future)

When claims are activated:
1. Add `claim create`, `claim heartbeat`, `claim release` CLI commands
2. Wire `item status` to check for active exclusive claim before allowing transition
3. Enable claim rendering in `render`
4. `maintain sweep` starts expiring claims for real

---

## 4. Transition enforcement — moved to db.py

### Current problem

`set_work_item_status` in db.py blindly writes. Transition rules live only in cli.py. This breaks the spec's own principle: "no hard process relies on agent goodwill."

### Fix

```python
# db.py

class InvalidTransition(ValueError):
    pass

def set_work_item_status(conn: sqlite3.Connection, item_id: int, new_status: str) -> None:
    item = get_work_item(conn, item_id)
    if item is None:
        raise ValueError(f"Item #{item_id} not found")
    current = item["status"]
    if new_status not in VALID_TRANSITIONS[current]:
        raise InvalidTransition(
            f"Cannot transition {current} -> {new_status}. "
            f"Allowed: {sorted(VALID_TRANSITIONS[current]) or 'none (terminal)'}"
        )
    conn.execute(
        "UPDATE work_item SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (new_status, item_id),
    )
    conn.commit()
```

CLI catches `InvalidTransition` and exits with error. But now `maintain sweep`, the future API wrapper, and any direct db.py caller all get enforcement for free.

### Sprint status transitions

Add equivalent enforcement:

```python
SPRINT_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"active"},
    "active": {"closed"},
    "closed": set(),
}
```

Applied in a `set_sprint_status()` function. `create_sprint` still accepts an initial status (for bootstrap convenience), but subsequent changes go through the enforced path.

---

## 5. Companion tool interface contract

### The problem Phase 3 solves

Durable knowledge (patterns learned, decisions made, reusable context) gets trapped in sprint events and never surfaces. A separate tool extracts and promotes it.

### Boundary

sprintctl owns sprint state. The companion tool ([kctl](https://github.com/bayleafwalker/kctl)) owns knowledge state. They share a read path but not a write path.

```
┌─────────────┐         ┌─────────────┐
│  sprintctl   │         │    kctl      │
│              │  reads  │              │
│  sprint.db   │◄────────│  knowledge   │
│  (sprints,   │         │  candidates  │
│   items,     │         │  approved    │
│   events)    │         │  published   │
│              │         │              │
│  maintain ◄──┼─────────│  on invoke,  │
│  sweep/check │ triggers│  calls       │
│              │         │  maintain    │
└─────────────┘         └─────────────┘
```

### What sprintctl provides for kctl

1. **Event stream as knowledge source**: `list_events()` already returns all events. kctl scans for event types that indicate knowledge (decisions, blockers resolved, architecture notes).

2. **Maintain trigger hook**: When kctl runs, it first calls `sprintctl maintain check` (or imports the function) to ensure sprint state is fresh. This prevents knowledge extraction from stale data.

3. **Event type conventions**: sprintctl should define a set of event types that kctl recognizes as knowledge-bearing:

```python
# Conventional event types that signal extractable knowledge
KNOWLEDGE_BEARING_EVENTS = {
    "decision",           # architectural or process decision
    "blocker-resolved",   # how a blocker was resolved
    "pattern-noted",      # reusable pattern identified
    "risk-accepted",      # explicit risk acceptance with reasoning
    "lesson-learned",     # retrospective insight
}
```

These aren't enforced by sprintctl (event_type is freeform TEXT), but documented as the contract.

4. **Structured payload convention**: For knowledge-bearing events, the payload should follow a loose schema:

```json
{
  "summary": "one-line description",
  "detail": "longer explanation",
  "tags": ["auth", "architecture"],
  "confidence": "high"
}
```

Again, not enforced — just documented convention that kctl can rely on.

### What kctl does NOT do

- Never writes to sprintctl's DB
- Never transitions item or sprint status
- Never creates claims

### Maintain as pre-flight

kctl's invocation pattern:

```sh
kctl extract --sprint-id 1
# internally: calls `sprintctl maintain check` first
# then: scans events, proposes knowledge candidates
# stores candidates in its own DB/store
```

This ensures that stale items are flagged before knowledge extraction, so kctl doesn't extract "knowledge" from abandoned work.

---

## 6. Revised file structure

```
sprintctl/
  __init__.py
  db.py          — schema, migrations, data access, transition enforcement
  cli.py         — Click commands (thin dispatch)
  calc.py        — pure functions: staleness, health, risk (NEW)
  maintain.py    — sweep, check, carryover logic (NEW)
  render.py      — plain-text sprint doc (enhanced with calc annotations)
  types.py       — shared constants: event types, thresholds, transitions (NEW, optional)
tests/
  conftest.py    — shared fixtures
  test_core.py   — existing workflow tests
  test_calc.py   — calc function unit tests (NEW)
  test_maintain.py — maintenance command tests (NEW)
```

---

## 7. Revised phase plan

### Phase 1.1 (immediate — tighten what exists)

- Move transition enforcement into db.py (`InvalidTransition`)
- Add sprint status transitions
- Add `calc.py` with `item_staleness`, `track_health`, `sprint_overrun_risk`
- Enhance `render` to include staleness annotations and track health
- Tests for calc functions

### Phase 2 (calculate-on-call + maintenance)

- Add `maintain.py` with `check`, `sweep`, `carryover`
- Wire `sprintctl maintain` CLI group
- Add claim table (migration 2, inactive — no CLI exposure)
- Add claim expiry logic in calc.py (dormant until claims are activated)
- Configurable thresholds (env vars or a `sprintctl.toml` — keep it simple)
- Tests for maintenance commands

### Phase 2.5 (activate claims — when needed)

- Add `claim create`, `claim heartbeat`, `claim release` CLI commands
- Wire claim checks into `item status` transitions
- Enable claim rendering
- `maintain sweep` starts expiring claims

### Phase 3 (companion tool — separate repo)

- `kctl` reads sprintctl DB
- Knowledge candidate extraction from events
- Promotion pipeline: candidate → approved → published
- Triggers `sprintctl maintain check` as pre-flight
- Own storage (SQLite or flat files)

---

## 8. Configuration model

Avoid per-field policy sprawl. One config source, minimal surface.

```sh
# Environment variables (already established pattern)
SPRINTCTL_DB=/path/to/db              # existing
SPRINTCTL_STALE_THRESHOLD=4h          # new: default 4h
SPRINTCTL_SWEEP_AUTO_CLOSE=false      # new: whether sweep auto-closes overdue sprints
```

If config grows beyond 3-4 vars, introduce `~/.sprintctl/config.toml`:

```toml
[thresholds]
stale_item = "4h"
claim_ttl = "1h"           # future
claim_heartbeat = "15m"    # future

[sweep]
auto_close_overdue = false
auto_block_stale = true
```

Config is read once at CLI startup and passed into functions. No global state. calc.py and maintain.py receive thresholds as arguments.

---

## 9. Open questions

1. **Carryover semantics**: Should carried-over items keep their original ID (via a `carried_from` column) or get new IDs with a reference? New IDs are cleaner for cross-sprint queries but lose direct lineage.

2. **Blocked → active revival**: Current transitions make `blocked` terminal. Should `maintain sweep` or manual action be able to unblock? If yes, add `blocked → active` to `VALID_TRANSITIONS`. This is likely needed for the stale-auto-block pattern to be reversible.

3. **Event retention**: As events accumulate, should old sprint events be archived or pruned? Or is SQLite's scale sufficient indefinitely for this use case? (Probably yes for years of single-user agent work.)

4. **kctl trigger mechanism**: Should kctl calling `maintain check` be a subprocess call, a Python import, or reading a shared library? Subprocess is cleanest for repo separation but slower. Python import couples the repos.
