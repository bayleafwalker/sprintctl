# Doc refs: linking work items to their design docs

Work items carry a one-line title; the actual implementation scope usually
lives in a repo doc (`docs/plans/`, `docs/sprints/`). The doc ref is the
machine link between the two. This page defines the convention: how docs
declare identity, and how items point at docs.

This is a convention on top of the existing `ref` primitive — no new tables,
no new commands.

## Doc frontmatter contract

Every plan or sprint doc that work items will reference starts with YAML
frontmatter:

```yaml
---
doc_id: my-feature-plan
status: draft
supersedes: null
---
```

| Field | Meaning |
|-------|---------|
| `doc_id` | Stable kebab-case slug. Never changes after creation, even if the file moves or is renamed. |
| `status` | Lifecycle state: `draft` → `ratified` → `superseded`. |
| `supersedes` | `doc_id` of the doc this one replaces, or `null`. When set, flip the old doc's `status` to `superseded` in the same change. |

Status rules:

- **`draft`** — the doc is being written or revised. Items may reference a
  draft, but the doc's contents are not yet a commitment.
- **`ratified`** — a human has reviewed the doc and accepted it as the basis
  for work. **Agents never set `ratified` themselves.** Ratification is a
  human edit, always.
- **`superseded`** — replaced by a newer doc. The successor names this doc in
  its `supersedes` field. Never delete superseded docs; the refs pointing at
  them are execution history.

## Attaching a doc ref to an item

Doc refs accept a repo-relative path (preferred for repo docs) or an absolute
http(s) URL. Put the `doc_id` in the label so the link survives file moves:

```bash
sprintctl item ref add \
  --id <item-id> \
  --type doc \
  --url docs/plans/my-feature-plan.md \
  --label my-feature-plan
```

Convention: **every shaped item carries either a doc ref or an explicit
"no doc" note** (`item note --type decision --summary "No doc: scope fits in
the item title/notes."`). Absence of both means the item is not yet shaped.

## Where refs surface

You do not need to remember to look for refs — the work-selection and resume
surfaces render them:

- `item show --id N` — full ref list; prints a nudge line when empty.
- `next-work --explain` (text and `--json`) — refs per ready item, plus which
  ready items have none.
- `claim create` / `claim start` — echo the claimed item's refs (text) and
  include a `refs` array (`--json`), so the pointer lands in the claiming
  agent's context at the moment work starts.
- `session resume` — refs on every active-claim item in the claim-recovery
  block.

## Reading a doc ref as an agent

When you claim an item and its ref points at a repo doc:

1. Read the doc **before** writing code. The per-task What/Where/How/Done-when
   in the doc is the scope; the item title is just the handle.
2. Check the doc's `status`. If `superseded`, follow the chain to the
   successor and flag the stale ref with a note on the item.
3. When your work changes what the doc claims (e.g. a `Status:` field inside
   the doc body), update the doc in the same commit — the reconciliation cost
   of drifting docs is a whole sprint, not a diff.
