# sprintctl UX journeys and phased delivery plan

## Status

Proposed.

## Purpose

Provide a plan-ready UX delivery path that strengthens the real user journeys around `sprintctl` while preserving the current CLI-first, explicit workflow model.

This document is intended to feed sprint planning directly.

---

## Delivery model

The plan is split into four phases:

1. clarify operating modes
2. narrow onboarding and startup
3. improve day-to-day working flow
4. formalize customization and advanced coordination guidance

The recommendation is to ship each phase as a docs-first and examples-first slice before considering any new core commands.

---

## User journeys

## Journey A — Fresh operator in a fresh repo

### Desired feeling

“I understand the default way to start without reading a novella.”

### Current pain

The command set and work model are coherent but broad. A newcomer meets the whole system quickly: claims, handoff, maintenance, refs, deps, knowledge, and coordination.

### Target path

1. initialize or point to an existing sprint database
2. create one sprint
3. add one or two tracks
4. add a few items
5. run a context command
6. pick ready work
7. update item state
8. record one note
9. render a snapshot

### Deliverables

- `docs/quickstart-solo.md`
- `docs/first-10-minutes.md`
- README restructure so the first path is solo-first
- one compact command cheat sheet for the default path

### Acceptance criteria

- a new user can reach meaningful work state with a narrow command set
- advanced claim and coordinator flows are not required reading

---

## Journey B — Solo operator resuming after interruption

### Desired feeling

“I can get back into work without excavating my own ruins.”

### Current pain

The pieces exist, but the resume flow is distributed across context, next-work, item show, render, and optional handoff.

### Target path

1. run a single documented resume sequence
2. see current sprint state, blocked items, active items, and ready work
3. open the relevant item
4. continue work with minimal decision latency

### Deliverables

- `docs/resume-work.md`
- recommended shell wrapper examples such as `sx-resume`
- repo-local task examples (`just resume`, `make resume`)
- guidance for passing `usage --context --json` to agent runtimes

### Acceptance criteria

- resume guidance fits on one page
- operator can re-enter a sprint in under a minute using the documented path

---

## Journey C — Solo operator with one agent

### Desired feeling

“The agent plugs into my workflow instead of demanding a new religion.”

### Current pain

The tool supports this mode, but the docs can still make it feel like the user must adopt the full coordination model.

### Target path

1. operator or agent captures current context
2. agent claims one item when needed
3. notes and refs are added during work
4. agent releases or hands off cleanly
5. operator resumes with a clear audit trail

### Deliverables

- `docs/quickstart-agent-assisted.md`
- example AGENTS.md snippet for `sprintctl`
- prompt template examples using `usage --context --json`, `next-work --json`, `item show --json`
- lightweight claim lifecycle examples focused on a single live agent

### Acceptance criteria

- single-agent collaboration is documented as an extension of the solo path
- coordinator/sub-agent language is absent from the beginner agent flow

---

## Journey D — Coordinator with sub-agents

### Desired feeling

“I can coordinate safely without infecting every other workflow with ceremony.”

### Current pain

Advanced coordination semantics exist, but the product story risks over-indexing on them.

### Target path

1. coordinator explicitly adopts advanced mode
2. coordination claims are used intentionally
3. runtime metadata and handoff discipline are documented clearly
4. the mode is isolated from beginner docs

### Deliverables

- `docs/advanced/coordinator-mode.md`
- `docs/advanced/claim-discipline.md`
- explicit “when to use coordinator mode” guidance
- examples for multi-session worktree usage and rotation

### Acceptance criteria

- advanced docs are strong and honest
- advanced mode no longer distorts the default story

---

## Journey E — Knowledge capture and review loop

### Desired feeling

“Important learning survives the sprint without extra bureaucracy.”

### Current pain

The model is present, but the UX is still closer to “you can do this” than “this fits naturally into normal work.”

### Target path

1. operator records structured notes while working
2. review path identifies durable knowledge
3. backlog seed feeds the next sprint or knowledge track
4. the path feels like a continuation of work, not separate overhead

### Deliverables

