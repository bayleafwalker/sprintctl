## Task: Architectural tightening — align Phase 1 output with revised architecture
## Spec reference: sprintctl_revised_architecture.md (sections 1, 4, 6)

## Context
Phase 1 is complete (32/32 tests passing). This transitional sprint closes the gap between what
Phase 1 built and what the revised architecture requires before Phase 2 begins. No new user-facing
features — these are correctness and structural fixes.

## This session builds:
- `sprintctl/calc.py` — pure functions: staleness, track health, sprint overrun risk (NEW)
- `sprintctl/db.py` — move transition enforcement in from cli.py; add sprint status transitions
- `sprintctl/render.py` — enhance with staleness annotations and track health via calc.py
- `tests/test_calc.py` — unit tests for all calc functions (NEW)

## Stop at:
- No `maintain.py`, no sweep/carryover commands
- No claim table or claim logic
- No config file / env var parsing for thresholds (hardcode defaults for now)
- No changes to CLI command surface beyond what transition enforcement requires

## Tasks

### 1. Move transition enforcement into db.py

Add `InvalidTransition(ValueError)` to `db.py`. Move the guard from `cli.py` into
`set_work_item_status()` so that every caller — CLI, future maintain.py, tests — gets enforcement
for free.

```python
class InvalidTransition(ValueError):
    pass

VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending":  {"active"},
    "active":   {"done", "blocked"},
    "done":     set(),
    "blocked":  set(),
}

def set_work_item_status(conn, item_id, new_status):
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

CLI should catch `InvalidTransition` and exit with a clean error message (not a traceback).
Remove the now-redundant guard from `cli.py`.

### 2. Add sprint status transitions to db.py

Add `SPRINT_TRANSITIONS` and a `set_sprint_status()` function enforcing `planned → active → closed`.

```python
SPRINT_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"active"},
    "active":  {"closed"},
    "closed":  set(),
}
```

`create_sprint` may still set initial status directly (bootstrap convenience). All subsequent
changes go through `set_sprint_status()`.

### 3. Add calc.py

Pure functions only — no DB calls, no side effects, `now` always passed explicitly.

```
calc.item_staleness(item, now, threshold=timedelta(hours=4)) -> dict
calc.track_health(items) -> dict
calc.sprint_overrun_risk(sprint, active_items, now) -> dict
```

See `sprintctl_revised_architecture.md` §1 for full signatures and return shapes.

### 4. Enhance render.py with calc annotations

- Per-track section: append track health summary (total, done ratio, blocked ratio)
- Per-item line: annotate stale items with idle duration (e.g. `[stale 6h12m]`)
- Sprint header: include overrun risk flag if `at_risk` or `overdue`

`render_sprint_doc()` signature gains a `now` parameter (already present); pass through to calc
functions. No new I/O.

### 5. Add tests/test_calc.py

Unit tests for all three calc functions. Use fixed `now` values — no `datetime.now()` calls in
tests. Cover:
- Item staleness: stale, not stale, terminal status (done/blocked should not be stale)
- Track health: empty track, all done, mixed, high blocked ratio
- Sprint overrun risk: at risk, overdue, healthy

## Acceptance criteria
- `pytest tests/test_core.py` still passes (32/32) — no regressions
- `pytest tests/test_calc.py` passes
- Attempting an invalid item transition via CLI exits with a clean error, not a traceback
- Attempting an invalid item transition directly via `db.set_work_item_status()` raises `InvalidTransition`
- `sprintctl render` output includes staleness annotations for stale items and track health summaries
