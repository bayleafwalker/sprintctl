# Bootstrap Workflow

The minimal pattern for onboarding `sprintctl` onto a fresh repository and running the first sprint.

For a full worked example including AGENTS.md, sprint naming conventions, workflow docs, and sample knowledge flow, see the [sprintctl-bootstrap-template](https://github.com/bayleafwalker/sprintctl-bootstrap-template) repo.

---

## Step 1: Scope the database to the repo

```bash
echo 'export SPRINTCTL_DB="${PWD}/.sprintctl/sprintctl.db"' > .envrc
source .envrc

# Add to .gitignore
echo '.sprintctl/' >> .gitignore
echo 'handoff-*.json' >> .gitignore
```

The database is created automatically on first use — no `init` command needed.

## Step 2: Create the first sprint

Use the naming convention `YYYY-SNN-<anchor>-<focus>-<phase>`:

```bash
sprintctl sprint create \
  --name 2026-S01-forge-schema-overture \
  --status active \
  --start 2026-03-30 \
  --end 2026-04-12
# Note the sprint ID (e.g., 1)
```

## Step 3: Add shaped items

Tracks are created implicitly via `--track <name>`. Use 3-5 tracks.

```bash
sprintctl item add --sprint-id 1 --track core --title "Define data models: User, Session, Event"
sprintctl item note --id 1 --type decision \
  --summary "Create src/models.py. User: id, email, created_at. Session: id, user_id, token, expires_at." \
  --actor setup

sprintctl item add --sprint-id 1 --track docs --title "Create AGENTS.md with track taxonomy and reservation policy"
sprintctl item note --id 2 --type decision \
  --summary "Done when AGENTS.md covers: tracks, reservation policy, review policy, artifact paths, source-of-truth order." \
  --actor setup
```

## Step 4: Render a committed snapshot

```bash
mkdir -p docs/sprint/archive docs/knowledge
sprintctl render > docs/sprint/current.md
git add docs/sprint/current.md
```

## Step 5: Verify

```bash
sprintctl sprint show --detail
sprintctl item list --sprint-id 1
sprintctl maintain check --sprint-id 1
```

---

## The basic work loop (reserve → work → done)

```bash
# Reserve an item before starting
RESERVATION=$(sprintctl reservation reserve \
  --item-id 1 \
  --actor claude-session-1 \
  --role execution \
  --session-id "${CODEX_THREAD_ID:-session-1}" \
  --json)

RESERVATION_ID=$(echo "$RESERVATION" | jq -r '.id')

# Record decisions during work
sprintctl item note --id 1 --type decision \
  --summary "Using SQLAlchemy declarative base with type annotations for all models." \
  --actor claude-session-1

# Done: note + status transition + release
sprintctl item note --id 1 --type decision \
  --summary "Done. src/models.py created with User, Session, Event. First Alembic migration generated." \
  --actor claude-session-1
REV=$(sprintctl item show --id 1 --json | jq -r '.item.status_revision')
sprintctl item status --id 1 --status done --actor claude-session-1 --expected-revision "$REV"
sprintctl reservation release --id "$RESERVATION_ID" --actor claude-session-1
```

## Handoff to the next session

```bash
# Leave a handoff note
sprintctl item note --id 2 --type claim-handoff \
  --summary "Partial progress: AGENTS.md written through reservation policy. Review policy not yet written." \
  --detail "Next: Write review policy section (schema changes, AGENTS.md changes require review). File: AGENTS.md at line ~80." \
  --actor claude-session-1

# Reassign the reservation to the next session
sprintctl reservation reassign \
  --id <reservation-id> \
  --actor claude-session-2 \
  --session-id next-session \
  --json
```

## Sprint wrap-up

```bash
# Run maintenance check and sweep stale reservations
sprintctl maintain check --sprint-id 1
sprintctl maintain sweep --sprint-id 1

# Carry over incomplete items to next sprint
sprintctl sprint create --name 2026-S02-shore-api-build --status active --start 2026-04-13 --end 2026-04-26
sprintctl maintain carryover --from-sprint 1 --to-sprint 2

# Archive current sprint
sprintctl render > docs/sprint/archive/2026-S01-forge-schema-overture.md
REV=$(sprintctl sprint show --id 1 --json | jq -r '.status_revision')
sprintctl sprint status --id 1 --status closed --expected-revision "$REV"

# Update current.md for new sprint
sprintctl render > docs/sprint/current.md
```

---

## Sprint naming vocabulary

For the `YYYY-SNN-<anchor>-<focus>-<phase>` format, a minimal starting vocabulary:

**Anchor** (project mood/place): hearth, forge, harbor, atlas, lantern, signal, anvil, grove
**Focus** (sprint concern): schema, workflow, reservation, memory, review, render, contract, handoff
**Phase** (sprint posture): overture, weave, survey, ascent, harvest, repair, cadence, shaping

Example names: `2026-S01-forge-schema-overture`, `2026-S02-harbor-reservation-weave`, `2026-S03-signal-review-harvest`

See the [sprintctl-bootstrap-template](https://github.com/bayleafwalker/sprintctl-bootstrap-template) repo for the full vocabulary, naming rules, and worked examples.
