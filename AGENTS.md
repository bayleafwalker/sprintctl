# sprintctl — Agent Integration Guide

> **Environment reference:** `/projects/dev/AGENTS.md` — devbox vs workstation context, tool install persistence, cluster access, direnv, PATH, cost logging, and mid-session switching.


## Tech Stack

Primary language: Python. Use `pytest` for testing. Markdown for documentation. Package manager: `uv` / `pipx`.

## Environment setup

### Required environment variables

| Variable | Purpose |
|---|---|
| `SPRINTCTL_INSTANCE_ID` | Optional session metadata; never a credential |
| `SPRINTCTL_RUNTIME_SESSION_ID` | Runtime session ID (auto-detected from `CODEX_THREAD_ID`) |
| `SPRINTCTL_DB` | Override the database path (default: `~/.sprintctl/sprintctl.db`) |

**Validate before use:**
```bash
echo $SPRINTCTL_DB   # for project-scoped work, must contain the project path, not ~/
```

> Using the home-directory default (`~/`) silently operates on the wrong database when working within a project that has its own `.sprintctl/` directory.

No cluster context — sprintctl is a local-first CLI tool.

## Development workflow

- Run `pytest` after making changes. Report pass/fail count before committing.
- **Never commit with failing tests.**
- **Commit after each sprint item completes — not at the end of a session.** One item = one commit. Run tests before each commit.
- Behavior changes must include updated or new tests in the same commit.

### Self-healing test loop

If tests fail after a change, diagnose the root cause, fix, and re-run — up to **5 cycles** — before escalating. Only escalate if still failing after 5 attempts or if a design decision is required.

---

sprintctl is a local sprint coordination CLI backed by a SQLite database.
It uses an **advisory reservation system** to make coordination visible among
agent sessions.  Read this file before touching any sprint item.

---

## Quick reference

```
sprintctl agent-protocol          # print full lifecycle protocol (human-readable)
sprintctl agent-protocol --json   # machine-readable JSON version
```

If your global `sprintctl` binary is stale and missing commands documented in
this file, run the repo-local source entrypoint instead:

```bash
.venv/bin/python -m sprintctl <command> ...
```

Keep global tool installs fresh before longer sessions:

```bash
# Preferred when pipx is available
pipx upgrade sprintctl && pipx upgrade kctl

# Equivalent uv tool flow
uv tool upgrade sprintctl kctl
```

---

## Reservation lifecycle (summary)

A reservation is a visible coordination signal, not a capability. Multiple
sessions may hold active reservations on the same item; conflicts are
operator-visible rather than enforced.

### 1. Startup — reserve the item

```bash
sprintctl reservation reserve \
  --item-id <id> --actor <your-name> \
  --role execution \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID" \
  --json
```

Save `reservation_id` from the response. There is no token, no secret, and no
recovery file.

The reservation response also carries the item's refs. Read every governing doc
ref before editing files, and pin the executed revision as described in
`docs/reference/doc-refs.md`.

The role is the relationship to the work: `execution` (doing it),
`verification` (reviewing or testing it), `observation` (watching it). That is
what makes an overlap readable — two `execution` reservations are worth
coordinating over, `execution` beside `verification` is ordinary.

If somebody else already holds a reservation, yours is still created. The
response carries `conflict`, `conflicting_reservations`, and
`conflict_severity`; read it and coordinate rather than assuming you are alone.
Nothing refuses you, because refusing you would only remove you from the
ledger, not from the work.

To deliberately displace an execution reservation — a stalled session, a
takeover you have agreed — add `--interrupt-existing`. It interrupts the
item's active execution reservations, records `interrupted by <actor>
(<session>)`, and emits a durable audit event. Verification and observation
reservations are left alone.

**Coordinators** (orchestrators spawning sub-agents): reserve with
`--role observation`. Orchestration is session and project context, not a
relationship to the item, so a coordinator observes the work it coordinates.
Sub-agents reserve with `--role execution` on the same item.

### 2. Activity — touch when useful

```bash
sprintctl reservation touch \
  --id <reservation_id> \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID"
