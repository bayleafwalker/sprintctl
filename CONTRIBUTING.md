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
sprintctl item status --id <id> --status active
sprintctl item status --id <id> --status done
```

Check sprint health at any time (read-only):

```sh
sprintctl maintain check
```

Commit a render snapshot at natural checkpoints — end of a work session, before a review, after a carryover:

```sh
make sprint-snapshot
# or: sprintctl render > docs/sprint-current.txt && git add docs/sprint-current.txt && git commit -m "chore: update sprint snapshot"
```

## What not to do

- Do not commit `.sprintctl/`. It is in `.gitignore` for a reason — it is a binary blob with no meaningful diff.
- Do not try to sync or share the database file. sprintctl is local-only tooling; the database is not designed to be shared or merged.

## Running tests

```sh
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```
