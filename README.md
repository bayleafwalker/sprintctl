# sprintctl

`sprintctl` is a local-first execution-state and handoff CLI for a single
developer with optional agent sessions.

It tracks work items, claims, decisions, dependencies, and sprint state in
SQLite, then projects that state into two primary read surfaces:

- `usage --context` for live resume context
- `handoff` for serialized working-memory snapshots

It is not a team project manager, a distributed coordinator, or a richer clone
of an existing task graph tool.

## What It Is

- A local SQLite database of sprint state: sprints, items, events, claims, refs, deps
- A CLI that enforces state transitions and claim proof
- A deterministic resume surface for agent and operator sessions
- A working-memory handoff bundle for session resumption
- A reviewable text renderer for committed sprint snapshots

## What It Is Not

- Not a Jira, Linear, or GitHub Projects replacement
- Not a team coordination layer
- Not a distributed lock service or agent swarm runtime
- Not a hosted app or web UI
- Not a "better task manager" project

## Default Path

```sh
# 1. Create a sprint and a few items
sprintctl sprint create --name "Sprint 4" --status active
sprintctl item add --sprint-id 1 --track docs --title "Write resume guide"

# 2. Read live context
sprintctl usage --context --json

# 3. Claim or start work
sprintctl claim start --item-id 1 --actor codex-session-1 --json

# 4. Record durable history during work
sprintctl item note --id 1 --type decision --summary "Use handoff as working-memory snapshot"

# 5. Hand off or render at a checkpoint
sprintctl handoff --output handoff.json
sprintctl render > docs/sprint-snapshots/sprint-current.txt
```

Use `usage --context` when you need the live answer to "what matters now?" Use
`handoff` when you need a resumable bundle that can cross session boundaries.

## Docs Map

Start here:

- [Start Here](docs/guides/start-here.md)
- [Resume Work](docs/guides/resume-work.md)
- [Agent-Assisted Work](docs/guides/agent-assisted.md)
- [Advanced Coordination](docs/guides/advanced-coordination.md)

Detailed guides:

- [Work Loop](docs/guides/work-loop.md)
- [Project Integration](docs/guides/project-integration.md)
- [Customization Guide](docs/customization.md)
- [Coordinator Mode](docs/advanced/coordinator-mode.md)
- [Claim Discipline](docs/advanced/claim-discipline.md)

Reference:

- [Context and Handoff Contracts](docs/reference/context-and-handoff.md)
- [Knowledge Review Flow](docs/reference/knowledge-review-flow.md)
- [Migration Guide](docs/reference/migration-guide.md)

Plans:

- [Roadmap Reset](docs/plans/roadmap-reset.md)
- [Plans Index](docs/plans/README.md)
- [UX Plan Pack](docs/plans/ux/00-index.md)

Examples:

- [AGENTS.sprintctl.md](docs/examples/AGENTS.sprintctl.md)
- [Makefile.sprintctl.mk](docs/examples/Makefile.sprintctl.mk)
- [repo-template.md](docs/examples/repo-template.md)
- [bootstrap-prompt.md](docs/examples/bootstrap-prompt.md)
- [bootstrap-workflow.md](docs/examples/bootstrap-workflow.md)

## Source Of Truth Order

When sources disagree, use this order:

1. live `sprintctl` state
2. `usage --context` and `handoff` projections
3. committed `render` output
4. repo docs and planning notes

The database is live state. Rendered snapshots are review artifacts. Plans are
not the control plane.

## Installation

```sh
pipx install git+https://github.com/bayleafwalker/sprintctl.git
```

For local development:

```sh
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m pytest tests/ -v
```

## Configuration

```sh
export SPRINTCTL_DB=/path/to/custom.db
export SPRINTCTL_STALE_THRESHOLD=4
export SPRINTCTL_PENDING_STALE_THRESHOLD=24
export SPRINTCTL_RUNTIME_SESSION_ID="${CODEX_THREAD_ID:-manual-session}"
export SPRINTCTL_INSTANCE_ID="stable-per-process-uuid"
```

Per-project repos should usually point `SPRINTCTL_DB` at `.sprintctl/sprintctl.db`
and gitignore that directory.

## Design Defaults

- CLI-first, local-first, explicit state
- `claim_id + claim_token` is the only ownership proof
- `usage --context --json` is the primary resume contract
- `handoff --format json` is the serialized working-memory contract
- JSON and text surfaces should describe the same state in the same order
- Convenience belongs mostly in repo-local wrappers, not in the core binary
