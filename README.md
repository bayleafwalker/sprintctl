# sprintctl

A local-first sprint state and handoff tool for a single developer and occasional agent sessions.

sprintctl tracks work items through enforced state transitions, records events and decisions, and produces plain-text and JSON artifacts that git can diff and agents can consume. It is not a project management platform, a team coordination layer, or a distributed task queue.

## What it is

- A local SQLite database of sprint state — items, tracks, events, claims
- A CLI for advancing work through enforced transitions (`pending → active → done | blocked`)
- A render command that produces a diffable plain-text sprint snapshot
- A handoff command that produces a JSON bundle for agent session resumption
- A claim system for coordinating exclusive access between you and one or two agents working the same sprint
- A maintenance layer (check, sweep, carryover) you invoke explicitly or schedule via cron

## What it is not

- Not a Jira/Linear replacement
- Not a multi-team coordination layer
- Not a distributed task queue or swarm orchestrator
- No web UI, no hosted dependency, no sync protocol
- No shared database — local state only

## Who it is for

One developer running medium- to long-term sprints, possibly with one or two agent sessions that pick up work, claim items, and hand off state between sessions. The tool is designed for sparse agentic use: an agent starts a session, reads state, claims an item, does work, records a note or decision, and either completes the item or hands off to the next session.

## Expected workflow

```sh
# Start a sprint — dates are optional; omit them for an open-ended execution container
sprintctl sprint create --name "Sprint 4" --status active
# Or with explicit dates if you want time-bound tracking:
sprintctl sprint create --name "Sprint 4" --start 2026-04-07 --end 2026-04-18 --status active

# Populate the backlog
sprintctl item add --sprint-id 1 --track backend --title "Implement auth"
sprintctl item add --sprint-id 1 --track infra --title "Set up CI pipeline"
sprintctl item add --sprint-id 1 --track docs --title "Write API reference"

# Check state at any time
sprintctl sprint show --detail
sprintctl maintain check

# Pick up an item and keep the returned claim token for the life of the claim
sprintctl claim create --item-id 1 --actor claude-session-1 \
    --runtime-session-id "${CODEX_THREAD_ID:-manual-session}" \
    --instance-id "codex-proc-1" \
    --branch feat/auth
# output includes:
# Claim #1 created: ...
# Claim token: <store-this-secret>

# Use the claim proof on owner-only operations
sprintctl item status --id 1 --status active --actor claude-session-1 \
    --claim-id 1 --claim-token <claim-token>

# Record decisions and notes during work
sprintctl item note --id 1 --type decision --summary "Using JWT with RS256" \
    --detail "Symmetric keys ruled out; need cross-service verification" \
    --tags auth,security --actor claude-session-1

# Complete or block the item, then release the claim
sprintctl item status --id 1 --status done --actor claude-session-1 \
    --claim-id 1 --claim-token <claim-token>
sprintctl claim release --id 1 --claim-token <claim-token> --actor claude-session-1

# Or explicitly hand the claim to the next live session
sprintctl claim handoff --id 1 --claim-token <claim-token> \
    --actor claude-session-2 --mode rotate \
    --runtime-session-id "${CODEX_THREAD_ID:-manual-session-2}" \
    --instance-id "codex-proc-2" --json > claim-handoff.json

# Produce a general sprint handoff bundle for the next session
sprintctl handoff

# Commit a plain-text snapshot for review and diffing
sprintctl render > docs/sprint-current.txt
git add docs/sprint-current.txt && git commit -m "chore: sprint snapshot"

# At sprint boundary: carry incomplete items forward
sprintctl sprint create --name "Sprint 5" --start 2026-04-21 --end 2026-05-02 --status active
sprintctl maintain carryover --from-sprint 1 --to-sprint 2
```

## Anti-goals

- No per-field policy sprawl — policies belong in a policy layer, not state objects
- No agent goodwill assumptions — transitions are enforced, not advisory
- No speculative orchestration primitives — claims coordinate two sessions, not a swarm

## Requirements

