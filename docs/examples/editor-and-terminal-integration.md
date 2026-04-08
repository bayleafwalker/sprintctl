# Editor And Terminal Integration

This page shows practical layouts for running `sprintctl` as part of normal
editing flow without adding new core commands.

## tmux split layout

Use one pane for coding and one pane for live sprint state:

```bash
tmux new-session -d -s sprint
tmux send-keys -t sprint:0.0 'nvim .' C-m
tmux split-window -h -t sprint:0.0
tmux send-keys -t sprint:0.1 'sprintctl sprint show --watch --detail --interval 20' C-m
tmux select-pane -t sprint:0.0
tmux attach -t sprint
```

## VS Code tasks

Create `.vscode/tasks.json`:

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "sprintctl: context bundle",
      "type": "shell",
      "command": "sprintctl usage --context --json && sprintctl next-work --json --explain && sprintctl git-context --json",
      "problemMatcher": []
    },
    {
      "label": "sprintctl: handoff",
      "type": "shell",
      "command": "sprintctl handoff --format json --output handoff.json && sprintctl handoff --format text",
      "problemMatcher": []
    }
  ]
}
```

## Neovim command wrappers

In `init.lua`:

```lua
vim.api.nvim_create_user_command("SprintContext", function()
  vim.cmd("split | terminal sprintctl usage --context && echo && sprintctl next-work --explain")
end, {})

vim.api.nvim_create_user_command("SprintHandoff", function()
  vim.cmd("terminal sprintctl handoff --format json --output handoff.json")
end, {})
```

## Git hook checkpoint (optional)

Example `.git/hooks/pre-push` fragment:

```bash
#!/usr/bin/env bash
set -euo pipefail

sprintctl maintain check >/dev/null
sprintctl render > docs/sprint-snapshots/current.txt
git add docs/sprint-snapshots/current.txt
```

Use this only if your team/repo already accepts auto-updated snapshot files.

## Notes

- Keep all integrations as transparent shell/task/editor configuration.
- Do not hide claim proof (`claim_id` + `claim_token`) behind opaque tooling.
- If command drift appears, switch integration commands to `.venv/bin/python -m sprintctl`.

