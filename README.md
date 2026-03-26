# sprintctl

A minimal, agent-centric sprint coordination CLI backed by SQLite.

**Not a general project management tool.** This is a lightweight coordination layer for agent-driven work — designed to support parallel agent/subagent execution with clear, enforced state transitions.

## Design goals

- Support parallel agent/subagent execution without shared markdown conflicts
- Maintain clear, time-bound sprint state in a single SQLite database
- Enforce process consistency (status transitions, event sourcing) at the CLI layer
- Keep the schema and surface area minimal — no ORM, stdlib only (plus Click)

## Status

**Phase 1 complete** — core schema, CLI, and rendering. 32/32 tests passing.

Phases 2 (claims, staleness, daemon) and 3 (knowledge promotion, policy profiles, API wrapper) are planned but not yet started.

## Requirements

- Python 3.11+
- [click](https://click.palletsprojects.com/) (only non-stdlib dependency)

## Installation

```sh
pip install -e .
```

Or in a virtual environment:

```sh
python -m venv .venv
.venv/bin/pip install -e .
```

## Configuration

By default, the database is stored at `~/.sprintctl/sprintctl.db`. Override with:

```sh
export SPRINTCTL_DB=/path/to/custom.db
```

## Usage

### Sprints

```sh
# Create a sprint
sprintctl sprint create --name "Sprint 1" --start 2026-03-24 --end 2026-04-04 --status active

# Show active sprint (or use --id to specify)
sprintctl sprint show

# List all sprints
sprintctl sprint list
```

### Work items

```sh
# Add an item to a sprint track (track is created if it doesn't exist)
sprintctl item add --sprint-id 1 --track backend --title "Implement auth"

# List items (filterable by sprint, track, or status)
sprintctl item list --sprint-id 1
sprintctl item list --track backend --status pending

# Transition item status (enforced: pending → active → done | blocked)
sprintctl item status --id 3 --status active
sprintctl item status --id 3 --status done
```

Status transitions are enforced. `done` and `blocked` are terminal states.

### Events

```sh
# Record an event against a sprint (optionally linked to a work item)
sprintctl event add --sprint-id 1 --type note --actor agent-1 --item-id 3
sprintctl event add --sprint-id 1 --type blocker-raised --actor agent-2 --payload '{"reason": "waiting on infra"}'
```

Source types: `actor` (default), `daemon`, `system`.

### Rendering

```sh
# Render a plain-text sprint document (defaults to active sprint)
sprintctl render
sprintctl render --sprint-id 1
```

Output is derived from database state. Re-running is idempotent. The rendered doc includes sprint header, per-track item lists, and a UTC timestamp.

## Architecture

```
sprintctl/
  db.py       — SQLite init, migrations, all data access functions
  cli.py      — Click CLI entry point; thin dispatch layer
  render.py   — Pure rendering function; no side effects
tests/
  conftest.py — Shared fixtures (in-memory DB)
  test_core.py — Workflow tests
```

**Key design decisions:**

- `VALID_TRANSITIONS` in `db.py` is the single source of truth for status transitions
- `render_sprint_doc()` is a pure function — timestamp is passed in, not generated inside
- WAL mode is enabled now for Phase 2 daemon readiness
- Rows are returned as dicts; no ORM layer
- DB path is resolved via `SPRINTCTL_DB` env var or `~/.sprintctl/sprintctl.db`

## Running tests

```sh
PYTHONPATH=. .venv/bin/python -m pytest tests/test_core.py -v
```

## Planned phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Core schema, CLI, rendering | Complete |
| 2 | Claims, staleness detection, daemon/sweeper, carryover | Planned |
| 3 | Knowledge promotion, policy profiles, API wrapper | Planned |

## Anti-goals

- Not a Jira/Linear replacement
- No web UI
- No per-field policy sprawl — policies belong in a policy layer, not state objects
- No agent goodwill assumptions — process enforcement is hard, not advisory
