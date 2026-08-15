# Agent Prompt Snippets

Use these snippets to standardize how operator sessions and coding agents
collect sprint context before taking action.

Adjust item IDs and actor names per repository.

## 1. Session startup snippet

```text
Run these commands and return concise JSON summaries before editing files:
1) sprintctl usage --context --json
2) sprintctl next-work --json --explain
3) sprintctl git-context --json
Then propose the single best next item to reserve.
```

## 2. Reserve-and-execute snippet

```text
Reserve item <ID> using role execution and actor <ACTOR>.
Save reservation_id.
While implementing:
- touch activity when useful
- record at least one decision note with git branch + sha
Before completion:
- run focused tests
- mark item done with expected-revision CAS
- release the reservation
Return: test results, files changed, and any follow-up risks.
```

## 3. Coordinator + sub-agent snippet

```text
You are coordinator. Do not let workers conflict on the same files.
1) Create a coordinate reservation on item <ID>.
2) Spawn worker execute reservations on the same item.
3) Assign disjoint file ownership to each worker.
4) Require each worker to return:
   - changed files
   - tests run
   - blockers
5) Consolidate, run integration tests, and close/release reservations.
```

## 4. End-of-session snippet

```text
Finalize session with sprint hygiene:
1) reassign or release every active reservation
2) sprintctl handoff --format json --output handoff.json
3) sprintctl render > docs/sprint-snapshots/current.txt
4) sprintctl maintain check
Summarize conflicts, stale work, and next_action.
```

## 5. Module-entrypoint-safe snippet

Use this when installed `sprintctl` differs from repository command surface:

```text
Run commands via repo-local entrypoint:
.venv/bin/python -m sprintctl usage --context --json
.venv/bin/python -m sprintctl next-work --json --explain
.venv/bin/python -m sprintctl git-context --json
```
