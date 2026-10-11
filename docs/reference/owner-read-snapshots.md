# Owner read snapshots for effect review and continuation

These operations expose bounded, advisory reads from Sprintctl's PostgreSQL
owner state. They do not create a Decision, accept an effect, acquire a lease,
or authorize a work action. Each result is read in one repeatable-read,
read-only transaction and is scoped to the authenticated repository.

## `work.effect.acceptance-snapshot-v1`

The request is exactly:

```json
{
  "intent_id": "<owner intent id>",
  "revision": 1,
  "canonical_intent_digest": "<64 lowercase hex>",
  "artifact_sha256": "sha256:<64 lowercase hex>",
  "verification_ref": {"run_id": "<run id>", "item_id": "<evidence item id>"}
}
```

`verification_ref` is optional and, when present, must identify a receipt in a
run bound to the current principal, workspace, client and grant. The read
requires both `work.effect.get` and `work:read`. It returns only the exact
effect identity/digest, supplied-versus-owner patch digest comparison, current
item revision/status, current Release digest/revision and declared effect
check revisions, bounded Decision references, and the optional owner's
receipt binding. It does not return diff bytes or private reference URLs.

`authorizes_acceptance` is always `false`. A receipt's check names and
revisions are evaluated with the same pure predicate used by protected effect
acceptance; the result describes a comparison and does not claim the check was
executed. When `effect_check_revisions` is absent from a legacy Release,
revision currentness is `unknown`. `evidence_obligations` are descriptive
labels and remain unmapped unless separately established. Lease/settlement
requirements are outside this snapshot and remain `unknown`.

The Release field `effect_check_revisions`, when present, maps one to 64 exact
check names to lowercase `sha256:<hex>` revisions. Protected acceptance
requires every mapped name to appear once in the protected receipt at that
revision with status `passed`. Absence preserves the prior acceptance rule.

## `work.run.continuation-delta-v1`

The request names an owned successor run and an item explicitly; the owner
never infers an item from a run:

```json
{
  "run_id": "<currently owned successor run>",
  "item_id": 1,
  "checkpoint": {
    "repo_id": "owner/repository",
    "item_id": 1,
    "work_edit_revision": "<owner revision>",
    "status_revision": "<owner revision>",
    "current_release_digest": null,
    "current_decision_digest": "sha256:<64 lowercase hex>",
    "current_reference_set_digest": "sha256:<64 lowercase hex>"
  },
  "limit": 32,
  "cursor": null,
  "after_note_id": null,
  "after_chain_seq": null
}
```

The checkpoint is caller-owned comparison data, not authority. It must name
the same repository and item as the current request. An absent checkpoint
returns `comparison: unknown` for every comparison; it never synthesizes an
unchanged baseline. The owner returns a canonical snapshot digest over the
current item, current Release, current Decision facts, reference-set digest
and current unacknowledged source checkpoint. The result includes a bounded
title/description excerpt, reference records, and one-hop predecessor
notes/evidence selected by the existing note-id and chain-sequence cursors.
The source checkpoint is an owner-declared note with its timestamp and any
declared Git SHA; no Git inspection occurs. Reference digests cover owner
reference records only. Source bytes, missing external content and
correspondence between a reference record and Git content remain `unknown`.

Decision and reference facts are capped at 64 rows per collection. A complete
collection digest is returned only when the full set fits inside that cap;
larger sets return a null digest and comparisons for that digest are
`unknown`. Counts and append-only high-water IDs remain in the snapshot digest
so a reference cursor can detect additions and removals without hashing a
paged subset. Decision rows are append-only and reference IDs are monotonically
allocated; deletion and reinsertion therefore changes the count or high-water
ID even when the population size is unchanged.

Reference paging uses a cursor object containing `expected_snapshot_digest`,
`checkpoint_digest` and `last_reference_id`. A later
page recomputes current owner facts inside its own read-only transaction. A
different snapshot or checkpoint is rejected as stale before reference
content is returned. Cursors select a page; they never establish authority.
Reference pages and predecessor notes/evidence are separately bounded, and
the owner reports truncation and next cursors without implying that a page is
the complete population.
