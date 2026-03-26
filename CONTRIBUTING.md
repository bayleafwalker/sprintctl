# Contributing

## Prerequisites

- Python 3.11+
- [pipx](https://pipx.pypa.io/)
- [direnv](https://direnv.net/)

## Setup

Install both tools globally:

```sh
pipx install git+https://github.com/bayleafwalker/sprintctl.git
pipx install git+https://github.com/bayleafwalker/kctl.git
```

Copy the direnv template into the project root and allow it:

```sh
cp envrc.example .envrc
direnv allow
```

This scopes `SPRINTCTL_DB` and `KCTL_DB` to the project directory. Verify with `echo $SPRINTCTL_DB`.

Bootstrap your local sprint state. If a sprint is already active, the committed render at `docs/sprint-current.txt` is the source of truth — read it, then create a matching local sprint:

```sh
sprintctl sprint create --name "Sprint N" --start <YYYY-MM-DD> --end <YYYY-MM-DD> --status active
```

If you are starting a new sprint, create it directly and commit the initial render.

## Daily workflow

Create and transition work items via CLI:

```sh
sprintctl item add --sprint-id <id> --track <track> --title "<title>"
sprintctl item status --id <id> --status active
sprintctl item status --id <id> --status done
```

Before pushing, commit the current sprint render:

```sh
make sprint-snapshot
# or: sprintctl render > docs/sprint-current.txt && git add docs/sprint-current.txt && git commit -m "chore: update sprint snapshot"
```

Periodically extract knowledge from sprint events and review what kctl surfaces:

```sh
kctl extract
kctl review
```

Approve or discard entries before committing any kctl output.

## What not to do

- Do not commit `.sprintctl/` or `.kctl/`. Both are in `.gitignore` for a reason — they are binary blobs with no meaningful diff.
- Do not share your database file directly. If another contributor needs sprint context, point them to `docs/sprint-current.txt`.
- Do not assume your local database matches another contributor's. It won't. The committed render is the shared record; local databases are local state.
