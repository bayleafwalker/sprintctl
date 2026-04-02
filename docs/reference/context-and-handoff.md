# Context and Handoff Contracts

This document defines the public read contracts that matter most for resuming
work safely.

## `usage --context --json`

`usage --context --json` is the primary live resume contract.

Contract version: `1`

Top-level shape:

```json
{
  "contract_version": "1",
  "sprint": {},
  "summary": {},
  "active_claims": [],
  "conflicts": [],
  "ready_items": [],
  "blocked_items": [],
  "stale_items": [],
  "recent_decisions": [],
  "next_action": {}
}
```

Field intent:

- `sprint`: sprint identity and goal
- `summary`: counts for total, done, active, pending, blocked, stale, ready, waiting-on-dependencies, active-claims
- `active_claims`: proof-aware active claim state
- `conflicts`: claim, dependency, blocked-work, or stale-work issues that should change operator behavior
- `ready_items`: pending items with no unresolved blockers
- `blocked_items`: items in explicit `blocked` status
- `stale_items`: items that are drifting based on configured thresholds
- `recent_decisions`: bounded decision/history slice for fast reconstruction
- `next_action`: one concise recommendation explaining what matters now

Text output mirrors the same section order so human and agent paths stay aligned.

## `next-work --explain`

`next-work --explain` enriches readiness output with exclusion reasons,
conflicts, and a local next-step recommendation.

- text mode (`next-work --explain`) renders a human-readable summary
- JSON mode (`next-work --json --explain`) emits the full typed contract below

Contract version: `1`

Top-level shape:

```json
{
  "contract_version": "1",
  "sprint": {},
  "summary": {},
  "ready_items": [],
  "dependency_waiting_items": [],
  "active_claims": [],
  "conflicts": [],
  "next_action": {},
  "recommended_commands": []
}
```

Field intent:

- `summary.pending_total`: `ready + waiting_on_dependencies`
- `ready_items`: pending items with no unresolved blockers, each with `reason_code=ready-unblocked`
- `dependency_waiting_items`: pending items excluded from ready output due to unresolved blockers, each with `reason_code=waiting-on-dependencies`
- `active_claims`: current active claim slice without claim secrets
- `conflicts`: claim/dependency conflicts derived from current sprint state
- `next_action`: one concise recommendation based on the same conflict/priority rules used by context surfaces
- `recommended_commands`: ordered command bundle aligned with `next_action`; some entries intentionally use placeholders like `<token>` or `<name>` where proof-bearing values are required

Compatibility note:

- `next-work --json` (without `--explain`) preserves the legacy list-only output shape.

## `session resume --json`

`session resume --json` is a convenience resume contract that bundles:

- `usage --context --json`
- `next-work --json --explain`
- current git metadata from `git-context` when available

Contract version: `1`

Top-level shape:

```json
{
  "contract_version": "1",
  "generated_at": "...",
  "sprint": {},
  "context": {},
  "next_work": {},
  "git_context": {},
  "next_action": {},
  "recommended_sequence": []
}
```

Field intent:

- `context`: embedded `usage --context --json` contract
- `next_work`: embedded `next-work --json --explain` contract
- `git_context`: current branch/SHA/worktree/dirty-files when in a git repo; otherwise `null`
- `next_action`: primary recommendation for resume flows
- `recommended_sequence`: explicit follow-up command sequence

Consistency rule:

- `next_action` is canonical for this surface and is mirrored into
  `next_work.next_action` so resume output presents one recommendation.

## `handoff --format json`

`handoff --format json` is the serialized working-memory contract.

Bundle version: `1`

Top-level shape:

```json
{
  "bundle_type": "handoff",
  "bundle_version": "1",
  "generated_at": "...",
  "generated_from": {},
  "sprint": {},
  "summary": {},
  "active_claims": [],
  "conflicts": [],
  "work": {},
  "recent_decisions": [],
  "recent_events": [],
  "next_action": {},
  "delta_since_last_handoff": {},
  "freshness": {},
  "evidence": {},
  "git_context": {},
  "resume_instructions": [],
  "agent_shutdown_protocol": {},
  "claim_identity_model": {}
}
```

Behavioral rules:

- one canonical bundle shape; no separate personas
- text mode is a rendering of the same semantics, not a different contract
- claim secrets are never included
- a `handoff-generated` event is recorded after successful bundle generation
- `delta_since_last_handoff` compares against the most recent prior handoff event

Important sections:

- `work.active_items`, `work.ready_items`, `work.blocked_items`, `work.stale_items`
- `freshness` for staleness and dirty-worktree visibility
- `evidence` for ref counts, dirty files, and validation placeholders
- `resume_instructions` for the recommended restart path

## Recent Decisions

`decision`, `pattern-noted`, `lesson-learned`, and `risk-accepted` are treated
as knowledge-bearing events for context reconstruction and backlog seeding.

Each recent decision entry includes:

- `event_id`
- `event_type`
- `created_at`
- `actor`
- `work_item_id`
- `summary`
- `detail`
- `tags`

## Ownership Model

- proof = `claim_id + claim_token`
- `claim handoff` transfers ownership
- `handoff` transfers resumable context
- `claim resume` finds claims by advisory identity when context is lost

## Design Constraints

- stdlib-only JSON contract surfaces
- no ORM requirement
- no distributed-coordination assumptions
- no extra personas or alternate bundle shapes until this contract is stable