```

`last_activity_at` also advances on its own whenever your session
successfully mutates the item (status, edit, note, ref, dep, item-scoped
events), so `touch` is for work that happens outside sprintctl — long external
or git-only stretches. Attribution is by session id, never by actor name, and
it works the same in served mode: the client attaches its session to the
invocation, since the server cannot see it.

Touch bumps `last_activity_at`. There is no lease, no TTL, and no heartbeat
contract to violate. Staleness is display-only.

### 3. Transition item status

```bash
sprintctl item status \
  --id <item_id> --status active|done|blocked \
  --actor <your-name> \
  --expected-revision <revision>
```

Status transitions are protected by expected-revision compare-and-swap, not by
reservation proof. Read the current `status_revision` from `item show --json`
before mutating.

### 4. Handoff — reassign when work continues

```bash
# Reassign the advisory reservation to the incoming session
sprintctl reservation reassign \
  --id <reservation_id> \
  --actor <next-agent-name> \
  --session-id <next-session-id> \
  --json

# Produce a sprint handoff bundle for the incoming session
sprintctl handoff [--sprint-id N] [--output path] [--format json|text]
```

`reservation reassign` changes the reserving actor/session. `sprintctl handoff`
produces a working-memory bundle; it does not carry ownership proof because
there is none.

`--format text` produces a human-readable bundle (status groups, active
reservations, shutdown protocol). `--format json` (default) produces the
machine-parseable bundle for agent session resumption.

### 5. Release — when work is done

```bash
sprintctl reservation release \
  --id <reservation_id> --actor <your-name>
```

---

## Session resumption (context loss recovery)

If you restart, there is no token to recover. List reservations and reassign or
reserve as appropriate:

```bash
# Find reservations by item or list all active reservations
sprintctl reservation list --item-id <id> --json
sprintctl reservation list --all --json

# Reassign an existing reservation to the current session, or release and
# create a new one if the old session is gone.
sprintctl reservation reassign \
  --id <reservation_id> \
  --actor <your-name> \
  --session-id <current-session-id> \
  --json
```

Reservations contain no recoverable credential.

---

## Shutdown checklist

Before terminating:

1. For each active reservation: **reassign** to the next session _or_ **release** it.
2. Run `sprintctl handoff` to write a bundle for the incoming session.
3. The bundle's `agent_shutdown_protocol` field repeats these instructions.

---

## Ownership model

- There is no ownership proof. `reservation_id` is a handle, not a secret.
- `instance_id`, `hostname`, `pid`, `actor` name, branch, worktree, and commit SHA
  are advisory metadata only.
- The reservation model is advisory: conflicting reservations are detected and
  surfaced, not prevented.
- Status transitions are gated by expected-revision CAS (`item:<uuid>@status:<status>`).

---

## Reading current sprint context

Before picking up work, read the current state in one call:

```bash
sprintctl usage --context [--sprint-id N] [--json]
```

This emits: sprint summary, active reservations (who is working on what),
stale/blocked items, ready-to-start items (no unresolved deps), and recent
knowledge candidates.

Use `--json` for machine-readable output — compact enough to paste into a prompt
without summarisation.

```bash
# See what's ready to pick up
sprintctl next-work [--sprint-id N] [--json] [--explain]

# See your current git context (branch, sha, worktree)
sprintctl git-context [--json]
```

---

## Refs and deps

### Repository-scoped references

On shared `remote` or `served` state, cite an item as `repo#id` (for example,
`sprintctl#1984`) in plans, handoffs, and chat. `item show`, `item status`,
`item add`, `event add`, and `sprint show` accept that form. A bare numeric ID
is only safe when the repository marker already establishes the scope; never
infer another repository from a similarly numbered item.

An item is shaped only when it has a governing doc ref or an explicit `No doc:`
decision note. Follow `docs/reference/doc-refs.md`; agents never set a doc to
`ratified` themselves.

