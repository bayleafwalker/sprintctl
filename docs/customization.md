# Customization Guide

Use this guide when you want faster local workflows without changing the core
`sprintctl` semantics.

The core CLI remains the source of truth. Customization exists to reduce
typing, speed startup, and standardize repeated repo patterns.

## Four-Layer Model

Use a layered model so speed does not hide correctness:

1. core CLI: state transitions, ownership proof, text and JSON contracts
2. repo-local workflow layer: committed scripts and task-runner targets
3. user-local workflow layer: aliases, shell functions, editor bindings
4. agent guidance layer: AGENTS.md snippets and runtime startup wrappers

Keep semantics in layer 1. Keep convenience in layers 2-4.

## Guardrails

- never hide authoritative writes in opaque wrappers
- optimize for ergonomics, not new semantics
- preserve `--json` access for automation paths
- keep repo conventions committed and personal shortcuts local

## Recommended Starter Pack

For most repos, start with this small set:

- one startup script (`usage --context --json`, `next-work --json`, `git-context --json`)
- one snapshot task (`render > docs/sprint-snapshots/...`)
- one AGENTS.md section describing claim/startup/shutdown expectations
- one example task-runner file (`Makefile` or `Justfile`)

This yields most of the UX gain without increasing CLI surface area.

## Practical Patterns

Task runner entry points:

```make
resume:
	@sprintctl usage --context
	@echo
	@sprintctl next-work --explain

agent-context:
	@sprintctl usage --context --json
	@sprintctl next-work --json --explain
	@sprintctl git-context --json
```

Thin startup wrapper:

```bash
#!/usr/bin/env bash
set -euo pipefail

sprintctl usage --context --json
sprintctl next-work --json --explain
sprintctl git-context --json
```

These should remain transparent wrappers over real commands.

## Promotion Criteria

Promote external glue into core CLI only when all are true:

1. repeated need appears across multiple repos
2. semantics remain explicit and protocol-safe
3. command stays useful without repo-specific assumptions
4. wrappers and docs can no longer solve the problem cleanly

## Related

- [Start Here](guides/start-here.md)
- [Advanced Coordination](guides/advanced-coordination.md)
- [Coordinator Mode](advanced/coordinator-mode.md)
- [Claim Discipline](advanced/claim-discipline.md)
- [Repo Template Example](examples/repo-template.md)