- `docs/knowledge-while-working.md`
- examples that connect item notes, review, `kctl`, and backlog seeding
- examples of note type conventions per work style

### Acceptance criteria

- knowledge capture fits within the standard loop
- users understand when to record a note versus when to create a new item

---

## Phased delivery

## Phase 1 — Reframe the product around operating modes

### Goal

Make the default story obvious without changing behavior.

### Scope

- rewrite README intro and command path around three operating modes:
  - solo operator
  - solo + one agent
  - coordinator + sub-agents
- reorganize docs links around journeys instead of only concepts
- add a “start here” page

### Deliverables

- `docs/start-here.md`
- README operating-mode section
- quickstart pages for solo and agent-assisted use

### Out of scope

- new commands
- TUI work
- workflow wrappers inside the binary

### Validation

- a new user can discover the solo path directly from README and docs nav

---

## Phase 2 — Build the startup and resume path

### Goal

Make startup and re-entry the standout UX strength.

### Scope

- document first 10 minutes
- document resume flow
- document repo-local startup wrappers
- add examples for shell and task runner integration

### Deliverables

- `docs/first-10-minutes.md`
- `docs/resume-work.md`
- `docs/examples/justfile.md`
- `docs/examples/shell-wrappers.md`

### Optional core work

Only if docs and wrappers still leave real friction:

- consider a thin convenience command such as `session resume`
- consider a thin convenience command such as `claim start`

These must preserve explicit semantics and return inspectable JSON.

### Validation

- experienced users report lower friction during session restart
- no core semantics are hidden or weakened

---

## Phase 3 — Strengthen in-flow working ergonomics

### Goal

Make normal operation smoother without bloating the model.

### Scope

- standardize examples for common daily flows
- publish alias/wrapper patterns for fast note-taking, claiming, and rendering
- improve examples for item refs and knowledge notes
- provide recommended command bundles for human and agent usage

### Deliverables

- `docs/daily-loop.md`
- `docs/examples/alias-pack.md`
- `docs/examples/agent-prompt-snippets.md`
- `docs/examples/editor-and-terminal-integration.md`

### Optional core work

Promote helpers into the binary only if repeated field use shows wrappers are not sufficient.

Candidate helpers:

- `claim start` wrapper over create + durable output
- `item done-from-claim` style convenience flow
- richer `next-work --json` reasons if that meaningfully improves automation

### Validation

- daily operation becomes faster through documented patterns
- most speed gains come from wrappers and examples, not CLI bloat

---

## Phase 4 — Formalize customization and advanced mode support

### Goal

Treat personalization and advanced coordination as supported overlays.

### Scope

- document local customization strategy
- document AGENTS.md and skill integration patterns
- isolate advanced coordinator guidance under a clear boundary
- ship repo template examples for integration

### Deliverables

- `docs/customization.md`
- `docs/advanced/coordinator-mode.md`
- `docs/advanced/claim-discipline.md`
- `docs/examples/repo-template.md`

### Validation

- users can build fast local workflows without asking the core product to become personal shell glue
- coordinator workflows are strong but contained

---

## Backlog candidates by theme

## Documentation backlog

- start-here page
- solo quickstart
- agent-assisted quickstart
- advanced coordinator guide
- first 10 minutes guide
- resume guide
- knowledge-while-working guide
- customization guide
- shell/task runner examples
- AGENTS.md snippets

## Product backlog

Only after docs/examples prove the need:

- `claim start`
- `session resume`
- richer `next-work --json` explanatory output
- exportable command bundles or example config generators

## Repo-integration backlog

- example `Justfile`
- example `Makefile`
- example `.agents/skills/` snippets
- example AGENTS.md section
- example snapshot commit workflow

---

## Rollout notes

- ship docs first
- prefer examples over new commands
- promote only the most universal helpers into core
- measure success by reduced confusion and faster restart, not by command count

---

## Decision

The strongest UX path is:

1. clarify the default operating mode
2. make startup and resume friction low
3. support speed through external wrappers and examples
4. keep advanced coordination strong but explicitly advanced

That path improves UX without sacrificing the protocol that makes `sprintctl` useful.
