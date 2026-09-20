---
doc_id: 2450-s6-ledger-checkpoint
status: accepted
supersedes: docs/runbooks/maintenance-lane.md "Interrupted items: checkpoint and pickup" (agentops) — S6 slice only; #2430 owns whatever of the interim rule remains after S6 lands
---

# S6 ledger checkpoint: Checkpoint bound to item and Release

agentops item #2450 (2430-S3). Specifies, for S6 planning (target-state path
step 5), the ledger-evidence replacement for the interim `handoff/v1` file
plus `lane.checkpoint` note pairing decided on agentops #2430 (note #3133,
2026-09-19 16:55 UTC) and landed in the maintenance-lane runbook's
"Interrupted items: checkpoint and pickup" section, step 5 ("S6, one
sentence"). Reviewed against target-state TS-5, TS-8, path step S6, and
direction §5.3.

## No new store

Direction §5.3: "a checkpoint is a Git commit plus evidence; do not wrap Git
to make a new noun." A Checkpoint is **not** a new table, not a new
`EvidenceSet` class, and not a new sprintctl subsystem. It is the existing
generic item-note/event mechanism (`sprintctl item note`, already used for
`lane.dispatch` / `lane.review` / `lane.checkpoint` / `decision`), given:

1. a fixed field set (below), carried in the note's free-form JSON payload
   exactly as `git_branch`, `git_sha`, `git_worktree`, `evidence_item_id`,
   and `evidence_event_id` already are (`contracts.py::canonicalize_decision_payload`
   and its siblings pass unrecognized keys through via `result.update(source)`,
   so no schema or contract change is required to carry
   `release_digest`, `worktree_host`, `validated`, `rejected`,
   `next_action`, `predecessor_session`, `acked_by`);
2. a note type reserved for it, `lane.checkpoint` (unchanged name — the
   interim rule already uses it, so migration is a copy, not a rename);
3. a binding to the item's **current Release** (`sprintctl/releases.py`):
   derived, never stored, as "the release frozen most recently at the
   item's current revise count." The checkpoint's `release_digest` field
   records which Release was current when the checkpoint was written, so a
   later `revise` decision (which makes a Release no longer current) does
   not silently invalidate an old checkpoint's meaning — the query can tell
   a checkpoint against a stale Release from one against the current one.

## Field table

Identical names and semantics to the interim rule (agentops #2430, note
#3133) so the S1 pairing (`lane.checkpoint` note + optional `handoff/v1`
file) migrates to this by dropping the file, not by renaming fields.

| Field | Type | Source | Notes |
|---|---|---|---|
| `item` | int (item id) | note's `work_item_id` (already the note's home item — not a payload field) | the item the checkpoint interrupts |
| `release_digest` | string, sha256 hex | `releases.py::release_digest` of the item's current Release at write time | binds the checkpoint to a specific frozen item revision + acceptance contract + context refs, not just the item id |
| `branch` | string | existing `git_branch` note column | unchanged from the interim rule |
| `sha` | string, 40-hex | existing `git_sha` note column | unchanged; `releases.py::validate_commit_sha` already validates this shape, reused here |
| `worktree_host` | string | new payload field, e.g. `workstation:/projects/dev/_wt/<repo>-<slug>` | interim rule's `git_worktree` was path-only, single-host; S6 field adds the host since a checkpoint may be picked up by a session on a different host (TS-8 cross-host resume) |
| `validated` | string | `detail` sub-field | what the interrupted session confirmed works |
| `rejected` | string | `detail` sub-field | approaches tried and discarded, so a successor does not repeat them (the falsifier this item names) |
| `next_action` | string | `detail` sub-field | what the successor should do first |
| `predecessor_session` | string | new payload field (actor/session identifier) | in served mode `--actor` is ignored and the authenticated identity is recorded instead (maintenance-lane runbook, "Dispatch"); `predecessor_session` is therefore carried explicitly in the payload, the same way tier/model/harness already ride in `--tags` for the same reason |
| `created_at` | timestamp | the note event's own `created_at` | not a payload field — read from the event envelope |
| `acked_by` | string or null | new payload field, null until claimed | see Ack rule below |

`validated` / `rejected` / `next_action` stay inside `--detail` as
structured text (the interim rule's existing shape:
`"validated: <...>; rejected: <...>; next_action: <...>"`) rather than three
separate note columns, because the note event schema has one `detail`
string and adding three more typed columns would be new schema for no
query gain — nothing needs to filter on `rejected` alone. `release_digest`,
`worktree_host`, `predecessor_session`, and `acked_by` do need to be
independently queryable (see Query below), so they go into the note's JSON
payload as named keys, not folded into `detail`.

## Ack rule

Ack is the successor's binding at session start, **not a Decision** (TS-5:
Decisions write terminal status only; an ack is not terminal — the item
keeps going).

