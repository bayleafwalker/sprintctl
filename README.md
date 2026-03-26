# sprintctl

A minimal, agent-centric sprint coordination CLI backed by SQLite.

**Not a project management tool.** sprintctl is a coordination layer for agent-driven work — a single source of truth for sprint state that agents read from and write to via CLI, instead of editing shared markdown files.

## Why this exists

Agents working from `.md` files drift. They pattern-match on prose structure, update the wrong section, let state go stale, and have no way to enforce transitions. sprintctl replaces that with a schema-backed CLI that makes the correct operation unambiguous and the wrong operation impossible.

## Anti-goals

- Not a Jira/Linear replacement
- No web UI, no hosted dependency
- No per-field policy sprawl — policies belong in a policy layer, not state objects
- No agent goodwill assumptions — transitions are enforced, not advisory

## Requirements

- Python 3.11+
- [click](https://click.palletsprojects.com/) (only non-stdlib dependency)

## Installation

```sh
pip install -e .
```

## Configuration

```sh
export SPRINTCTL_DB=/path/to/custom.db  # default: ~/.sprintctl/sprintctl.db
```

## Quickstart

```sh
# Create a sprint
sprintctl sprint create --name "Sprint 1" --start 2026-03-24 --end 2026-04-04 --status active

# Add work items to tracks
sprintctl item add --sprint-id 1 --track backend --title "Implement auth"
sprintctl item add --sprint-id 1 --track infra --title "Set up CI pipeline"

# Move items through enforced transitions: pending → active → done | blocked
sprintctl item status --id 1 --status active
sprintctl item status --id 1 --status done

# Record events
sprintctl event add --sprint-id 1 --type note --actor agent-1 --item-id 1

# Render current sprint state as a plain-text document
sprintctl render
```

## Commands

### Sprints

```sh
sprintctl sprint create --name <name> --start <YYYY-MM-DD> --end <YYYY-MM-DD> --status <active|planned|closed>
sprintctl sprint show [--id <id>]      # defaults to active sprint
sprintctl sprint list
```

### Work items

```sh
sprintctl item add --sprint-id <id> --track <name> --title <title>
sprintctl item list [--sprint-id <id>] [--track <name>] [--status <status>]
sprintctl item status --id <id> --status <pending|active|done|blocked>
```

Status transitions are enforced. `done` and `blocked` are terminal.

### Events

```sh
sprintctl event add --sprint-id <id> --type <type> --actor <name> [--item-id <id>] [--payload <json>]
```

Event types: `note`, `blocker-raised`, `status-change`, etc.  
Source types: `actor` (default), `daemon`, `system`.

### Rendering

```sh
sprintctl render [--sprint-id <id>]
```

Output is derived entirely from database state. Idempotent. Re-running is safe.

## Architecture

```
sprintctl/
  db.py       — schema, migrations, all data access; transition enforcement via
                InvalidTransition; VALID_TRANSITIONS and SPRINT_TRANSITIONS are
                the single source of truth for allowed status changes
  cli.py      — Click entry point; thin dispatch only, no business logic
  calc.py     — pure functions: item_staleness, track_health, sprint_overrun_risk;
                no DB calls, no side effects, `now` always passed explicitly
  render.py   — plain-text sprint doc; calls calc for staleness annotations and
                track health summaries
tests/
  conftest.py — shared fixtures (in-memory DB)
  test_core.py
  test_calc.py
```

WAL mode is enabled for future concurrent-read readiness.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e .
PYTHONPATH=. .venv/bin/python -m pytest tests/test_core.py -v
```

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Core schema, CLI, rendering | Complete |
| 1.5 | Transition enforcement in db.py, calc.py, render annotations | Complete |
| 2 | maintain.py (check/sweep/carryover), claim table (inactive), configurable thresholds | Planned |
| 2.5 | Activate claims: claim create/heartbeat/release, enforce in transitions | Planned |
| 3 | Companion tool ([kctl](https://github.com/bayleafwalker/kctl)): knowledge extraction from events | In progress |
