---
doc_id: doc-backlog-linking-phase0
status: draft
supersedes: null
---

# Doc–backlog linking, Phase 0: convention + surfacing

## Problem

The remote backend holds ~831 work items across 11 repos; only 7 doc refs
have ever been created, all in this repo. Items carry a one-line title and an
empty description while the real implementation scope (per-task
What/Where/How/Done-when) lives in rich docs under `docs/plans/` and
`docs/sprints/` with no machine link back to the item. Doc pointers get
dropped into notes ad hoc (~5% of homelab-analytics events mention a `docs/`
path). The cost is real in both directions: agents implement from a one-line
title without the doc, and docs drift from what the backlog actually did — an
entire reconciliation sprint in homelab-analytics was spent fixing stale
`Status:` fields in sprint docs.

The failure is not missing tooling — the `ref` primitive and
`item ref add --type doc` have existed all along. It is a missing convention:
nothing in the workflows, resume surfaces, or agent guidance ever made an
agent create or consume a doc ref.

## What Phase 0 does

Three ideas, borrowed conceptually from scribectl but implemented as
convention on the existing primitive:

1. **Stable doc identity** — `doc_id` in frontmatter, never changes.
2. **Status lifecycle** — `draft` → `ratified` → `superseded` in frontmatter.
   Ratification is a human edit, never automated.
3. **Provenance** — items cite the doc they execute against via a doc ref;
   the ref label carries the `doc_id` so links survive file moves.

The full contract lives in
[docs/reference/doc-refs.md](../reference/doc-refs.md).

### Changes shipped with this doc

**sprintctl surfaces** (so the pointer reaches the agent without anyone
remembering to look):

- Doc refs accept repo-relative paths (`docs/plans/foo.md`), not just
  http(s) URLs (`db._validate_ref_url`, both backends).
- `next-work --explain`: each ready item includes `refs` in JSON; text output
  renders a `Refs:` subsection under the ready-items table, including which
  ready items have none.
- `claim create` / `claim start`: echo the claimed item's refs in text output
  and include a `refs` array in `--json` — the doc pointer lands in the
  claiming agent's context at the moment work starts.
- `session resume`: each active claim in the claim-recovery block carries the
  item's refs (JSON and text).
- `item show`: prints an explicit `Refs: (none — …)` nudge with the exact
  `item ref add` command when an item has no refs.

**Convention**:

- Frontmatter (`doc_id`, `status`, `supersedes`) added to all docs in
  `docs/plans/`; new plan/sprint docs start as `draft`.
- Workflow A (bootstrap template) amended: an item is not shaped until it
  carries a doc ref or an explicit "no doc" note.
- Agent guidance amended: on claim, read the doc ref before writing code;
  when work changes what a doc claims, update the doc in the same commit.

**Backfill**: doc refs attached to active-sprint items in the remote backend
where an authoritative doc exists.

## What Phase 0 deliberately does not do

- No doc registry, no `item add --doc` resolution, no claim-time policy gate
  on doc status, no drift checker. Those are Phase 1, and only if Phase 0
  evidence shows agents use the pointers when handed them.
- No automated ratification of anything, ever. If a pipeline needs a
  machine-settable state, it gets a different word — `ratified` stays a human
  tick.
- No scribectl involvement. Vault assumptions, note types, and wikilink
  routing stay in scribectl's home domain.

## Done when

- An agent claiming any item in an active sprint sees the governing doc path
  in its claim output and resume bundle without running extra commands.
- Shaping a new item without either a doc ref or a "no doc" note reads as an
  incomplete shape per Workflow A.

## Success test for Phase 1

Revisit after a few sprints of use: if agents handed the pointer still don't
read or update the docs, more tooling would not have helped — stop. If the
convention sticks but drift persists at the edges (doc `Status:` fields vs
item status), build the Phase 1 pieces: doc registry scan, claim gate on
ratified status, and a `maintain`-style drift check.
