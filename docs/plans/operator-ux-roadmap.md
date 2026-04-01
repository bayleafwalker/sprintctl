# Operator UX roadmap

sprintctl is a local-first CLI tool. This document defines the phased approach
to operator UX and the boundary between what stays in the CLI versus what moves
to an optional TUI layer.

---

## Phase 1 — CLI baseline (shipped)

All output is text or JSON. No live refresh, no interactive panes.

What the CLI provides today:

- `sprint show --detail` — full sprint breakdown with track health
- `sprint list` — tabular sprint list
- `item list` — flat item list with status and track
- `item show` — single item with events, claims, refs, deps
- `maintain check` — diagnostic report: stale items, track health, overrun risk
- `usage --context` — one-shot agent-consumable context dump (text or JSON)
- `render` — full sprint snapshot suitable for committing
- `handoff --format text` — human-readable handoff bundle
- All list/show/status/check commands support `--json` for scripting

This is the stable, supported interface. It covers the full operator and agent
workflow without any interactive dependency.

---

## Phase 2 — Lightweight live view (shipped, optional)

This phase is now in the CLI. `watch` mode periodically re-renders sprint
status in the terminal using a simple loop (no TUI framework needed).

```sh
sprintctl sprint show --watch [--interval 30]
```

Behavior:
- Clears the terminal and re-renders every `--interval` seconds (default: 30)
- `Ctrl-C` exits cleanly
- Falls back gracefully if the terminal doesn't support clear

This is useful for keeping a pane open during active work sessions. It does not
require a persistent process — each refresh is an independent DB read.

This phase also includes fzf-friendly output for pipe workflows:

```sh
# One item per line: #ID<TAB>STATUS<TAB>TRACK<TAB>ASSIGNEE<TAB>TITLE
sprintctl item list --fzf

# Pipe to fzf for interactive item selection
ITEM_ID=$(sprintctl item list --fzf | fzf | awk '{print $1}' | tr -d '#')
sprintctl item show --id "$ITEM_ID"
```

---

## Phase 3 — Optional TUI sidecar (future, opt-in)

A richer live view implemented as a separate optional binary (`sprintctl-tui`)
or behind a feature flag. Not bundled in the default install.

Candidate features:
- Split-pane layout: sprint overview + item detail
- Live claim expiry countdown
- Keyboard navigation for item status transitions

### CLI/TUI boundary

The CLI is the authoritative interface. The TUI is a read/navigate layer on top
of the same SQLite database — it does not introduce new write paths.

| Concern | CLI | TUI |
|---------|-----|-----|
| All write operations | ✅ stays in CLI | ❌ not in TUI |
| Machine-readable output (`--json`) | ✅ stays in CLI | ❌ not in TUI |
| Agent-consumable context | ✅ stays in CLI (`usage --context`) | ❌ not in TUI |
| Live refresh / watch mode | ✅ `sprint show --watch` | Phase 3 (optional richer live view) |
| Interactive panes | ❌ not in CLI | Phase 3 (optional) |
| Scripting and piping | ✅ CLI only | ❌ not in TUI |

**Rule**: any workflow that an agent or script needs must work entirely within
the CLI without the TUI present. The TUI is operator comfort, not operator
correctness.

---

## JSON coverage

All commands that produce structured output support `--json`. As of schema v8:

| Command | `--json` |
|---------|----------|
| `sprint show` | ✅ |
| `sprint list` | ✅ |
| `sprint status` | ✅ |
| `sprint kind` | ✅ |
| `sprint backlog-seed` | ✅ |
| `item show` | ✅ |
| `item list` | ✅ |
| `item ref list` | ✅ |
| `item dep list` | ✅ |
| `event list` | ✅ |
| `claim create` | ✅ |
| `claim list` | ✅ |
| `claim list-sprint` | ✅ |
| `claim handoff` | ✅ |
| `maintain check` | ✅ |
| `maintain sweep` | ✅ |
| `maintain carryover` | ✅ |
| `handoff` | ✅ (json or text via `--format`) |
| `next-work` | ✅ |
| `usage --context` | ✅ |
| `git-context` | ✅ |
| `render` | — (text only; structured output is `handoff --format json`) |

---

## Anti-goals

- No web UI, no hosted dependency, no sync protocol
- No mandatory interactive pane for any workflow
- No TUI dependency in the default `pip install`
- No persistent daemon; all state is derived on demand from the database
