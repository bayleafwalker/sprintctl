# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Agent integration guide

**Read `AGENTS.md` first.** It is the primary reference for this repo: claim lifecycle, environment variables, session resumption, shutdown checklist, and quick-reference commands. The parent `/workspace/dev/AGENTS.md` covers devbox vs workstation context, tool install rules, PATH, direnv, and session cost logging.

## Development

```sh
# Set up local venv
python -m venv .venv
.venv/bin/pip install -e .

# Run all tests
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v

# Run a single test file
PYTHONPATH=. .venv/bin/python -m pytest tests/test_claims.py -v

# Invoke CLI from source (use instead of global binary when developing)
.venv/bin/python -m sprintctl --help
```

Refresh stale global installs with `pipx upgrade sprintctl && pipx upgrade kctl` (or `uv tool upgrade sprintctl kctl`).

## Commit discipline

- Run `pytest` before every commit; report pass/fail count.
- **Never commit with failing tests.**
- **One sprint item = one commit.** Commit when the item is done, not at session end.
- Behavior changes must include updated or new tests in the same commit.
- If tests fail, self-heal (diagnose, fix, re-run) up to 5 cycles before escalating.

## Source layout

| Module | Role |
|--------|------|
| `sprintctl/cli.py` | Click CLI — all commands defined here |
| `sprintctl/backend.py` | Business logic, state transitions, claim operations |
| `sprintctl/db.py` | SQLite schema, migrations, low-level queries |
| `sprintctl/contracts.py` | Typed data models for JSON/text output surfaces |
| `sprintctl/calc.py` | Staleness thresholds, derived state calculations |
| `sprintctl/maintain.py` | `maintain check` health rules |
| `sprintctl/render.py` | Text renderer for sprint snapshot documents |
| `sprintctl/pg.py` | Optional PostgreSQL backend (`remote` extra) |

The `contracts.py` models are the boundary between internal DB state and what `--json` surfaces emit. Keep JSON and text output describing the same state in the same order.

## Environment

`SPRINTCTL_DB` must point at the project-scoped database, not `~/`. The `envrc.example` template sets this; copy it to `.envrc` and run `direnv allow`.
