# Contributing

## Prerequisites

- Python 3.11+
- [pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/)
- [direnv](https://direnv.net/) (recommended)

## Setup

Install or update CLI tools:

```sh
pipx install git+https://github.com/bayleafwalker/sprintctl.git
pipx install git+https://github.com/bayleafwalker/kctl.git
pipx upgrade sprintctl
pipx upgrade kctl
# or: uv tool install git+https://github.com/bayleafwalker/sprintctl.git
# or: uv tool install git+https://github.com/bayleafwalker/kctl.git
# or: uv tool upgrade sprintctl kctl
```

Copy the direnv template into the project root and allow it:

```sh
cp envrc.example .envrc
direnv allow
```

This scopes `SPRINTCTL_DB` to the project directory. Verify with `echo $SPRINTCTL_DB`.

For repo-local development, prefer running the module entrypoint so you always
exercise the checked-out source:

```sh
.venv/bin/python -m sprintctl --help
.venv/bin/python -m sprintctl next-work --help
```

If a globally installed `sprintctl` misses documented flags, keep using the
repo-local module entrypoint and refresh global tools with
`pipx upgrade sprintctl && pipx upgrade kctl` (or `uv tool upgrade sprintctl kctl`).

Bootstrap your local sprint state:

```sh
sprintctl sprint create --name "Sprint N" --start <YYYY-MM-DD> --end <YYYY-MM-DD> --status active
```

If you're migrating from another machine, import from an export file:

```sh
sprintctl import --file sprint-N.json
```

## Daily workflow

Create and transition work items via CLI:

```sh
sprintctl item add --sprint-id <id> --track <track> --title "<title>"
REV=$(sprintctl item show --id <id> --json | jq -r '.item.status_revision')
sprintctl item status --id <id> --status active --expected-revision "$REV"
# close with a decision (accept sets done with a recorded rationale)
sprintctl item decide --id <id> --kind accept --rationale "<why>" --actor <you>
```

Check sprint health at any time (read-only):

```sh
sprintctl maintain check
```

Commit a render snapshot at natural checkpoints — end of a work session, before a review, after a carryover:

```sh
# with docs/examples/Makefile.sprintctl.mk included in your Makefile:
make sprint-snapshot
# or: sprintctl render > docs/sprint-snapshots/sprint-current.txt && git add docs/sprint-snapshots/sprint-current.txt && git commit -m "chore: update sprint snapshot"
```

## What not to do

- Do not commit `.sprintctl/`. It is in `.gitignore` for a reason — it is a binary blob with no meaningful diff.
- Do not try to sync or share the database file. sprintctl is local-only tooling; the database is not designed to be shared or merged.

## Running tests

```sh
uv sync --extra dev
uv run pytest -q -m "not perf"
```

This matches CI (`.github/workflows/ci.yml`). Wall-clock `perf` tests are
deselected there; run them on demand with `uv run pytest -q -m perf`.
PostgreSQL integration tests need a disposable database; see
[docs/guides/postgres-integration-tests.md](docs/guides/postgres-integration-tests.md).

```sh
# equivalent without uv
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q -m "not perf"
```
