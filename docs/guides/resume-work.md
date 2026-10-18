# Resume Work

The resume path should be mechanical:

1. read the most recent handoff bundle if one exists
2. refresh live state with `usage --context` + `next-work --explain`
3. inspect the target item only if you need more detail
4. resume or recreate the reservation

If your global `sprintctl` install is older than the repository source, run
commands via `python -m sprintctl` from the repo so options like
`next-work --explain` are available.

## Live Resume Path

```sh
sprintctl usage --context --json
sprintctl next-work --json --explain
sprintctl item show --id <id> --json
```

Prioritize the `next_action` and `conflicts` fields from `usage --context`.
For a quick human view, use `sprintctl next-work --explain`.
In JSON mode, `next-work --json --explain` includes both
`recommended_commands` and `recommended_command_bundle` (structured step
metadata with placeholder/executability flags), so restart automation can
execute or preflight a concrete next-step bundle.

In local SQLite mode, `session resume --json` mirrors this with `recommended_sequence` and
`recommended_sequence_bundle`, and it surfaces active reservations and their
activity state.

`session resume` is a convenience surface in local SQLite mode. The served
catalog deliberately does not expose that combined command. Use the constituent
reads above and `sprintctl reservation list --all --json` in served mode, without
switching to another database. Served continuation is supported through the
persisted checkpoint and run-context reads.

## Brief one served item

With your own registered run and an explicit item in the current repository:

```sh
sprintctl run brief --run-id <your-run-id> --item-id <item-id>
sprintctl run brief --run-id <your-run-id> --item-id <item-id> --format json > brief.json
jq '.delta.current' brief.json > checkpoint.json
```

On a later continuation, compare with that saved checkpoint:

```sh
sprintctl run brief --run-id <your-current-run-id> --item-id <item-id> --checkpoint checkpoint.json
```

`--checkpoint` takes the complete seven-field owner checkpoint in `delta.current`,
including repository and item identity. It does not take the whole briefing or
`source_checkpoint` (the recorded Git continuation point). Keep the JSON snapshot
with the work's existing artifacts; the CLI creates no new store.

The briefing performs one `work.run.continuation-delta-v1` read. It groups owner
comparisons into **changed**, **unchanged**, and **unknown**, shows the latest
decision, references, continuation point, and checks needed before continuing.
Without a checkpoint, comparison is unknown. Unavailable digests also remain
unknown. Reference equality covers owner reference records, not source bytes,
artifact accessibility, or Git correspondence. The briefing never grants action
authority or treats a decision record as proof that an external effect occurred.
Text shows at most five reference URLs, each excerpted to 200 characters, and
names omitted content. JSON retains the full owner page and decision evidence.

Reads are bounded (`--limit` 1–64, default 32), without automatic paging. JSON
preserves the full owner response, completeness flags, and cursors. To continue
references, save `.delta.next_reference_cursor` as a JSON object and pass it with
`--cursor`; retain the same checkpoint. The owner refuses a stale cursor when
its snapshot basis changes. Use `--after-note-id` and `--after-chain-seq` for the
independent predecessor cursors. Decision history may remain truncated even after
all reference pages have been read. Use `run context` when you need predecessor
note/evidence content; the briefing only carries their references.

The source command requires an authority advertising the existing continuation
delta operation. A refused or unavailable served read is a failed check, never
an empty result or an invitation to use a stale local database.

## If a handoff bundle exists

```sh
cat handoff.json | jq '.summary, .work, .next_action'
```

Then refresh with live state:

```sh
sprintctl usage --context --json
```

The handoff bundle is a snapshot. `usage --context` is the current answer.
Text handoffs show the predecessor's recorded position separately from current
Git observations. A dirty predecessor commit does not identify uncommitted
contents; unavailable current Git context does not mean the worktree is clean.

## If a reservation is involved

Find active reservations:

```sh
sprintctl reservation list --all --json
```

Reassign an existing reservation to the current session:

```sh
sprintctl reservation reassign \
  --id <reservation-id> \
  --actor <you> \
  --session-id "$SPRINTCTL_RUNTIME_SESSION_ID" \
  --json
```

If no active reservation exists, create a new one with `sprintctl reservation
reserve`. There is no token or recovery file.

## Resume Checklist

- check `conflicts` before starting new work
- inspect `recent_decisions` before repeating context gathering
- use `reservation list --all` before creating a potentially overlapping reservation
- use `item show` only after `usage --context` narrows the target

## Related

- [Start Here](start-here.md)
- [Work Loop](work-loop.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
