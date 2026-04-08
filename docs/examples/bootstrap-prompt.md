# Bootstrap Prompt

A prompt to paste into an agent session when onboarding `sprintctl` onto a fresh repository.

For detailed context, workflow docs, sample sprints, and the full worked example, see [sprintctl-bootstrap-template](https://github.com/bayleafwalker/sprintctl-bootstrap-template).

---

## Main bootstrap prompt

```
You are initializing the sprintctl workflow on this repository. Set up the sprint execution layer and leave the repo in a clean, working state with a first sprint ready to execute.

sprintctl is a local-first sprint coordination CLI. It manages sprints, tracks, items, claims, handoffs, and state transitions via a repo-local SQLite database. There is NO `init` command — the database is created on first use.

## Step 1: Set up DB scope

Create .envrc:
  echo 'export SPRINTCTL_DB="${PWD}/.sprintctl/sprintctl.db"' > .envrc
  source .envrc

Add to .gitignore: .sprintctl/  handoff-*.json  sprint-*.json

## Step 2: Assess the repo

Read README.md and AGENTS.md if they exist. Run: sprintctl sprint show
Identify: what is this repo for, does a sprint already exist, what tracks make sense.

## Step 3: Create the first sprint

Name format: YYYY-SNN-<anchor>-<focus>-<phase>
Use phase 'overture' for a first/setup sprint.

  sprintctl sprint create --name <name> --status active --start <today> --end <today+14>

Note the sprint ID.

## Step 4: Add shaped items

Tracks are created implicitly via --track <name>. Use 3-5 tracks. Create 8-12 items.
Each item needs an outcome-focused title, not an activity.

  sprintctl item add --sprint-id <id> --track <track> --title "<specific outcome>"
  sprintctl item note --id <item-id> --type decision --summary "<scope, done condition>" --actor setup

## Step 5: Create AGENTS.md

If AGENTS.md doesn't exist, create it. Must cover: repo purpose, track taxonomy,
claim policy, review policy, artifact paths, source-of-truth order.

## Step 6: Render and commit snapshot

  mkdir -p docs/sprint/archive docs/knowledge
  sprintctl render > docs/sprint/current.md

## Step 7: Verify

  sprintctl sprint show --detail
  sprintctl item list --sprint-id <id>
  sprintctl maintain check --sprint-id <id>

Confirm: active sprint, 8+ items, AGENTS.md exists, docs/sprint/current.md committed, no stale claims.
```

---

## Workflow-only prompt (for repos already initialized)

```
You are working in a repository that uses the sprintctl workflow. Orient yourself, then work.

## Orientation

1. source .envrc
2. sprintctl sprint show --detail
3. sprintctl item list --sprint-id <id>
4. sprintctl claim list-sprint --sprint-id <id>
5. Read AGENTS.md
6. For any active items, read: sprintctl item show --id <id>

## Claim → work → done cycle

Before starting non-trivial work:
  sprintctl claim start --item-id <id> --actor <you> --runtime-session-id "${CODEX_THREAD_ID:-session}" --json
  # Save claim_id and claim_token from output

Record decisions during work:
  sprintctl item note --id <id> --type decision --summary "<decision and rationale>" --actor <you>

When done:
  sprintctl item status --id <id> --status done --actor <you> --claim-id <id> --claim-token <token>
  sprintctl claim release --id <claim-id> --claim-token <token>

When handing off mid-work:
  sprintctl item note --id <id> --type claim-handoff --summary "<state, next steps>" --detail "<details>" --actor <you>
  sprintctl claim handoff --id <claim-id> --claim-token <token> --actor <next-session> --mode rotate

## Before stopping

  sprintctl render > docs/sprint/current.md
  # Release any claims you won't continue
```
