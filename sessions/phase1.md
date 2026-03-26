## Task: Core schema, CLI, and basic rendering
## Spec reference: sprintctl_starter.md (agent reads as needed)

## This session builds:
- `sprintctl/db.py` — SQLite init, migrations, schema for Sprint, Track, WorkItem, Event
- `sprintctl/cli.py` — CLI entry point (sprint, item, event subcommands)
- `sprintctl/render.py` — plain-text sprint doc rendered from DB state
- `tests/test_core.py` — workflow tests covering the scenarios below

## Stop at:
- No claims, no daemon, no staleness logic
- No handoff, pending, or knowledge tables
- No policy profiles

## Acceptance criteria:
- `sprintctl sprint create` + `sprint show` round-trips cleanly through SQLite
- `sprintctl item add` / `item list` / `item status` enforce only the allowed status transitions (pending → active → done | blocked)
- `sprintctl render` produces a doc with sprint header, per-track item list, and a timestamp; re-running is idempotent
- All acceptance scenarios pass under `pytest tests/test_core.py`