- Python 3.11+
- [click](https://click.palletsprojects.com/) (only non-stdlib dependency)

## Installation

```sh
pipx install git+https://github.com/bayleafwalker/sprintctl.git
```

sprintctl is developer tooling invoked at a project root — not a library imported by application code. Installing it globally via pipx keeps it out of your project's dependency graph and available across all projects without manual venv activation.

For local development:

```sh
python -m venv .venv && .venv/bin/pip install -e .
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

## Configuration

```sh
export SPRINTCTL_DB=/path/to/custom.db          # default: ~/.sprintctl/sprintctl.db
export SPRINTCTL_STALE_THRESHOLD=4              # active item staleness threshold in hours (default: 4)
export SPRINTCTL_PENDING_STALE_THRESHOLD=24     # pending item staleness in hours (unset = pending items never stale)
```

See [envrc.example](envrc.example) for a direnv template.

## Integration into projects

For a fuller project-level operating model, see [docs/project-integration.md](docs/project-integration.md). Copyable samples live in [docs/examples/AGENTS.sprintctl.md](docs/examples/AGENTS.sprintctl.md) and [docs/examples/Makefile.sprintctl.mk](docs/examples/Makefile.sprintctl.mk).

### Per-project database via direnv

Each project should have its own sprint database scoped to its working directory. Add this to `.envrc`:

```sh
export SPRINTCTL_DB="${PWD}/.sprintctl/sprintctl.db"
```

With `direnv allow`, the variable is set automatically whenever you enter the project directory.

### .gitignore

Add `.sprintctl/` to `.gitignore`:

```
.sprintctl/
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

### Recommended project conventions

The most effective repo integrations follow a simple split:

- live state in `.sprintctl/sprintctl.db` (gitignored)
- shared state in a committed snapshot such as `docs/sprint-snapshots/sprint-current.txt`
- repo guidance in `AGENTS.md` or a runbook that tells agents to consult live `sprintctl` state before editing files

Recommended precedence when sources disagree:

1. `sprintctl` live state for item status, claims, and recent events
2. committed `sprintctl render` output for the shared reviewable view
3. repo process docs such as `AGENTS.md` or runbooks

When multiple agents or worktrees may touch the same item, claim before editing files, keep heartbeats alive, and use `claim_id + claim_token` as the only ownership proof. Use `sprintctl claim handoff` when ownership itself changes; use `sprintctl handoff` when the next session just needs broader sprint context.

## Local-only design

sprintctl is explicitly local developer tooling. The database lives on your machine; there is no shared database, no sync protocol, and no hosted dependency.

- The database is a binary blob. Git cannot merge it. It belongs in `.gitignore`.
- The committed `sprintctl render` output is the reviewable, portable record — not the database.
- Export/import exists for migrating state between your own machines, not for sharing state with others.

## Commands

### Sprints

```sh
sprintctl sprint create --name <name> \
    [--start <YYYY-MM-DD>] [--end <YYYY-MM-DD>] \
    [--status <planned|active|closed>] [--goal <text>] [--kind <active_sprint|backlog|archive>]

sprintctl sprint show [--id <id>] [--detail] [--json]  # --detail adds health summary and track breakdown
sprintctl sprint list [--include-backlog] [--include-archive] [--json]
sprintctl sprint status --id <id> --status <planned|active|closed>
sprintctl sprint kind --id <id> --kind <active_sprint|backlog|archive>
sprintctl sprint backlog-seed --from-sprint-id <id> --to-sprint-id <id> [--actor <name>] [--json]
```

Sprint is a generic execution container. Dates are optional — omit them for open-ended work cycles. Status transitions are enforced: `planned → active → closed`. `closed` is terminal.

Sprint kinds classify a sprint's role — `active_sprint` (default), `backlog`, or `archive`. Sprints of kind `backlog` and `archive` are hidden from `sprint list` unless the corresponding flag is passed.

### Work items

```sh
sprintctl item add --sprint-id <id> --track <name> --title <title> [--assignee <name>]
sprintctl item show --id <id> [--json]
sprintctl item list [--sprint-id <id>] [--track <name>] [--status <pending|active|done|blocked>] [--json]
sprintctl item status --id <id> --status <pending|active|done|blocked> \
    [--actor <name>] [--claim-id <id>] [--claim-token <token>]
sprintctl item note --id <id> --type <type> --summary <text> [--detail <text>] [--tags <a,b>] [--actor <name>]
```

Item status transitions are enforced:

```
pending → active → done     (terminal)
                 → blocked → active   (revivable)
```

`done` is terminal. `blocked` is revivable — use `item status --status active` to unblock after addressing the issue.

If an active exclusive claim exists, `item status` requires the matching `--claim-id` and `--claim-token`. `--actor` is identity metadata for the event trail, not ownership proof.

`item show` displays a single item with its recent events and active claims. Use `--json` for machine-readable output.

`item note` records a structured event on a work item. Use it for decisions, blockers, architecture notes, and lessons learned. The `--type` value is freeform; see [Events](#events) for conventional types that the companion tool [kctl](https://github.com/bayleafwalker/kctl) recognizes.

`item note` accepts provenance fields for knowledge traceability:

```sh
sprintctl item note --id <id> --type decision --summary <text> \
    [--evidence-item-id <id>] [--evidence-event-id <id>] \
    [--git-branch <name>] [--git-sha <sha>] [--git-worktree <path>] \
    [--actor <name>]
```

### Item refs

Attach typed external references to an item:

```sh
sprintctl item ref add --id <item-id> \
    --type <pr|issue|doc|other> --url <url> [--label <text>]
sprintctl item ref list --id <item-id> [--json]
sprintctl item ref remove --id <item-id> --ref-id <ref-id>
```

Refs appear in `item show`, `render`, and handoff output.

### Item dependencies

Record blocking dependencies between items:

```sh
sprintctl item dep add --id <blocker-id> --blocks-item-id <blocked-id>
sprintctl item dep list --id <item-id> [--json]
sprintctl item dep remove --id <item-id> --dep-id <dep-id>
```

Items with unresolved blockers are excluded from `next-work` output. A blocked item becomes ready automatically once all its blockers reach `done`.

### Events

```sh
sprintctl event add --sprint-id <id> --type <type> --actor <name> \
    [--item-id <id>] [--source <actor|daemon|system>] [--payload <json>]

sprintctl event list --sprint-id <id> [--item-id <id>] [--type <type>] [--limit <n>] [--json]
```

Event type is freeform text. Conventional types recognized by [kctl](https://github.com/bayleafwalker/kctl) for knowledge extraction:

| Type | Meaning |
|------|---------|
| `decision` | Architectural or process decision |
| `blocker-resolved` | How a blocker was resolved |
| `pattern-noted` | Reusable pattern identified |
| `risk-accepted` | Explicit risk acceptance with reasoning |
| `lesson-learned` | Retrospective insight |
| `claim-handoff` | Explicit ownership transfer or token rotation between live sessions |
| `claim-ownership-corrected` | Legacy ambiguous claim adopted and re-issued with a valid token |
| `claim-ambiguity-detected` | sprintctl detected an ambiguous legacy claim with no proof token |
| `coordination-failure` | Wrong-token heartbeat/release, missing proof, or similar coordination mistake |

For knowledge-bearing events, structure the payload as:

```json
{
  "summary": "one-line description",
  "detail": "longer explanation",
  "tags": ["auth", "architecture"]
}
```

### Claims

Claims let you or an agent take ownership of a work item before working on it. An exclusive claim blocks other agents from transitioning the item until the claim is released or expires. Claims are a local coordination mechanism — they are meaningful only within a single database, not across machines or sessions that don't share state.

```sh
sprintctl claim create --item-id <id> --actor <name> \
    [--type <inspect|execute|review|coordinate>] [--ttl <seconds>] [--non-exclusive] \
    [--branch <name>] [--worktree <path>] [--commit-sha <sha>] [--pr-ref <owner/repo#123>] \
    [--runtime-session-id <id>] [--instance-id <id>] [--hostname <name>] [--pid <n>] [--json]

sprintctl claim heartbeat --id <claim-id> --claim-token <token> [--actor <name>] [--ttl <seconds>]
sprintctl claim release --id <claim-id> --claim-token <token> [--actor <name>]
sprintctl claim handoff --id <claim-id> [--claim-token <token>] --actor <next-actor> \
    [--mode <transfer|rotate>] [--ttl <seconds>] [--runtime-session-id <id>] [--instance-id <id>] \
    [--allow-legacy-adopt] [--json]
sprintctl claim list --item-id <id> [--all] [--json]
sprintctl claim list-sprint [--sprint-id <id>] [--all] [--expiring-within <seconds>] [--json]
```

Claim types:

| Type | Meaning |
|------|---------|
| `execute` | Default. Agent is actively working the item. Exclusive. |
| `inspect` | Agent is reading state. Non-exclusive. |
| `review` | Agent is reviewing completed work. Exclusive. |
| `coordinate` | Agent is orchestrating sub-agents on the item. Exclusive. |

Default TTL is 300 seconds. Refresh with `claim heartbeat` to keep a long-running claim alive. Expired claims are purged by `maintain sweep`.

#### Ownership proof vs advisory metadata

Ownership proof is always `claim_id + claim_token`.

- `claim_id` is the stable claim handle.
- `claim_token` is a server-minted opaque token returned on claim creation or explicit handoff.
- `runtime_session_id`, `instance_id`, `actor`, `branch`, `worktree_path`, `commit_sha`, `pr_ref`, `hostname`, and `pid` are identity context only.
- Shared metadata is never enough to heartbeat, release, or transition a claimed item.

Exclusive claim heartbeats and releases reject missing or wrong tokens. Legacy claims created before token support are surfaced as `legacy_ambiguous` in JSON and require an explicit adoption handoff or expiry.

#### Runtime and workspace metadata on claims

Claims can record the git context in which an agent is working:

```sh
sprintctl claim create --item-id 3 --actor claude-session-2 \
    --runtime-session-id "${CODEX_THREAD_ID:-manual-session}" \
    --instance-id "codex-proc-2" \
    --branch feat/auth --commit-sha abc1234 --pr-ref owner/repo#42
```

The `--worktree` flag records the path to a git worktree if the agent is working in one. `--hostname` and `--pid` default to the current process. `--runtime-session-id` should come from the runtime when available. For Codex sessions, `CODEX_THREAD_ID` is accepted automatically when `--runtime-session-id` is omitted.

Recommended client behavior:

- Prefer a runtime-provided session ID when the host runtime exposes one.
- Mint a stable per-process `instance_id` and reuse it for every `claim create`, `claim heartbeat`, and `claim handoff` call from that process.
- Store `claim_token` securely for the entire life of the claim.
- Treat branch, worktree, commit SHA, and similar fields as hints for the next session, not as proof of ownership.

Concurrent agents in the same worktree:

```sh
# Session A
sprintctl claim create --item-id 9 --actor codex-a \
    --runtime-session-id thread-a --instance-id proc-a \
    --worktree /repo --commit-sha abc1234

# Session B — same actor and same git metadata still conflicts without the token
sprintctl claim create --item-id 9 --actor codex-a \
    --runtime-session-id thread-b --instance-id proc-b \
    --worktree /repo --commit-sha abc1234
```

The second command is rejected because shared workspace metadata does not prove ownership.

### Context surface

```sh
sprintctl usage [--context] [--sprint-id <id>] [--json]
```

Without `--context`, prints a compact command reference. With `--context`, emits
the current sprint state as a one-shot summary: sprint goal, item counts, active
claims, stale/blocked items, ready-to-start items, and recent knowledge candidates.
Designed to be injected into an agent prompt at session start without summarisation.

```sh
# See what's unblocked and ready to pick up
sprintctl next-work [--sprint-id <id>] [--json]

# Print current git context (branch, sha, worktree)
sprintctl git-context [--json]
```

### Sprint backlog seeding

```sh
sprintctl sprint backlog-seed \
    --from-sprint-id <source-id> \
    --to-sprint-id <target-backlog-id> \
    [--actor <name>] [--json]
```

Seeds knowledge candidate events from the source sprint as items into the target
sprint's `knowledge` track. Idempotent — re-running is safe. Source events must
be of type `decision`, `pattern-noted`, `lesson-learned`, or `risk-accepted`.

### Handoff

```sh
sprintctl handoff [--sprint-id <id>] [--output <path>] [--events <limit>] [--format <json|text>]
```

Produces a bundle for agent session resumption. `--format json` (default) produces
a machine-parseable envelope containing the sprint, all items, recent events, and
active claims. `--format text` produces a human-readable summary with status groups,
active claims, and a shutdown protocol checklist.

Default output file: `handoff-N.json`. Pass `--output -` to write to stdout.

General sprint handoff bundles surface claim identity state, including whether a claim is `proven` or `legacy_ambiguous`, but they do not include `claim_token`. Use `sprintctl claim handoff` when ownership itself needs to move to the next live session.

Typical agent session start:

```sh
sprintctl handoff --output handoff.json
# Pass handoff.json as context to the next agent session
```

### Maintenance

```sh
sprintctl maintain check [--sprint-id <id>] [--threshold <Nh>] [--json]
sprintctl maintain sweep [--sprint-id <id>] [--threshold <Nh>] [--auto-close]
sprintctl maintain carryover --from-sprint <id> --to-sprint <id>
```

`maintain check` is a read-only diagnostic — it reports stale items, track health, and sprint overrun risk without writing anything. Safe to run at any time; the companion tool [kctl](https://github.com/bayleafwalker/kctl) calls it as a pre-flight before knowledge extraction.

`maintain sweep` executes: stale active items are transitioned to `blocked` with a system event, and expired claims are deleted. With `--auto-close`, an overdue sprint with no remaining active items is closed automatically.

`maintain carryover` moves all incomplete items (`pending`, `active`, `blocked`) from the source sprint to the target sprint. Each original item is marked `done` with a carryover payload. New items are created in the target sprint preserving track and title.

The staleness threshold defaults to 4 hours and can be overridden per-invocation with `--threshold 2h`, or globally via `SPRINTCTL_STALE_THRESHOLD`.

These commands are regular CLI invocations — schedule them externally if you want automated sweeps:

```sh
# cron example
*/30 * * * * sprintctl maintain sweep 2>&1 | logger -t sprintctl
```

### Export / Import

```sh
sprintctl export --sprint-id <id> [--output <path>]   # default: sprint-N.json
sprintctl import --file <path>
```

Export writes a JSON envelope containing the sprint, tracks, items, and events. Import re-sequences all IDs into the local database. Use this for migrating state between your own machines, not for sharing state with others.

### Rendering

```sh
sprintctl render [--sprint-id <id>]
```

Output is derived entirely from database state. Idempotent. Re-running is safe. Includes staleness annotations and track health summaries. Pipe to a file and commit it for a diffable record.

## Architecture

```
sprintctl/
  db.py       — schema, migrations, all data access; transition enforcement via
                InvalidTransition and ClaimConflict; VALID_TRANSITIONS and
                SPRINT_TRANSITIONS are the single source of truth for allowed changes
  cli.py      — Click entry point; thin dispatch only, no business logic
  calc.py     — pure functions: item_staleness, track_health, sprint_overrun_risk;
                no DB calls, no side effects, `now` always passed explicitly
  render.py   — plain-text sprint doc; calls calc for staleness annotations and
                track health summaries
  maintain.py — check, sweep, carryover logic; all writes go through db.py
tests/
  conftest.py      — shared fixtures (in-memory DB)
  test_core.py     — schema, transitions, core workflow
  test_claims.py   — claim layer and CLI integration
  test_calc.py     — calc function unit tests
  test_maintain.py — maintenance command tests
```

Key architectural decisions:

- **No daemon** — calculate-on-call: all derived state computed at read time in `calc.py`
- **Maintenance = explicit CLI commands** — not sweep loops
- **Transition enforcement lives in db.py** — not cli.py; both `InvalidTransition` and `ClaimConflict` are raised there
- **WAL mode enabled** — allows concurrent reads from multiple local sessions alongside CLI writes
- **DB path**: `SPRINTCTL_DB` env var or `~/.sprintctl/sprintctl.db`
- **No ORM** — sqlite3 stdlib only; rows returned as dicts
- **`render_sprint_doc()` is a pure function** — timestamp passed in

## Companion tool

[kctl](https://github.com/bayleafwalker/kctl) is a separate tool that reads the sprintctl database (read-only, never writes) and extracts durable knowledge from sprint events. It operates on its own storage and calls `sprintctl maintain check` as a pre-flight before extraction.

Claim ambiguity, explicit claim handoffs, ownership corrections, and coordination failures are emitted as structured sprint events so kctl can preserve the identity context and turn repeated mistakes into durable lessons.

sprintctl and kctl share a read path but not a write path. kctl never transitions item or sprint status, never creates claims, and never modifies the sprintctl database.
