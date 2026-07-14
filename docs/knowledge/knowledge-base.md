# Knowledge Base — sprintctl
Generated: 2026-07-14T14:29:26Z

## Decisions

### Plan: agentops/docs/plans/agentops/substrate-resilience-plan.md §'tmux enforcement'. Guard script: if [ -z TMUX ]; exit 1; fi. Update AGENTS.md devbox workflow section.
Source: track: Resilience, sprint: 379
Tags: daemon, tmux, operator-safety

The sprintctl daemon refuses to start outside tmux (guard script: `if [ -z "$TMUX" ]; then exit 1; fi`), documented in the devbox workflow section of AGENTS.md. This prevents an operator from accidentally starting a long-running daemon in a shell that will vanish on disconnect, which would otherwise silently kill the daemon and leave takeups stuck. Source: agentops/docs/plans/agentops/substrate-resilience-plan.md §'tmux enforcement'.

---

### Plan: agentops/docs/plans/agentops/substrate-resilience-plan.md §'Daemon startup recovery'. On start: actionctl sessions --active, classify by PID liveness, re-adopt live sessions, emit session.exited + release takeup for dead sessions. Implement in actionq-dispatcher.
Source: track: Resilience, sprint: 379
Tags: daemon, resilience, session-recovery

On daemon startup, sprintctl's resilience design classifies in-flight sessions by PID liveness (via `actionctl sessions --active`): live sessions are re-adopted, and dead sessions get a session.exited event plus their takeup released. This prevents a daemon restart from leaving orphaned takeups that block other agents from claiming the same sprint. Implemented in actionq-dispatcher. Source: agentops/docs/plans/agentops/substrate-resilience-plan.md §'Daemon startup recovery'.

---

### Plan: agentops/docs/plans/agentops/substrate-resilience-plan.md §'Takeup sweep'. Cross-references actionq session list. Run on daemon startup and expose as standalone CLI verb. Requires sprintctl + actionq co-location or actionq-server read API.
Source: track: Resilience, sprint: 379
Tags: takeup, actionq, resilience, daemon

sprintctl takeup sweep releases stale takeups by cross-referencing actionq session state, runs on daemon startup, and is also exposed as a standalone CLI verb. It requires either sprintctl and actionq co-location (shared local state) or an actionq-server read API when they're not co-located. Source: agentops/docs/plans/agentops/substrate-resilience-plan.md §'Takeup sweep'.

---

### Hardened backend marker parsing for malformed/non-object backend.json and added resolver coverage for the .sprintctl/sprintctl.db directory sentinel used by remote migration.
Source: track: Core backend, sprint: 379
Tags: backend, remote-mode, resilience

sprintctl.backend.resolve_repo_identity/load_backend_config parse .sprintctl/backend.json defensively: a marker whose `backend` field is missing or not one of 'local'/'remote' raises a clear BackendConfigError instead of an unhandled JSONDecodeError or a silent wrong-mode fallback, and repo_id is coerced to str only when present. Resolver test coverage was extended for the .sprintctl/sprintctl.db directory sentinel used during migrate-to-remote, so local-mode repo_id resolution still works via that sentinel when no backend.json marker exists yet. Rule: a backend marker file that gates which storage a CLI talks to should fail loudly and specifically on malformed input, not fall through to a default backend.

---

## Coordination Lessons

### actionq scope-iterate fails when the worktree has uncommitted plan-doc changes
Source: track: dispatch-smoke, sprint: 379
Tags: actionq, dispatcher, coordination, worktree

Three actionq scope-iterate dispatches (items #111, #112, #113) failed within 5 minutes on 2026-04-28 with the same cause: 'Worktree has uncommitted changes: M docs/plans/pg-backend-remote-mode-plan.md M docs/plans/sprintctl-multi-agent-takeup-plan.md'. The dispatcher requires a clean worktree before it will hand an item to an agent; in-progress edits to plan docs (left uncommitted from planning work) block every subsequent dispatch until they're committed or stashed. Lesson: commit or stash plan-doc edits before dispatching sprint items in the same worktree, and expect a dispatcher failure loop rather than a single clear error if you forget.

---
