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
Then propose the single best next item to claim.
```

## 2. Claim-and-execute snippet

```text
Claim item <ID> with TTL 900 using actor <ACTOR>. Save claim_id and claim_token.
While implementing:
- heartbeat every ~450s
- record at least one decision note with git branch + sha
Before completion:
- run focused tests
- mark item done with claim proof
- release the claim
Return: test results, files changed, and any follow-up risks.
```

## 3. Coordinator + sub-agent snippet

```text
You are coordinator. Do not let workers conflict on the same files.
1) Create a coordinate claim on item <ID>.
2) Spawn worker execute claims using coordinate claim id/token.
3) Assign disjoint file ownership to each worker.
4) Require each worker to return:
   - changed files
   - tests run
   - blockers
5) Consolidate, run integration tests, and close/release claims.
```

## 4. End-of-session snippet

```text
Finalize session with sprint hygiene:
1) handoff or release every owned claim
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

