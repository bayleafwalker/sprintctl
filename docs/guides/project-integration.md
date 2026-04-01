# Project Integration Guide

For the shortest path, start with [Start Here](start-here.md) and then use this
guide once you are wiring `sprintctl` into a real repository. For the current
read contracts, see [Context and Handoff Contracts](../reference/context-and-handoff.md).
If you need to integrate `sprintctl` with an external tracker, planner, or
orchestrator, also read [Interoperability Patterns](interoperability.md).

This guide covers how to use `sprintctl` inside a real repository, not just how to invoke the CLI.

The patterns here are based on the way a larger reference repo (`homelab-analytics`) uses `sprintctl`: local operational state, committed shared snapshots, and explicit claim-based coordination for agent sessions.

`sprintctl` should usually be the execution-memory layer inside the repo, not a
replacement for the surrounding issue tracker or planning system. Keep external
planning broad; mirror only the execution state that must remain local and
recoverable.

## Recommended Repo Shape

Keep the repository split between local operational state and committed shared artifacts.

Local-only, gitignored state:
- `.sprintctl/sprintctl.db`
- `handoff-*.json`
- `sprint-*.json`

Committed shared artifacts:
- `docs/sprint-snapshots/sprint-current.txt`
- `AGENTS.md` guidance telling agents how to use `sprintctl`
- optional runbooks describing the repo's sprint operating model

The database is the live control plane. The committed snapshot is the reviewable shared view.

## Source Of Truth Order

When sources disagree, use this order:

1. live `sprintctl` state for item status, claims, and recent events
2. committed `sprintctl render` output for the shared sprint view
3. repo process docs such as `AGENTS.md` and runbooks
4. sprint planning docs, briefs, and session notes

This prevents stale markdown from overriding current execution state.

## Baseline Setup

### 1. Scope the DB to the repo

Add this to `.envrc`:

```sh
export SPRINTCTL_DB="${PWD}/.sprintctl/sprintctl.db"
```

The repository already ships [envrc.example](../envrc.example) for this.

### 2. Ignore local state

Add this to `.gitignore`:

```gitignore
.sprintctl/
handoff-*.json
sprint-*.json
```

### 3. Commit the shared snapshot

Create a stable location for rendered sprint state, typically:

```text
docs/sprint-snapshots/sprint-current.txt
```

Add a small automation target:

```makefile
sprint-snapshot:
	mkdir -p docs/sprint-snapshots
	sprintctl render > docs/sprint-snapshots/sprint-current.txt
```

See [docs/examples/Makefile.sprintctl.mk](examples/Makefile.sprintctl.mk) for a copyable sample.

### 4. Teach agents the operating rules

Put a short `sprintctl` section in `AGENTS.md` so agents know:

- load `.envrc` before using the CLI
- consult live `sprintctl` state before repo docs when resuming sprint work
- claim sprint-scoped work before editing files when overlap is possible
- treat `claim_id + claim_token` as the only ownership proof
- refresh the shared snapshot after material sprint-state changes

See [docs/examples/AGENTS.sprintctl.md](examples/AGENTS.sprintctl.md) for a sample section.

## Working Loop

### Register scope

When accepted work needs tracking:

```sh
sprintctl sprint create --name "Sprint 4" --status active
sprintctl item add --sprint-id 1 --track docs --title "Document claim handoff flow"
sprintctl render > docs/sprint-snapshots/sprint-current.txt
```

### Resume or execute work

Before repo edits, inspect live state:

```sh
sprintctl item list --json
sprintctl item show --id 1 --json
sprintctl claim list-sprint --json
```

If the item is yours to execute, create a claim and keep its token:

```sh
sprintctl claim create \
  --item-id 1 \
  --actor codex-session-1 \
  --type execute \
  --ttl 600 \
  --runtime-session-id "${CODEX_THREAD_ID:-manual-session}" \
  --instance-id "$SPRINTCTL_INSTANCE_ID" \
  --json
```

Then move the item to active:

```sh
sprintctl item status \
  --id 1 \
  --status active \
  --actor codex-session-1 \
  --claim-id <claim-id> \
  --claim-token <claim-token>
```

### Record execution history while work happens

Use structured notes or events when a decision, blocker, or coordination lesson matters later:

```sh
sprintctl item note \
  --id 1 \
  --type decision \
  --summary "Use explicit claim handoff between live sessions" \
  --detail "Shared actor labels and branch names are advisory only; ownership proof is claim_id plus claim_token." \
  --tags claims,coordination \
  --actor codex-session-1
```

### Refresh the shared artifact after live state changes

Update the snapshot only after the DB state is correct:

```sh
mkdir -p docs/sprint-snapshots
sprintctl render > docs/sprint-snapshots/sprint-current.txt
```

### Hand off or release cleanly

If ownership itself changes:

```sh
sprintctl claim handoff \
  --id <claim-id> \
  --claim-token <claim-token> \
  --actor codex-session-2 \
  --mode rotate \
  --runtime-session-id "${CODEX_THREAD_ID:-manual-session-2}" \
  --instance-id "$NEXT_INSTANCE_ID" \
  --json > claim-handoff.json
```

If the next session only needs context, produce a broader bundle:

```sh
sprintctl handoff --output handoff-current.json
```

If work is done, release the claim:

```sh
sprintctl claim release --id <claim-id> --claim-token <claim-token> --actor codex-session-1
```

## Claim Rules Worth Writing Down

Projects that use multiple agents should repeat these rules in `AGENTS.md` or a runbook:

- claim before repo edits when the task already exists as a sprint item and overlap is possible
- never infer ownership from actor label, branch, worktree, or commit SHA alone
- only `claim_id + claim_token` proves ownership
- use `claim handoff` to transfer ownership, not `handoff`
- use `claim resume` to recover claims by identity after session restart
- refresh heartbeats around half-TTL for long-running sessions

## Suggested Minimal Project Bundle

If you want the shortest useful integration, add only these:

1. `.envrc` with `SPRINTCTL_DB`
2. `.gitignore` entry for `.sprintctl/`
3. `docs/sprint-snapshots/sprint-current.txt`
4. one `AGENTS.md` section describing live-state and claim rules
5. one `Makefile` target that renders the snapshot

That is enough to reproduce the strongest parts of the reference usage without importing its entire documentation structure.

## Bootstrap Prompts and Workflow Examples

To initialize `sprintctl` on a fresh repository using an agent session, see:

- [docs/examples/bootstrap-prompt.md](examples/bootstrap-prompt.md) — copy-paste prompt for agent onboarding
- [docs/examples/bootstrap-workflow.md](examples/bootstrap-workflow.md) — minimal walkthrough of the setup + work loop

For a complete worked example — including AGENTS.md, sprint naming conventions, all five workflow patterns (idea-to-backlog, direct implementation, review, knowledge promotion, fresh-repo bootstrap), and a sample rendered sprint — see the separate bootstrap template repository:

**[sprintctl-bootstrap-template](https://github.com/bayleafwalker/sprintctl-bootstrap-template)**

That repo demonstrates "what does good look like when starting from nothing?" It is designed to be forked and adapted, not read in-place.

If the repository already has another system for backlog or orchestration,
[Interoperability Patterns](interoperability.md) describes the boundary to keep.
