# `AGENTS.md` Sample Section For `sprintctl`

Drop this into your repo's `AGENTS.md` and adapt names or paths as needed.

```md
## Sprint state

Sprint state is managed with `sprintctl`.

- Load `.envrc` before using `sprintctl`; the project DB should resolve to `.sprintctl/sprintctl.db`, not a home-directory default.
- For sprint-scoped work, consult live `sprintctl` state before repo docs when choosing or resuming work.
- Inspect item status, recent events, and active reservations before editing repo files.
- Treat an item as shaped only when it has a governing doc ref or an explicit `No doc:` decision note.
- Read the governing doc before editing, and never set its frontmatter `status` to `ratified` as an agent.
- Before implementation, pin the executed revision with a `<doc-id>@git:<full-commit-sha>` ref label.
- Reserve sprint items before repo edits when parallel overlap is possible.
- Use a stable session identity: `runtime_session_id` and optional `instance_id`.
- Treat actor label, branch, worktree, commit SHA, hostname, and pid as advisory metadata only.
- A reservation is an advisory coordination signal, not ownership proof.
- If multiple active reservations exist on the same item, treat it as a visible conflict and coordinate before editing. `reserve` reports the overlap (`conflict`, `conflict_severity`) instead of refusing you; two `execution` reservations are a `warning`, `execution` beside `verification` or `observation` is ordinary.
- Use `sprintctl reservation reassign` when the reservation for an active item changes sessions.
- Use `sprintctl handoff` when the next session needs broader sprint context but not the reservation.
- Refresh `docs/sprint-snapshots/sprint-current.txt` after material sprint-state changes.
```
