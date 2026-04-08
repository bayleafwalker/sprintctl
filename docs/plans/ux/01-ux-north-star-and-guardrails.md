# sprintctl UX north star and guardrails

## Status

Proposed.

## Purpose

Define a UX direction for `sprintctl` that preserves the current strengths:

- local-first operation
- CLI as the authoritative interface
- explicit state transitions
- explicit coordination semantics
- machine-readable context for agents
- compatibility with repo-local and user-local workflow customization

This plan assumes the current product identity is correct:

> `sprintctl` is a local-first sprint state and handoff tool for a single developer with optional agent sessions.

That identity should be sharpened, not broadened.

---

## Core product statement

`sprintctl` should feel like:

- a **local sprint ledger** for a solo operator
- a **resumable work context tool** across sessions
- a **shared coordination surface** when one or more agent sessions are involved

`sprintctl` should not feel like:

- a team project manager
- a ticketing platform
- a distributed orchestrator
- a UI-heavy planning suite
- a magic assistant that hides the work model

---

## Golden goose: what must not be harmed

The following qualities are the core of the existing workflow story and must be preserved.

### 1. The CLI remains the source of truth

All meaningful state transitions, coordination rules, and machine-readable output stay in the CLI.
Any future TUI or wrapper must be an optional convenience layer only.

### 2. The core model stays explicit

Claims, transitions, blockers, notes, refs, handoff bundles, and maintenance remain visible concepts.
UX must reduce friction without pretending these concepts do not exist.

### 3. JSON and text output remain stable operator surfaces

The app must keep serving two primary consumers:

- human operators in a terminal
- local automation / agent sessions consuming `--json`

### 4. No daemon-first UX

The app should not require background services, live panes, or ambient automation to feel complete.
Explicit invocation remains the default mode.

### 5. Customization belongs mostly outside the binary

Aliases, shell functions, repo wrappers, prompt templates, AGENTS.md conventions, and skill packs should carry a large share of ergonomic customization.
The binary should expose stable primitives, not absorb every convenience idea.

---

## UX thesis

The path to a stronger UX is **progressive disclosure**, not feature inflation.

This means:

- the default path focuses on solo operation and quick session restart
- single-agent collaboration is a thin extension of the same path
- coordinator / sub-agent workflows are clearly labeled as advanced
- local customization is treated as a first-class support surface
- docs are organized by operating mode, not just by command family

In practice, users should be able to succeed at three levels:

### Level 1 — Solo operator

Goal: start work quickly, resume work cleanly, and finish without losing context.

Primary commands:

- `usage --context`
- `next-work`
- `item show`
- `item note`
- `item status`
- `render`

Claims are optional here.

### Level 2 — Solo operator + one agent

Goal: use the same workflow but add a safe handoff and a shared context surface.

Primary additions:

- `claim create`
- `claim release`
- `handoff`
- `claim handoff` when ownership itself moves

### Level 3 — Coordinator + sub-agents

Goal: use explicit coordination safely without turning the default story into a cockpit manual.

Primary additions:

- `claim create --type coordinate`
- claim lifecycle discipline
- explicit runtime / worktree metadata
- stronger handoff and audit patterns

---

## UX design principles

### Principle 1 — Narrow the first path

The first hour should not require understanding every feature.
A new operator should be able to run a sprint with a minimal command slice and only encounter advanced concepts when needed.

### Principle 2 — Preserve protocol, reduce ceremony

Do not remove coordination correctness.
Instead, introduce wrappers, examples, documentation paths, and convenience flows that package the existing rules into easier habits.

### Principle 3 — Default to operator reality

The most common use case is not “agent swarm commander.”
The most common use case is a single developer who needs:

- a reliable restart point
- explicit work state
- an audit trail of decisions
- occasional agent support

Design around this first.

### Principle 4 — Make customization an intended extension point

Local shell aliases, repo task runners, agent prompt templates, and workflow wrappers should be documented and encouraged.
That is not a workaround. It is part of the product strategy.

### Principle 5 — Keep advanced modes honest

Coordinator/sub-agent behavior should be explicit and well-supported, but presented as an advanced coordination mode rather than the default mental model.

---

## UX surfaces

### Surface A — Core CLI

Purpose:

- authoritative operations
- stable JSON/text output
- explicit state changes