```bash
# Attach a governing repo doc while shaping
sprintctl item ref add --id <item-id> --type doc \
  --url docs/plans/<plan>.md --label <doc-id>

# Before implementation, pin the exact revision in a second ref label
sprintctl item ref add --id <item-id> --type doc \
  --url docs/plans/<plan>.md --label <doc-id>@git:<full-commit-sha>

# Attach other evidence and inspect/remove refs
sprintctl item ref add --id <item-id> --type pr --url <url> [--label <text>]
sprintctl item ref list --id <item-id> [--json]
sprintctl item ref remove --id <item-id> --ref-id <ref-id>
```

Record blocking dependencies between items:

```bash
# item-A must finish before item-B can start
sprintctl item dep add --id <item-A-id> --blocks-item-id <item-B-id>
sprintctl item dep list --id <item-id> [--json]
sprintctl item dep remove --id <item-id> --dep-id <dep-id>
```

Items with unresolved blockers are excluded from `next-work` output.

---

## Recording git context on notes

`item note` accepts git provenance fields so knowledge candidates carry their origin:

```bash
sprintctl item note --id <item-id> --type decision \
  --summary "Chose RSA over ECDSA for compatibility" \
  --git-branch feat/auth --git-sha abc1234 \
  --evidence-item-id <related-item-id> \
  --actor <your-name>
```

---

## Capability receipt at sprint close

For an intentional sprint close, first run the close gate, read the current
`sprint show --json` `status_revision`, then close explicitly with
`sprintctl sprint status --id <id> --status closed --actor <actor> --expected-revision <revision> --json`.
The status change and one local `sprint-close-boundary` event commit atomically;
the JSON response returns `boundary_event_id` and its database-local
`boundary_revision` (`event:<id>`). That reference depends on preserving the
database, event row, and project/sprint mapping; it is not migration-stable.

Only after that boundary exists should the operator decide whether capability
changed. A supported delta invokes the `capability-receipt` dispatch skill; a
routine close records an evidence-backed no-receipt decision. Drafts live under
`/projects/dev/_artifacts/<repo-id>/capability/receipts/`, while sprint state
stores only the canonical pointer and SHA-256 digest. Agents draft but never
ratify or publish receipts; operator-directed ratification is an external,
append-only procedural assertion rather than authenticated identity. An
`--auto-close` maintenance sweep emits no capability boundary. See
[Capability receipts at sprint close](docs/reference/capability-receipts.md).

---

## Stateful protocol verification

Routing and hooks are declared in `sprintctl.dispatch.json`; closed subjects
and escalation rules live in `.agents/overlays/sprintctl.state-protocols.md`.
Use `verify-state-protocols` for reservations, retries, idempotency,
reconciliation, append-only histories, canonical projections, crash recovery,
dual writes, concurrent workers, or SQLite/PostgreSQL parity. `survey` and
`reconcile` are read-only; product repair requires separate authorization. Run
concurrent histories only against temporary SQLite databases and disposable
PostgreSQL repository scopes.

## Hybrid dispatch

Only `mechanical_bulk` packets are worker-eligible: low-risk implementation
against frozen interfaces, a coordinator-owned oracle the worker cannot
modify, and explicit registered gates that fail for each relevant incorrect
behaviour. One rejected attempt returns to the coordinator.

Parity fixtures, test-oracle construction, tests as the primary deliverable,
SQLite/PostgreSQL behavioural proof, and reservation, authority, compatibility,
migration, recovery, or credential semantics are coordinator-only regardless
of diff size.

## Environment variables

| Variable | Purpose |
|---|---|
| `SPRINTCTL_INSTANCE_ID` | Optional session metadata only; never a credential |
| `SPRINTCTL_RUNTIME_SESSION_ID` | Runtime session ID (auto-detected from `CODEX_THREAD_ID`) |
| `SPRINTCTL_DB` | Override the database path |

<!-- agentops-project-pointer:start -->
See `.agents/project.generated.md` for cross-repo project context (agentops-managed; do not hand-edit).
<!-- agentops-project-pointer:end -->

<!-- agentops-environment-pointer:start -->
See `.agents/environment.generated.md` for the active Vuoro environment's constraints and runbooks (agentops-managed; do not hand-edit).
<!-- agentops-environment-pointer:end -->