An ack is a second `lane.checkpoint`-family note (type `lane.checkpoint.ack`,
or the successor's own `lane.dispatch` note citing the predecessor note id —
either satisfies it, matching the interim rule's "a later session takes an
orphaned or checkpointed item by writing its own `lane.dispatch` note citing
the predecessor note id: that is the ack and the claim"). Writing it sets
that predecessor checkpoint's `acked_by` for the purposes of the query below
— computed by matching a later note's `evidence_event_id` (or a cited note
id in its `detail`/tags) back to the checkpoint note's own event id, not by
mutating the original note. Notes are append-only; nothing rewrites the
checkpoint's payload in place, so "acked" is a join, not an update — same
pattern the current `next-work` conflict/ready computation already uses
(`application_common.py::_next_work_action`) to derive state from event
history rather than storing derived flags.

## Query: unacknowledged checkpoints per sprint

"Items with an unacknowledged checkpoint" is, per item: the newest
`lane.checkpoint` note on the item where no later note (by `created_at`)
either (a) is itself a `lane.checkpoint.ack`, or (b) is a `lane.review` /
`lane.dispatch` note whose `detail` or tags cite the checkpoint note's id.
Scoped to a sprint: join through the item's `sprint_id` the same way
`next-work` already scopes ready/waiting items to a sprint
(`work_application.py::_read_next_work`).

Exposure surface: `next-work`'s explain contract
(`served.py::read_next_work_explain`, `application_common.py::_next_work_explain_contract`)
already assembles active reservations, conflicts, ready and waiting items
per sprint for the same audience (a session picking its next item). The S6
implementation slice should add one more bucket to that same contract —
`checkpointed_unacked: [...]` — rather than a new CLI subcommand, so a
session already reading `next-work` output sees interrupted-item pickups
without a second query. `sprintctl next-work` gets an `--include-checkpoints`
flag (default on) wiring this bucket into the existing render path
(`render.py`) the same way ready/waiting already render.

## Staleness (carried over unchanged)

The interim rule's staleness definition (checkpoint older than 24h, or sha
not on the recorded branch, or branch gone → restart from
`origin/<default>`) is not a ledger concept and needs no S6 change: it is
computed at read time from `created_at` plus a live `git fetch` of `branch`
against `sha`, exactly as today. The ledger only replaces *where the fields
live* (note payload vs. note payload + optional file); it does not change
how staleness is decided.

## Migration (S1 → S6): a copy, not a rewrite

Because the field names above are identical to the interim rule's, the S6
slice's migration step is: stop writing the optional `handoff/v1` file (the
note was always the ledger source of truth; the file was a convenience for
harnesses without a served read path — TS-8's "harnesses without
SessionStart need an explicit launch path" covers that gap separately, not
through the file), and add `release_digest`, `worktree_host`,
`predecessor_session`, and `acked_by` as new payload keys on the same
`lane.checkpoint` note type. No historical note needs to be rewritten:
older `lane.checkpoint` notes without these keys simply have `release_digest`
etc. absent, which the query and ack-join treat as "unknown Release" / "not
yet acked" — both safe defaults.

## Falsifier

Per #2430 and this item: replay #2431 (the first live interrupted-item
case, checkpoint commit `89dc1d5`, note #3134) against this design and
confirm the successor picks it up through the `checkpointed_unacked` bucket
and resumes from `next_action` without repeating the rejected approach
recorded in `rejected`. #2430's evidence note (#3191) already recorded one
live pickup (checkpoint #3134 at 17:07 UTC → pickup dispatch #3180 at
22:03 UTC → accepted #3181, same harness); replaying it against the S6
query bucket (rather than the interim note-scan) is the acceptance evidence
for the implementation slice, not for this design item.

## Implementation slices (fast-build, filed separately)

1. Add `release_digest`, `worktree_host`, `predecessor_session`, `acked_by`
   payload keys to the `lane.checkpoint` note-writing call sites (lane-loop
   prompt / `handoff.py` successor of #2448's runbook wording); stop writing
   `handoff/v1` for lane-loop checkpoints once this lands.
2. Add the ack-join computation and the `checkpointed_unacked` bucket to
   `_next_work_explain_contract` / `read_next_work_explain`, scoped per
   sprint like the existing buckets.
3. Wire `sprintctl next-work --include-checkpoints` (default on) through
   `render.py`.
4. Replay #2431 through the new bucket as the falsifier evidence; record it
   as an evidence note on this item's slice, the same shape as #2430's
   note #3191.
5. Once slice 1 lands, retire the `handoff/v1`-writing branch of the
   maintenance-lane runbook's "Interrupted items" step 1 (owned by #2430,
   not this item — #2430 keeps whatever of the interim rule remains after
   S6 supersedes the file mechanism).

## What this item did not decide

- The operator-facing `~/continue` flow (#2429) — out of scope, as in the
  interim rule.
- Cross-harness (Codex/OpenCode) continuation from the ledger alone — TS-8
  flags this untested; the `checkpointed_unacked` bucket is harness-neutral
  by construction (readable from `sprintctl next-work` plus `git fetch`,
  same as the interim rule's harness-neutral claim), but no cross-harness
  instance has run it yet.
- Retiring the `handoff/v1` writer entirely — left to #2430 per the
  coordination note above, so the two items don't duplicate ownership of
  the same runbook section.
