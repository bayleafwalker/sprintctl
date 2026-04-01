# sprintctl local operator customization plan

## Status

Proposed.

## Purpose

Define how `sprintctl` should integrate with personal workflows, repo-local tooling, and agent guidance **without embedding every habit into the core CLI**.

This document treats customization as a supported extension layer.

---

## Customization model

Use a four-layer model:

1. **core CLI** — truth, state transitions, JSON/text output
2. **repo-local workflow layer** — project-specific scripts, task runners, committed conventions
3. **user-local workflow layer** — aliases, shell functions, terminal/editor bindings
4. **agent guidance layer** — AGENTS.md conventions, prompt snippets, skill packs, wrapper scripts for agent runtimes

The goal is to keep layers 2–4 powerful while leaving layer 1 stable and explicit.

---

## Design rules

### Rule 1 — Never hide authoritative state changes in opaque glue

Wrappers are fine.
Hidden side effects are not.
A wrapper should still map clearly to real `sprintctl` commands.

### Rule 2 — Prefer wrappers for speed, not semantics

Use customization to reduce typing and bundle common flows.
Do not use customization to invent conflicting meanings for status, ownership, or handoff.

### Rule 3 — Keep machine-readable output available

Whenever possible, wrappers should preserve access to `--json` so agent runtimes and scripts can consume output safely.

### Rule 4 — Separate repo opinion from personal preference

Repo-local conventions should be committed and shared.
User-local shortcuts should remain personal unless they prove broadly useful.

---

## Repo-local customization patterns

## Pattern A — Task runner commands

Use `Justfile`, `Makefile`, `task`, or scripts to provide memorable entry points.

### Example

```make
resume:
	@sprintctl usage --context
	@echo
	@sprintctl next-work

snapshot:
	@sprintctl render > docs/sprint-status/current.md

stale:
	@sprintctl maintain check
```

### Good for

- shared project conventions
- documentation that points to stable entry points
- lowering typing cost without hiding behavior

### Avoid

- silently changing item state
- wrappers that discard useful command output

---

## Pattern B — Repo scripts for common bundles

A thin script can package the startup or shutdown path.

### Example

```bash
#!/usr/bin/env bash
set -euo pipefail

sprintctl usage --context --json
sprintctl next-work --json
sprintctl git-context --json
```

Suggested path:

```text
scripts/sprint/start-session.sh
```

### Good for

- agent session startup context
- editor tasks
- shell integration

---

## Pattern C — Committed rendered state

Keep live DB local, but commit rendered snapshots or handoff artifacts when they are meant to be shared.

### Good for

- diffable state history
- shared visibility in a repo
- PR-friendly progress tracking

### Suggested convention

```text
docs/sprint-status/current.md
```

---

## User-local customization patterns

## Pattern D — Shell aliases for inspection

Use aliases only for pure reads or obvious wrappers.

### Examples

```bash
alias sx='sprintctl usage --context'
alias sn='sprintctl next-work'
alias sg='sprintctl git-context --json'
alias ss='sprintctl maintain check'
```

### Good for

- high-frequency inspection
- low-risk ergonomics

### Avoid

- aliases that obscure destructive actions
- aliases with surprising defaults for ownership or status

---

## Pattern E — Shell functions for guided flows

Functions are better than aliases for multi-step flows.

### Examples

```bash
sx-resume() {
  sprintctl usage --context
  echo
  sprintctl next-work
}

sx-snapshot() {
  sprintctl render > docs/sprint-status/current.md
}

sx-agent-context() {
  sprintctl usage --context --json
  sprintctl next-work --json
  sprintctl git-context --json
}
```

### Good for

- startup and shutdown routines
- fast context capture
- feeding agent runtimes

---

## Pattern F — Fuzzy pickers and terminal glue

Use `fzf`, `skim`, tmux, zellij, or editor tasks to speed selection.

### Example concept

- select an item from `sprintctl item list --json`
- pipe the chosen ID into `sprintctl item show --id ...`

This should remain external glue, not core behavior.

---

## Agent guidance patterns

## Pattern G — AGENTS.md integration

Add repo guidance that tells agents how to orient before working.

### Example snippet

```md
## sprintctl operating expectations

Before starting meaningful work:

1. Run `sprintctl usage --context --json`.
2. Run `sprintctl next-work --json`.
3. If you are taking ownership of a live item, create a claim.
4. Record notable decisions or blockers with `sprintctl item note`.
5. Before ending the session, release or hand off any live claim and refresh the rendered snapshot if the repo expects one.
```

### Good for

- making the agent fit the repo instead of improvising
- standardizing safe behavior across sessions

---

## Pattern H — Skill or prompt snippets

Keep short reusable snippets for agent startup and shutdown.

### Startup snippet

```text
Use sprintctl as the local source of sprint state.
Start by collecting:
- sprintctl usage --context --json
- sprintctl next-work --json
- sprintctl git-context --json
If taking ownership of an item, create a claim and keep the token for the life of the claim.
Record important decisions, blockers, and lessons as item notes.
```

### Shutdown snippet

```text
Before ending work:
- update item state if needed
- record any important note or ref
- release or hand off active claims
- render or update shared snapshot artifacts if the repo expects them
```

---

## Pattern I — Agent wrapper scripts

A local wrapper can gather context and prepare a prompt bundle for a chosen runtime.

### Good for

- Codex / Claude / other runtime startup
- reducing repeated manual setup
- keeping prompt assembly reproducible

### Requirements

- wrapper output should be inspectable
- authoritative state stays in `sprintctl`
- wrapper must not invent fake ownership semantics

---

## Recommended starter pack

For the current product stage, the recommended supported customization set is:

### Repo-local

- one `start session` script
- one `render snapshot` task
- one AGENTS.md section for sprintctl expectations
- one sample task runner file

### User-local

- 3–5 inspection aliases
- 2–3 shell functions for resume, snapshot, and agent context
- optional fzf picker for item inspection

### Agent-facing

- one startup snippet
- one shutdown snippet
- one claim-discipline snippet for advanced use

This is enough to make the UX much stronger without product bloat.

---

## Promotion criteria: when external glue should become core

A wrapper or customization pattern should graduate into the core product only if:

1. it solves a recurring problem for many users
2. it preserves explicit semantics
3. it remains useful without repo-specific assumptions
4. it improves both human and automation use, or strongly improves one without harming the other
5. it cannot be handled cleanly by a wrapper plus docs

Examples that may eventually qualify:

- `claim start`
- `session resume`
- structured reason codes in `next-work --json`

Examples that usually should not qualify:

- personalized aliases
- shell-specific functions
- tmux/zellij bindings
- editor command definitions
- prompt text tuned to one runtime or one operator

---

## Risks

### Risk 1 — customization drift

Different repos or operators can create incompatible conventions.

Mitigation:

- define a small official example set
- keep AGENTS.md snippets short and aligned to the same core workflow

### Risk 2 — wrapper opacity

A convenience layer can make it harder to understand what actually happened.

Mitigation:

- keep wrappers thin
- prefer wrappers that simply bundle visible commands

### Risk 3 — core pressure from personal habits

Every fast local trick can start to look like a product requirement.

Mitigation:

- keep a clear promotion bar
- treat external customization as a supported success path, not second-class behavior

---

## Decision

Formalize customization as an intended UX layer around `sprintctl`.

The product should:

- publish example wrappers and integration patterns
- encourage repo-local and user-local speed layers
- keep the core CLI authoritative and explicit
- move convenience into core only when it proves broadly valuable and semantically safe
