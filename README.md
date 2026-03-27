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
pipx install git+https://github.com/bayleafwalker/sprintctl.git
```

sprintctl is developer tooling invoked at a project root — not a library imported by application code. Installing it globally via pipx keeps it out of your project's dependency graph and available across all projects without manual venv activation.

For local development:

```sh
python -m venv .venv && .venv/bin/pip install -e .
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

## Configuration

```sh
export SPRINTCTL_DB=/path/to/custom.db          # default: ~/.sprintctl/sprintctl.db
export SPRINTCTL_STALE_THRESHOLD=4              # active item staleness threshold in hours (default: 4)
export SPRINTCTL_PENDING_STALE_THRESHOLD=24     # pending item staleness in hours (unset = pending items never stale)
```

See [envrc.example](envrc.example) for a direnv template.

## Integration into projects

### Per-project database via direnv

Each project should have its own sprint database scoped to its working directory. Add this to `.envrc`:

```sh
export SPRINTCTL_DB="${PWD}/.sprintctl/sprintctl.db"
```

With `direnv allow`, the variable is set automatically whenever you enter the project directory.

### .gitignore

Add `.sprintctl/` to `.gitignore`:

```
.sprintctl/
```

The database is a binary SQLite blob. Committing it produces opaque diffs and creates merge conflicts that cannot be resolved meaningfully. It is local state, not source.

### Committed snapshots

Commit `sprintctl render` output as the reviewable, diffable record of sprint state. Plain text diffs cleanly; the binary database does not.

Example Makefile target:

```makefile
sprint-snapshot:
	sprintctl render > docs/sprint-current.txt
	git add docs/sprint-current.txt
	git commit -m "chore: update sprint snapshot"
```

Run this at natural checkpoints — end of day, before a review, after a carryover. The snapshot is what you share and review; the database is what drives it.

## Local-only design

sprintctl is explicitly local developer tooling. The database lives on your machine; there is no shared database, no sync protocol, and no hosted dependency.

- The database is a binary blob. Git cannot merge it. It belongs in `.gitignore`.
- The committed `sprintctl render` output is the reviewable, portable record — not the database.
- Export/import exists for migrating state between your own machines, not for sharing state with others.

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
sprintctl sprint create --name <name> --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
    [--status <planned|active|closed>] [--goal <text>] [--kind <active_sprint|backlog|archive>]

sprintctl sprint show [--id <id>] [--detail]   # --detail adds health summary and track breakdown
sprintctl sprint list [--include-backlog] [--include-archive]
sprintctl sprint status --id <id> --status <planned|active|closed>
sprintctl sprint kind --id <id> --kind <active_sprint|backlog|archive>
```

Sprint status transitions are enforced: `planned → active → closed`. `closed` is terminal.

Sprint kinds classify a sprint's role — `active_sprint` (default), `backlog`, or `archive`. Sprints of kind `backlog` and `archive` are hidden from `sprint list` unless the corresponding flag is passed.

### Work items

```sh
sprintctl item add --sprint-id <id> --track <name> --title <title> [--assignee <name>]
sprintctl item list [--sprint-id <id>] [--track <name>] [--status <pending|active|done|blocked>]
sprintctl item status --id <id> --status <pending|active|done|blocked> [--actor <name>]
sprintctl item note --id <id> --type <type> --summary <text> [--detail <text>] [--tags <a,b>] [--actor <name>]
```

Item status transitions are enforced. `done` and `blocked` are terminal (no further transitions allowed). Passing `--actor` to `item status` enforces exclusive claim checks — the transition is rejected if another agent holds an active exclusive claim on the item.

`item note` records a structured event on a work item. Use it for decisions, blockers, architecture notes, and lessons learned. The `--type` value is freeform; see [Events](#events) for conventional types that the companion tool [kctl](https://github.com/bayleafwalker/kctl) recognizes.

### Events

```sh
sprintctl event add --sprint-id <id> --type <type> --actor <name> \
    [--item-id <id>] [--source <actor|daemon|system>] [--payload <json>]
```

Event type is freeform text. Conventional types recognized by [kctl](https://github.com/bayleafwalker/kctl) for knowledge extraction:

| Type | Meaning |
|------|---------|
| `decision` | Architectural or process decision |
| `blocker-resolved` | How a blocker was resolved |
| `pattern-noted` | Reusable pattern identified |
| `risk-accepted` | Explicit risk acceptance with reasoning |
| `lesson-learned` | Retrospective insight |

For knowledge-bearing events, structure the payload as:

```json
{
  "summary": "one-line description",
  "detail": "longer explanation",
  "tags": ["auth", "architecture"]
}
```

### Claims

Claims allow agents to take exclusive or shared ownership of a work item before transitioning it. An exclusive claim on an item blocks other agents from transitioning it until the claim is released or expires.

```sh
sprintctl claim create --item-id <id> --agent <name> \
    [--type <inspect|execute|review|coordinate>] [--ttl <seconds>] [--non-exclusive]

