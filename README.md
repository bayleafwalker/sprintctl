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

For local development:

```sh
pip install -e .
```

## Configuration

```sh
export SPRINTCTL_DB=/path/to/custom.db  # default: ~/.sprintctl/sprintctl.db
```

See [envrc.example](envrc.example) for a direnv template covering sprintctl and kctl together.

## Integration into projects

### Installation

Install via [pipx](https://pipx.pypa.io/), not as a project dependency:

```sh
pipx install git+https://github.com/bayleafwalker/sprintctl.git
```

sprintctl is developer tooling invoked at a project root — not a library imported by application code. Installing it globally via pipx keeps it out of your project's dependency graph and available across all projects without manual venv activation.

For Nix-based setups, a flake is planned (Phase 2). Until then, `pipx` is the canonical method.

### Per-project database via direnv

Each project should have its own sprint database scoped to its working directory. Add this to `.envrc`:

```sh
export SPRINTCTL_DB="${PWD}/.sprintctl/sprintctl.db"
```

With `direnv allow`, the variable is set automatically whenever you enter the project directory. You no longer need `--sprint-id` flags — commands resolve the active sprint from this database.

### .gitignore

Add `.sprintctl/` and `.kctl/` to `.gitignore`:

```
.sprintctl/
.kctl/
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

## Multi-contributor workflows

### DB ownership model

Each contributor maintains their own local `.sprintctl/sprintctl.db`. There is no shared database.

This is deliberate:

- SQLite over a network filesystem (NFS, SMB, cloud-synced folders) breaks under concurrent access. WAL mode does not save you here.
- The database is a binary blob. Git cannot merge it. A conflict is unresolvable.
- A hosted database (Postgres, hosted SQLite) defeats the tool's philosophy. sprintctl has no hosted dependency by design.

Each contributor's database is local state that drives their local agents. It is not a replica of a shared system.

### Synchronization via committed renders

The rendered markdown committed to the repo is the shared state. Contributors render before pushing.

Add a pre-push hook at `.git/hooks/pre-push`:

```sh
#!/bin/sh
sprintctl render > docs/sprint-current.txt
git add docs/sprint-current.txt
git diff --cached --quiet || git commit -m "chore: update sprint snapshot"
```

```sh
chmod +x .git/hooks/pre-push
```

Every push includes an up-to-date snapshot. The diff on `docs/sprint-current.txt` is the reviewable record of what changed and when.

### Divergence is expected

Contributor A's database will not have contributor B's transitions. This is correct behavior, not a gap.

sprintctl coordinates agent-driven work per contributor. It is not a cross-contributor ticketing system. Contributor A's agents operate on contributor A's sprint state. The repo is the integration layer — divergent local databases converge through committed renders, not database replication.

### Onboarding mid-sprint

This is a known gap. A new contributor joining an active sprint has no mechanism to import existing sprint state into a fresh local database. For now:

1. Pull the repo. The committed render in `docs/sprint-current.txt` provides full context on current sprint state.
2. Create a local sprint that mirrors the committed snapshot manually, or start tracking only new work.

A future `sprintctl export` / `sprintctl import` command would address this. It is not scheduled yet.

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
