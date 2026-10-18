# sprintctl

Sprintctl keeps work identifiable, resumable, and explainable as people and
agents change.

It records work items, advisory reservations, releases, decisions, evidence,
dependencies, and continuation points. The CLI supports local SQLite work and
a served Vuoro authority. Served capabilities also include durable inboxes,
run continuation, and scoped delegation.

Sprintctl is Vuoro's reference work provider. Its role is to preserve meaningful
work identity and history across sessions, hosts, and providers; the ledger is
replaceable. Native harnesses own execution, and protected executors own effects.

Start with these read surfaces:

- `usage --context` for current work, conflicts, and a next action
- `handoff` for working-memory snapshots, including predecessor position
- `run brief` for a task-specific served change briefing
- `run diagnose` for missing continuation context and explicitly unchecked facts
- `effect explain` for exact effect acceptance facts, receipt visibility, and missing links
- `session resume` for a combined context bundle in local SQLite mode;
  in served mode, use the constituent reads in the [resume guide](docs/guides/resume-work.md)

## What It Is

- Explicit work records in SQLite or a served authority
- A CLI that enforces state transitions and expected-revision compare-and-swap
- A deterministic resume surface for agent and operator sessions
- A working-memory handoff bundle for session resumption
- A reviewable text renderer for committed sprint snapshots
- Readable continuation over owner facts, with changed, unchanged, and unknown kept distinct

Reservations signal coordination; they do not grant authority. Reported completion
does not substitute for acceptance. A successor reads predecessor context without
inheriting predecessor credentials. An unchanged reference record does not prove
that the referenced artifact is unchanged or accessible.

The next product phase is [resume and explain](docs/plans/resume-and-explain.md).
The compact briefing and bounded effect explanation are available in this source tree; a fully qualified result
explanation, recovery-burden assessment, and accepted-outcome comparison remain
separate development slices. The existing archival result transfer is a bounded
fixture proof, not a general supported result-packet export.
The [effect explanation guide](docs/guides/explain-effects.md) distinguishes the
owner's acceptance facts from evidence of an external effect.

## What It Is Not

- Not a Jira, Linear, or GitHub Projects replacement
- Not a team coordination layer
- Not a distributed lock service or agent swarm runtime
- Not a hosted app or web UI
- Not a "better task manager" project

## Default Path

This example uses local SQLite. For a served authority, follow the
[resume guide](docs/guides/resume-work.md); snapshot rendering is local-only.

```sh
# 1. Create a sprint and a few items
sprintctl sprint create --name "Sprint 4" --status active
sprintctl item add --sprint-id 1 --track docs --title "Write resume guide" \
  --description "Document the reservation reassignment and handoff path."

# Descriptions can be replaced after an item is reshaped.
sprintctl item edit --id 1 --description "Document reservation reassignment, activity touch, and handoff."

# 2. Read live context
sprintctl usage --context --json
sprintctl next-work --json --explain

# 3. Reserve or start work
sprintctl reservation reserve --item-id 1 --actor codex-session-1 --json

# 4. Record durable history during work
sprintctl item note --id 1 --type decision --summary "Use handoff as working-memory snapshot"

# 5a. If done: record an accept decision (it closes the item), then release
sprintctl item decide --id 1 --kind accept --rationale "Verified and merged" --actor codex-session-1
sprintctl reservation release --id <reservation_id> --actor codex-session-1

# 5b. If work continues: reassign the reservation instead
# (do not release first)
sprintctl reservation reassign --id <reservation_id> --actor codex-session-2 --session-id <next-session-id> --json
sprintctl handoff --output handoff.json
sprintctl render > docs/sprint-snapshots/sprint-current.txt
```

Use `usage --context` when you need the live answer to "what matters now?" Use
`handoff` when you need a resumable bundle that can cross session boundaries.
For a served run and explicit item, use `sprintctl run brief --run-id <run-id>
--item-id <id> --checkpoint checkpoint.json` to inspect changes against a saved owner checkpoint. It performs
one read and leaves missing observations unknown; see the
[resume guide](docs/guides/resume-work.md) for checkpoint and pagination handling.

## Docs Map

Start here:

- [Start Here](docs/guides/start-here.md)
- [Resume Work](docs/guides/resume-work.md)
- [Agent-Assisted Work](docs/guides/agent-assisted.md)
- [Advanced Coordination](docs/guides/advanced-coordination.md)

Detailed guides:

- [Work Loop](docs/guides/work-loop.md)
- [Daily Loop](docs/guides/daily-loop.md)
- [Project Integration](docs/guides/project-integration.md)
- [Multi-repository Project Scope](docs/guides/project-scope.md)
- [Normal synchronization](docs/guides/normal-sync.md)
- [Remote Authority Commands](docs/guides/authority-commands.md)
- [Customization Guide](docs/customization.md)
- [Coordinator Mode](docs/advanced/coordinator-mode.md)
- [Reservation Discipline](docs/advanced/reservation-discipline.md)

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
- [alias-pack.md](docs/examples/alias-pack.md)
- [agent-prompt-snippets.md](docs/examples/agent-prompt-snippets.md)
- [editor-and-terminal-integration.md](docs/examples/editor-and-terminal-integration.md)
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
pipx install git+https://github.com/bayleafwalker/kctl.git
```

Equivalent `uv tool` install:

```sh
uv tool install git+https://github.com/bayleafwalker/sprintctl.git
uv tool install git+https://github.com/bayleafwalker/kctl.git
```

To refresh stale global installs:

```sh
pipx upgrade sprintctl
pipx upgrade kctl
# or: uv tool upgrade sprintctl kctl
```

For local development:

```sh
uv sync --frozen --extra dev --extra served --extra remote
uv run --frozen --extra dev --extra served --extra remote pytest -q -m "not perf"
```

Use the committed lockfile and a clean worktree to reproduce release checks.
PostgreSQL integration requires the disposable test harness; a normal source
test run skips it. An old wheel left in `dist/` participates in wheel-contract
tests and must not be mistaken for the current build.

Prefer invoking the CLI from the source tree while developing:

```sh
.venv/bin/python -m sprintctl --help
.venv/bin/python -m sprintctl next-work --help
```

If your global install drifts from the checked-out source command surface,
prefer the module entrypoint for `sprintctl` and refresh global tools with
`pipx upgrade sprintctl && pipx upgrade kctl` (or `uv tool upgrade sprintctl kctl`).

Run the read-only doctor before changing an installation or backend:

```sh
sprintctl doctor
sprintctl doctor --json
```

The report compares the executable found on `PATH`, imported package metadata,
and a checked-out source version. It also reports the `remote` extra, effective
backend marker/configuration, and the configured database's schema capability.
Remote URLs are never printed. Schema probes are read-only and the command does
not install packages, migrate a database, or otherwise repair findings. Follow
the emitted `pipx`, `uv`, or editable-source reinstall guidance explicitly.

The source-tree entrypoint should expose the same command surface as the
console script, including `next-work --explain` and `session resume`.

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
- Reservations are advisory coordination signals, not ownership proof
- `usage --context --json` is the primary resume contract
- `session resume --json` surfaces active reservations and next-work explanation
- `handoff --format json` is the serialized working-memory contract
- JSON and text surfaces should describe the same state in the same order
- critical recovery ergonomics belong in the core binary; repo-local wrappers can build on top of them
- the remote authority-command journal is opt-in and defaults to `off`; accepted remote decisions, never pending local requests, authorize its effects