sprintctl claim heartbeat --id <claim-id> --agent <name> [--ttl <seconds>]
sprintctl claim release --id <claim-id> --agent <name>
sprintctl claim list --item-id <id> [--all]
sprintctl claim list-sprint [--sprint-id <id>] [--all] [--expiring-within <seconds>] [--json]
```

Claim types:

| Type | Meaning |
|------|---------|
| `execute` | Default. Agent is actively working the item. Exclusive. |
| `inspect` | Agent is reading state. Non-exclusive. |
| `review` | Agent is reviewing completed work. |
| `coordinate` | Agent is orchestrating sub-agents on the item. |

Default TTL is 300 seconds. Refresh with `claim heartbeat` to keep a long-running claim alive. Claims past their TTL are expired by `maintain sweep`.

### Maintenance

```sh
sprintctl maintain check [--sprint-id <id>] [--threshold <Nh>] [--json]
sprintctl maintain sweep [--sprint-id <id>] [--threshold <Nh>] [--auto-close]
sprintctl maintain carryover --from-sprint <id> --to-sprint <id>
```

`maintain check` is a read-only diagnostic — it reports stale items, track health, and sprint overrun risk without writing anything. Safe to run at any time.

`maintain sweep` executes: stale active items are transitioned to `blocked` and a system event is emitted per item. With `--auto-close`, an overdue sprint with no remaining active items is closed automatically.

`maintain carryover` moves all incomplete items (`pending`, `active`, `blocked`) from the source sprint to the target sprint. Each original item is marked `done` with a carryover payload. New items are created in the target sprint preserving track and title.

The staleness threshold defaults to 4 hours and can be overridden per-invocation with `--threshold 2h`, or globally via `SPRINTCTL_STALE_THRESHOLD`.

These commands are regular CLI invocations — schedule them externally if you want automated sweeps:

```sh
# cron example
*/30 * * * * sprintctl maintain sweep 2>&1 | logger -t sprintctl
```

### Export / Import

```sh
sprintctl export --sprint-id <id> [--output <path>]   # default: sprint-N.json
sprintctl import --file <path>
```

Export writes a JSON envelope containing the sprint, tracks, items, and events. Import re-sequences all IDs into the local database. Use this for migrating state between your own machines.

### Rendering

```sh
sprintctl render [--sprint-id <id>]
```

Output is derived entirely from database state. Idempotent. Re-running is safe. Includes staleness annotations and track health summaries.

## Architecture

```
sprintctl/
  db.py       — schema, migrations, all data access; transition enforcement via
                InvalidTransition and ClaimConflict; VALID_TRANSITIONS and
                SPRINT_TRANSITIONS are the single source of truth for allowed changes
  cli.py      — Click entry point; thin dispatch only, no business logic
  calc.py     — pure functions: item_staleness, track_health, sprint_overrun_risk;
                no DB calls, no side effects, `now` always passed explicitly
  render.py   — plain-text sprint doc; calls calc for staleness annotations and
                track health summaries
  maintain.py — check, sweep, carryover logic; all writes go through db.py
tests/
  conftest.py     — shared fixtures (in-memory DB)
  test_core.py    — schema, transitions, core workflow
  test_calc.py    — calc function unit tests
  test_maintain.py — maintenance command tests
```

Key architectural decisions:

- **No daemon** — calculate-on-call: all derived state computed at read time in `calc.py`
- **Maintenance = explicit CLI commands** — not sweep loops
- **Transition enforcement lives in db.py** — not cli.py; both `InvalidTransition` and `ClaimConflict` are raised there
- **WAL mode enabled** — allows concurrent reads from multiple local agents alongside CLI writes
- **DB path**: `SPRINTCTL_DB` env var or `~/.sprintctl/sprintctl.db`
- **No ORM** — sqlite3 stdlib only; rows returned as dicts
- **`render_sprint_doc()` is a pure function** — timestamp passed in

## Companion tool

[kctl](https://github.com/bayleafwalker/kctl) is a separate tool that reads the sprintctl database (read-only, never writes) and extracts durable knowledge from sprint events. It operates on its own storage and calls `sprintctl maintain check` as a pre-flight before extraction.

sprintctl and kctl share a read path but not a write path. kctl never transitions item or sprint status, never creates claims, and never modifies the sprintctl database.
