# `AGENTS.md` Sample Section For `sprintctl`

Drop this into your repo's `AGENTS.md` and adapt names or paths as needed.

```md
## Sprint state

Sprint state is managed with `sprintctl`.

- Load `.envrc` before using `sprintctl`; the project DB should resolve to `.sprintctl/sprintctl.db`, not a home-directory default.
- For sprint-scoped work, consult live `sprintctl` state before repo docs when choosing or resuming work.
- Inspect item status, recent events, and active claims before editing repo files.
- Claim sprint items before repo edits when parallel overlap is possible.
- Use a strong live claim identity for each agent session: `runtime_session_id`, `instance_id`, and the returned `claim_token`.
- Treat actor label, branch, worktree, commit SHA, hostname, and pid as advisory metadata only.
- Ownership proof is always `claim_id + claim_token`.
- If an exclusive claim belongs to another live session, do not heartbeat or reuse it; get a handoff or pick different work.
- Use `sprintctl claim handoff` when ownership of an active claim changes sessions.
- Use `sprintctl handoff` when the next session needs broader sprint context but not claim ownership.
- Refresh `docs/sprint-snapshots/sprint-current.txt` after material sprint-state changes.
```
