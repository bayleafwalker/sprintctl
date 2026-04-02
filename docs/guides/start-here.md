# Start Here

Use this page when you want the narrowest useful `sprintctl` path.

If your global `sprintctl` install is older than this repo and a documented
command/flag is missing, run commands via `python -m sprintctl` from the repo
checkout.

## 1. Create a sprint

```sh
sprintctl sprint create --name "Sprint 4" --status active
```

Add a few items:

```sh
sprintctl item add --sprint-id 1 --track docs --title "Write resume guide"
sprintctl item add --sprint-id 1 --track cli --title "Tighten handoff contract"
```

## 2. Read live context

```sh
sprintctl usage --context --json

# Optional live pane during active work
sprintctl sprint show --watch --detail --interval 30
```

This is the primary resume surface. It gives you:

- sprint summary
- active claims
- conflicts
- ready, blocked, and stale work
- recent decisions
- one concise next action

## 3. Start or claim work

If overlap is possible, claim before editing files:

```sh
sprintctl claim start --item-id 1 --actor codex-session-1 --json
```

If you are working solo and do not need claim discipline, you can still move
the item directly:

```sh
sprintctl item status --id 1 --status active
```

## 4. Record durable history

Use `item note` for decisions, blockers, lessons, and risks:

```sh
sprintctl item note \
  --id 1 \
  --type decision \
  --summary "Use handoff as working-memory snapshot"
```

## 5. Hand off or checkpoint

```sh
sprintctl handoff --output handoff.json
sprintctl render > docs/sprint-snapshots/sprint-current.txt
```

Use `handoff` when a later session must resume the same work. Use `render` when
you want a reviewable snapshot in git.

## Next Guides

- [Daily Loop](daily-loop.md)
- [Resume Work](resume-work.md)
- [Agent-Assisted Work](agent-assisted.md)
- [Project Integration](project-integration.md)
- [Customization Guide](../customization.md)
- [Interoperability Patterns](interoperability.md)
- [Alias Pack](../examples/alias-pack.md)
- [Agent Prompt Snippets](../examples/agent-prompt-snippets.md)
- [Editor And Terminal Integration](../examples/editor-and-terminal-integration.md)
- [Coordinator Mode](../advanced/coordinator-mode.md)
- [Claim Discipline](../advanced/claim-discipline.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
