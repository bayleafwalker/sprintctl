# Resume Work

The resume path should be mechanical:

1. read the most recent handoff bundle if one exists
2. refresh live state with `session resume` (or `usage --context` + `next-work --explain`)
3. inspect the target item only if you need more detail
4. resume or reclaim ownership

If your global `sprintctl` install is older than the repository source, run
commands via `python -m sprintctl` from the repo so options like
`next-work --explain` are available.

## Live Resume Path

```sh
sprintctl session resume --json
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

`session resume --json` mirrors this with `recommended_sequence` and
`recommended_sequence_bundle`, and it now includes a top-level
`claim_recovery` field that reports active claim IDs, local recovery-token file
status, recovery-token paths, and current runtime/instance match hints.

`session resume` is a convenience surface that packages those checks into one
output contract. The underlying commands remain the source of truth and should
still be used when you need to script one surface independently.

## If a handoff bundle exists

```sh
cat handoff.json | jq '.summary, .work, .next_action'
```

Then refresh with live state:

```sh
sprintctl usage --context --json
```

The handoff bundle is a snapshot. `usage --context` is the current answer.

## If a claim is involved

Find your claims by identity:

```sh
sprintctl claim resume --instance-id "$SPRINTCTL_INSTANCE_ID" --json
```

If sprintctl previously wrote a local recovery file for the claim, restore the
token directly:

```sh
sprintctl claim recover --id <claim-id> --json
```

If no local recovery file exists and the claim is legacy/ambiguous:

```sh
sprintctl claim handoff --id <claim-id> --actor <you> --mode rotate --allow-legacy-adopt --json
```

## Resume Checklist

- check `conflicts` before starting new work
- inspect `recent_decisions` before repeating context gathering
- use `claim resume` before creating a competing claim
- use `claim_recovery` from `session resume --json` to confirm whether a local token file exists before escalating to adoption
- use `item show` only after `usage --context` narrows the target

## Related

- [Start Here](start-here.md)
- [Work Loop](work-loop.md)
- [Context and Handoff Contracts](../reference/context-and-handoff.md)