Allowed responsibilities:

- state transitions
- claims and handoff semantics
- context summaries
- validation and diagnostics
- export / render / carryover

Not preferred here:

- personalized shortcuts
- workflow opinion presets tied to one user's habits
- shell ergonomics that can live outside the tool

### Surface B — Official examples and workflow guides

Purpose:

- narrow the first path
- show recommended habits
- provide operating-mode quickstarts

Examples:

- solo operator quickstart
- solo + one agent quickstart
- coordinator mode quickstart
- first 10 minutes in a fresh repo
- session restart after interruption

### Surface C — Repo-local integration

Purpose:

- align `sprintctl` with real project workflows
- connect snapshots, docs, scripts, agent guidance, and git hygiene

Examples:

- `make sprint-context`
- `just next`
- scripts for repo-local startup checks
- committed rendered snapshots
- AGENTS.md conventions that pull in `usage --context --json`

### Surface D — User-local customization

Purpose:

- allow operator-specific speed without polluting the core product

Examples:

- shell aliases and functions
- fzf pickers
- tmux / zellij bindings
- local startup scripts
- editor tasks
- prompt templates for agent runtimes

---

## Product boundaries

### Keep in core

- stable command verbs and flags
- clear status and claim semantics
- `--json` support on important commands
- context and handoff bundles
- diagnostics and maintenance primitives
- documentation for operating modes

### Prefer outside core

- heavily personalized command aliases
- environment-specific wrappers
- interactive fuzzy pickers
- terminal keybindings
- editor integration glue
- agent prompt assembly scripts
- repo-specific workflow recipes

### Add to core only if all are true

A convenience feature belongs in core only if it:

1. materially improves the default path for many users
2. preserves explicit semantics
3. remains scriptable and inspectable
4. reduces recurring operator error or friction
5. cannot be cleanly solved by repo-local or user-local wrappers

---

## Target UX outcomes

### Outcome 1 — The default story becomes obvious

A user should understand within minutes that the primary workflow is:

1. orient
2. pick work
3. update work state
4. capture notes
5. render or hand off

### Outcome 2 — Advanced coordination stops leaking into the beginner path

A solo operator should not feel forced to learn claim rotation and coordinator claims on day one.
Those concepts should appear only when needed.

### Outcome 3 — Customization becomes part of the official story

The product should explicitly support a layered setup:

- core CLI for truth
- repo wrappers for team or project shape
- local wrappers for personal flow
- agent guidelines for runtime behavior

### Outcome 4 — Session restart becomes a standout strength

The strongest lived UX should be:

- stop abruptly
- come back later
- regain context in under a minute
- continue without archaeological work

---

## Non-goals

This plan does not propose:

- converting `sprintctl` into a team collaboration platform
- replacing the CLI with a TUI
- adding daemonized background coordination
- embedding per-user workflow magic in the database model
- weakening claim or transition correctness for convenience

---

## Risks

### Risk 1 — Over-correcting into “friendliness”

A UX push can accidentally erase the explicit protocol that makes the tool trustworthy.

Mitigation:

- keep advanced mechanics explicit
- reduce ceremony via wrappers and docs, not hidden side effects

### Risk 2 — Adding too many first-class convenience commands

The tool can bloat into a personal shell profile with delusions of grandeur.

Mitigation:

- treat customization as an external extension layer by default
- require a high bar before promoting helpers into core

### Risk 3 — Letting advanced agent workflows define the whole product identity

This would distort docs, onboarding, and defaults around a narrower use case.

Mitigation:

- center solo operator + optional agent as the primary story
- frame coordinator mode as advanced

---

## Decision

Adopt a layered UX strategy:

1. **refine the default solo-operator journey**
2. **treat solo + agent as the natural extension**
3. **document coordinator / sub-agent mode as advanced**
4. **formalize repo-local and user-local customization as intended UX layers**
5. **keep the core binary strict, composable, and explicit**

---

## Acceptance criteria

This plan is succeeding when:

- a new operator can complete a first sprint without learning advanced claim flows
- a solo operator can resume an interrupted session quickly using the recommended path
- repo maintainers have clear templates for local integration
- power users can customize speed locally without requesting product-level changes for every habit
- coordinator workflows remain available and correct, but no longer dominate the default story
