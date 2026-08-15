# sprintctl Repo Template

Use this template when integrating `sprintctl` into a repository that already
has its own build/test conventions.

## Goal

Provide one consistent shape for:

- startup context collection
- reservation-aware execution
- checkpoint rendering
- session shutdown and handoff

## Suggested Layout

```text
.
├── AGENTS.md
├── Makefile
├── scripts/
│   └── sprint/
│       ├── start-session.sh
│       └── end-session.sh
├── docs/
│   └── sprint-snapshots/
└── .sprintctl/
```

## AGENTS.md Section

```md
## sprintctl operating expectations

Before meaningful edits:
1. sprintctl usage --context --json
2. sprintctl next-work --json --explain
3. if taking ownership, sprintctl reservation reserve and keep reservation_id for session

During work:
- touch active reservations when useful
- record decision/blocker notes on the active item

Before session end:
- set item status with expected-revision when appropriate
- reassign or release every active reservation
- refresh handoff bundle
```

## Makefile Sketch

```make
resume:
	@sprintctl usage --context
	@echo
	@sprintctl next-work --explain

agent-context:
	@sprintctl usage --context --json
	@sprintctl next-work --json --explain
	@sprintctl git-context --json

snapshot:
	@sprintctl render > docs/sprint-snapshots/current.txt

handoff:
	@sprintctl handoff --format text
```

## Startup Script

`scripts/sprint/start-session.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

sprintctl usage --context --json
sprintctl next-work --json --explain
sprintctl git-context --json
```

## Shutdown Script

`scripts/sprint/end-session.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

sprintctl handoff --format json --output handoff.json
sprintctl handoff --format text
```

## Notes

- Keep the SQLite DB local (`.sprintctl/`) and out of git.
- Commit only review artifacts such as rendered snapshots.
- Prefer explicit command wrappers over hidden side effects.

## Related

- [Project Integration](../guides/project-integration.md)
- [Customization Guide](../customization.md)
